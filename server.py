#!/usr/bin/env python3
"""Sentry trace viewer — local web UI with file picker."""

import json
import os
import re
import subprocess
import tempfile
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HERE = Path(__file__).parent
SENTRY_DIR = HERE / 'sentry'
PORT = 8765
COLORS_FILE = HERE / 'table-colors.json'
SVG_DIR = HERE / 'svg'

DEFAULT_COLORS = {
    'spread': 35,
}


def load_colors():
    """Load saved spread value or return default."""
    if COLORS_FILE.exists():
        try:
            data = json.loads(COLORS_FILE.read_text())
            return {**DEFAULT_COLORS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_COLORS)


def save_colors(colors):
    """Persist spread value to disk."""
    merged = {**DEFAULT_COLORS, **colors}
    COLORS_FILE.write_text(json.dumps(merged, indent=2))
    return merged


# ── data processing ──────────────────────────────────────────────────────


def build_flat_tree(spans):
    """Convert flat spans to depth-first ordered list with precomputed depth."""
    span_map = {s['span_id']: s for s in spans}
    children: dict[str, list] = {}
    for s in spans:
        children.setdefault(s['span_id'], [])
    roots = []
    for s in spans:
        pid = s.get('parent_span_id')
        if pid in span_map:
            children.setdefault(pid, []).append(s)
        else:
            roots.append(s)

    for sid in children:
        children[sid].sort(key=lambda x: x.get('start_timestamp', 0))
    roots.sort(key=lambda x: x.get('start_timestamp', 0))

    result = []

    def walk(nodes, depth):
        for s in nodes:
            sid = s['span_id']
            dur = (s['timestamp'] - s['start_timestamp']) * 1000
            result.append({
                'span_id': sid,
                'parent_span_id': s.get('parent_span_id'),
                'op': s.get('op', ''),
                'description': _clean_desc(s.get('description', '')),
                'duration_ms': round(dur, 2),
                'start_ts': s['start_timestamp'],
                'end_ts': s['timestamp'],
                'depth': depth,
                'has_children': len(children.get(sid, [])) > 0,
            })
            walk(children.get(sid, []), depth + 1)

    walk(roots, 0)
    return result


def group_db_spans(spans):
    """Group consecutive db spans into collapsible parent entries."""
    if not spans:
        return spans
    result = []
    i = 0
    while i < len(spans):
        s = spans[i]
        # Start a group if this db span is followed by another at same level
        if (s['op'] == 'db'
                and i + 1 < len(spans)
                and spans[i + 1]['op'] == 'db'
                and spans[i + 1]['depth'] == s['depth']
                and spans[i + 1].get('parent_span_id') == s.get('parent_span_id')
                and not s['has_children']):
            # Collect the run
            db_children = []
            total_dur = 0.0
            group_pid = s.get('parent_span_id')
            group_depth = s['depth']
            while (i < len(spans)
                   and spans[i]['op'] == 'db'
                   and spans[i]['depth'] == group_depth
                   and spans[i].get('parent_span_id') == group_pid
                   and not spans[i]['has_children']):
                db_children.append(spans[i])
                total_dur += spans[i]['duration_ms']
                i += 1
            if len(db_children) >= 2:
                group = {
                    'span_id': f"db-group-{len(result)}",
                    'parent_span_id': group_pid,
                    'op': 'db',
                    'description': '',
                    'duration_ms': round(total_dur, 2),
                    'start_ts': db_children[0]['start_ts'],
                    'end_ts': db_children[-1]['end_ts'],
                    'depth': group_depth,
                    'has_children': True,
                    'is_group': True,
                }
                result.append(group)
                for ch in db_children:
                    ch['depth'] = group_depth + 1
                    ch['parent_span_id'] = group['span_id']
                    ch['has_children'] = False
                    result.append(ch)
            else:
                # Single db span, no grouping needed
                result.append(db_children[0])
        else:
            result.append(s)
            i += 1
    return result


def filter_spans(spans):
    """Remove spans the user doesn't want to see."""
    return [s for s in spans if s['op'] != 'POST /message http send']


def apply_ttft_mode(spans):
    """Cut trace at the first-token marker under ResponseSynthesizer.run.
    Returns (spans, ttft_time_or_None)."""
    # 1. Find ResponseSynthesizer.run
    rs = None
    for s in spans:
        if s['op'] == 'ResponseSynthesizer.run':
            rs = s
            break
    if not rs:
        return spans, None

    # 2. Find first child of ResponseSynthesizer.run
    child = None
    for s in spans:
        if s.get('parent_span_id') == rs['span_id']:
            child = s
            break
    if not child:
        return spans, None

    source_start = child['start_ts']

    # 3. Find first POST /message http send that starts >= source_start
    ttft = None
    for s in spans:
        if s['op'] == 'POST /message http send' and s['start_ts'] >= source_start:
            ttft = s
            break
    if not ttft:
        return spans, None

    ttft_time = ttft['start_ts']

    # 4. Truncate the source child's duration
    new_dur = (ttft_time - source_start) * 1000
    child['duration_ms'] = round(max(0, new_dur), 2)
    child['end_ts'] = ttft_time

    # 5. Cut: keep only spans that start before ttft_time
    result = [s for s in spans if s['start_ts'] < ttft_time]

    # 6. Truncate every remaining span whose end exceeds ttft_time.
    #    This fixes parent durations so they add up correctly.
    for s in result:
        if s['end_ts'] > ttft_time:
            new_dur = (ttft_time - s['start_ts']) * 1000
            s['duration_ms'] = round(max(0, new_dur), 2)
            s['end_ts'] = ttft_time

    return result, ttft_time


def _clean_desc(desc):
    if not desc:
        return ''
    desc = desc.replace('\n', ' ').replace('\r', ' ')
    return re.sub(r'\s+', ' ', desc).strip()


# ── HTML ────────────────────────────────────────────────────────────────


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Load Testing Formatter</title>
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
  background: #0d1117;
  color: #e6edf3;
  height: 100vh;
  display: flex;
  flex-direction: row;
  overflow: hidden;
}
/* ── sidebar ── */
.sidebar {
  width: 200px;
  flex-shrink: 0;
  background: #161b22;
  border-right: 1px solid #30363d;
  padding: 20px 16px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.sidebar-title {
  font-size: 15px;
  font-weight: 700;
  color: #f0f6fc;
}
.sidebar-section label {
  display: block;
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #8b949e;
  margin-bottom: 6px;
}
.sidebar-section select {
  appearance: none;
  -webkit-appearance: none;
  width: 100%;
  padding: 6px 28px 6px 10px;
  border: 1px solid #30363d;
  border-radius: 6px;
  background: #0d1117 url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' fill='%238b949e'%3E%3Cpath d='M6 8L2 4h8z'/%3E%3C/svg%3E") no-repeat right 8px center;
  color: #e6edf3;
  font-size: 13px;
  cursor: pointer;
  outline: none;
}
.sidebar-section select:focus { border-color: #58a6ff; }
.sidebar-section select option { background: #161b22; }
.sidebar-section .mode-wrap {
  display: flex;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  overflow: hidden;
}
.sidebar-section .mode-opt {
  flex: 1;
  padding: 6px 0;
  font-size: 12px;
  font-weight: 600;
  text-align: center;
  cursor: pointer;
  color: #8b949e;
  transition: background 0.15s, color 0.15s;
  user-select: none;
}
.sidebar-section .mode-opt:hover { background: #1c2128; color: #e6edf3; }
.sidebar-section .mode-opt.active { background: #1f6f2e; color: #fff; }
.sidebar-section input[type=number] {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid #30363d;
  border-radius: 6px;
  background: #0d1117;
  color: #e6edf3;
  font-size: 13px;
  outline: none;
}
.sidebar-section input[type=number]:focus { border-color: #58a6ff; }

/* ── main content ── */
.main {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 20px 24px;
  min-width: 0;
}
/* ── header ── */
.header {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid #30363d;
}
.meta {
  display: flex;
  gap: 16px;
  font-size: 13px;
  color: #8b949e;
}
.meta span { display: inline-flex; align-items: center; gap: 4px; }
.meta strong { color: #e6edf3; font-weight: 600; }
.trace-id { font-family: 'SFMono-Regular', 'Cascadia Code', 'Fira Code', monospace; font-size: 12px; color: #8b949e; }
/* ── toolbar ── */
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 0;
}
.search-box {
  flex: 1;
  max-width: 360px;
  padding: 7px 12px;
  border: 1px solid #30363d;
  border-radius: 6px;
  background: #161b22;
  color: #e6edf3;
  font-size: 13px;
  outline: none;
}
.search-box:focus { border-color: #58a6ff; }
.search-box::placeholder { color: #6e7681; }
.stats { font-size: 12px; color: #8b949e; white-space: nowrap; }
.export-btn {
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid #30363d;
  border-radius: 6px;
  background: #21262d;
  color: #e6edf3;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s;
}
.export-btn:hover { background: #30363d; }
/* ── table ── */
.table-wrap {
  flex: 1;
  overflow-y: auto;
  border: 1px solid #30363d;
  border-radius: 8px;
  background: #161b22;
  font-size: 13px;
}
table { max-width: 100%; border-collapse: collapse; }
thead { position: sticky; top: 0; z-index: 2; }
th {
  padding: 8px 14px;
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #8b949e;
  background: #1c2128;
  border-bottom: 1px solid #30363d;
  white-space: nowrap;
}
th.name-th { text-align: left; }
th.dur-th { text-align: right; width: 120px; }
td {
  padding: 0;
  border-bottom: 1px solid #21262d;
  vertical-align: middle;
}
tr:not(.hidden):hover td { background: #1c2128; }
tr.hidden { display: none; }

/* Row inner: two cells side by side */
.row-inner {
  display: flex;
  align-items: center;
  padding: 5px 14px;
  min-height: 32px;
}
.name-cell {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  min-width: 0;
  flex: 0 1 auto;
  overflow: hidden;
  max-width: 65%;
}
.dur-cell {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex: 0 0 auto;
  justify-content: flex-end;
  width: 120px;
  flex-shrink: 0;
  margin-left: auto;
}
.indent { display: inline-block; flex-shrink: 0; }
.toggle {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 3px;
  cursor: pointer;
  font-size: 10px;
  color: #8b949e;
  user-select: none;
  transition: background 0.15s;
}
.toggle:hover { background: #30363d; color: #e6edf3; }
.toggle.leaf { visibility: hidden; pointer-events: none; }
.op-label {
  font-weight: 600;
  color: #e6edf3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.desc-label {
  font-size: 11px;
  color: #6e7681;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.count-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 7px;
  height: 18px;
  border-radius: 999px;
  background: #1f2937;
  color: #8b949e;
  font-size: 11px;
  font-weight: 600;
  margin-left: 4px;
  flex-shrink: 0;
  line-height: 1;
}
.dur-bar {
  width: 50px;
  height: 6px;
  background: #21262d;
  border-radius: 3px;
  overflow: hidden;
  flex-shrink: 0;
}
.dur-fill { height: 100%; border-radius: 3px; }
.dur-text {
  font-family: 'SFMono-Regular', 'Cascadia Code', 'Fira Code', monospace;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  min-width: 44px;
  text-align: right;
}
.dur-text.slow { color: #ff7b72; }
.dur-text.warn { color: #d29922; }
.dur-text.fast { color: #3fb950; }
.dur-text.very-fast { color: #6e7681; }

.table-view {
  table-layout: fixed;
  max-width: none;
}
.table-view th,
.table-view td {
  border-right: 1px solid #30363d;
}
.table-view td {
  border-bottom: 2px solid #6e7681;
}
.table-view th:last-child,
.table-view td:last-child {
  border-right: none;
}
.table-view th {
  border-bottom: 2px solid #6e7681;
}
.table-view th.dur-th {
  text-align: center;
}

.table-view td.name-cell { padding: 6px 14px; max-width: 460px; overflow: hidden; }
.table-view td.dur-cell {
  display: table-cell;
  text-align: right;
  font-family: 'SFMono-Regular', 'Cascadia Code', 'Fira Code', monospace;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  padding: 6px 14px;
  width: 120px;
  margin-left: 0;
  flex: none;
  justify-content: initial;
  gap: 0;
}
.table-view td.dur-cell.slow { color: #ff7b72; }
.table-view td.dur-cell.warn { color: #d29922; }
.table-view td.dur-cell.fast { color: #3fb950; }
.table-view td.dur-cell.very-fast { color: #6e7681; }
.table-view .name-wrap { min-width: 0; }
.table-view .name-main {
  font-weight: 600;
  color: #e6edf3;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.table-view .name-desc {
  font-size: 11px;
  color: #6e7681;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  margin-top: 2px;
}
/* Depth-based row backgrounds — applied dynamically via JS (table-colors.json) */
/* Hover override stays strongest */
.table-view tbody tr:hover td { background: #1c2128 !important; }

/* Spread slider + swatch preview */
#spread-slider {
  width: 100%;
  height: 6px;
  -webkit-appearance: none;
  appearance: none;
  background: #30363d;
  border-radius: 3px;
  outline: none;
  cursor: pointer;
}
#spread-slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #58a6ff;
  border: none;
  cursor: pointer;
}
#spread-slider::-moz-range-thumb {
  width: 16px; height: 16px;
  border-radius: 50%;
  background: #58a6ff;
  border: none;
  cursor: pointer;
}
.spread-swatch-row {
  display: flex;
  gap: 2px;
  margin-top: 6px;
}
.spread-swatch-row .swatch {
  flex: 1;
  height: 20px;
  border-radius: 3px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 700;
  color: rgba(255,255,255,0.7);
  text-shadow: 0 1px 2px rgba(0,0,0,0.5);
  border: 1px solid #30363d;
}


/* Empty state */
.empty-state {
  text-align: center;
  padding: 60px 20px;
  color: #6e7681;
}
.empty-state p { font-size: 14px; margin-top: 8px; }
.empty-state .big { font-size: 40px; margin-bottom: 12px; }

/* Loading */
.loading { text-align: center; padding: 40px; color: #8b949e; }
.spinner {
  display: inline-block;
  width: 24px; height: 24px;
  border: 2px solid #30363d;
  border-top-color: #58a6ff;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* Scrollbar */
.table-wrap::-webkit-scrollbar { width: 8px; }
.table-wrap::-webkit-scrollbar-track { background: #161b22; }
.table-wrap::-webkit-scrollbar-thumb { background: #30363d; border-radius: 4px; }
.table-wrap::-webkit-scrollbar-thumb:hover { background: #484f58; }

@media (max-width: 640px) {
  body { flex-direction: column; }
  .sidebar { width: 100%; border-right: none; border-bottom: 1px solid #30363d; padding: 12px; flex-direction: row; flex-wrap: wrap; gap: 10px; align-items: center; }
  .sidebar-title { font-size: 14px; }
  .sidebar-section { min-width: 140px; flex: 1; }
  .sidebar-section label { margin-bottom: 3px; }
  .main { padding: 12px; }
  .dur-bar { width: 30px; }
}
</style>
</head>
<body>

<div class="sidebar">
  <div class="sidebar-title">🏷️ Load Testing Formatter</div>
  <div class="sidebar-section">
    <label for="file-picker">Trace File</label>
    <select id="file-picker" onchange="loadFile()"></select>
  </div>
  <div class="sidebar-section">
    <label>Mode</label>
    <div class="mode-wrap">
      <span class="mode-opt" data-mode="full" onclick="setMode('full')">Full</span>
      <span class="mode-opt" data-mode="ttft" onclick="setMode('ttft')">TTFT</span>
    </div>
  </div>
  <div class="sidebar-section">
    <label>View</label>
    <div class="mode-wrap">
      <span class="mode-opt" data-view="trace" onclick="setView('trace')">Trace</span>
      <span class="mode-opt" data-view="table" onclick="setView('table')">Table</span>
    </div>
  </div>
  <div class="sidebar-section" id="depth-section" style="display:none">
    <label>Depth</label>
    <input type="number" id="depth-input" value="3" min="1" max="9" onchange="setDepth(this.value)">
  </div>
  <div class="sidebar-section" id="color-section" style="display:none">
    <label>Spread <span id="spread-label">35</span></label>
    <input type="range" id="spread-slider" min="0" max="100" value="35" oninput="setSpread(this.value)">
    <div class="spread-swatch-row">
      <span class="swatch" data-d="0">0</span>
      <span class="swatch" data-d="1">1</span>
      <span class="swatch" data-d="2">2</span>
      <span class="swatch" data-d="3">3</span>
      <span class="swatch" data-d="4">4</span>
      <span class="swatch" data-d="5">5</span>
      <span class="swatch" data-d="6">6</span>
      <span class="swatch" data-d="7">7</span>
      <span class="swatch" data-d="8">8</span>
    </div>
  </div>
</div>

<div class="main">

<div class="header">
  <div class="meta" id="meta-bar">
    <span>Duration <strong id="meta-dur">—</strong></span>
    <span>Spans <strong id="meta-spans">—</strong></span>
    <span>Trace <strong class="trace-id" id="meta-trace">—</strong></span>
  </div>
</div>

<div class="toolbar">
  <input class="search-box" id="search" type="text" placeholder="Filter by operation or description…" autofocus disabled>
  <span class="stats" id="stats"></span>
  <button class="export-btn" id="export-btn" onclick="exportPNG()" title="Export table as PNG">Export PNG</button>
</div>

<div class="table-wrap" id="table-wrap">
  <div class="empty-state" id="empty-state">
    <div class="big">📂</div>
    <p>Select a trace file above to visualize</p>
  </div>
  <table id="trace-table" style="display:none">
    <thead><tr><th class="name-th">Name</th><th class="dur-th">Duration</th></tr></thead>
    <tbody id="trace-body"></tbody>
  </table>
  <div class="loading" id="loading" style="display:none">
    <div class="spinner"></div>
    <p style="margin-top:12px">Loading trace…</p>
  </div>
</div>

</div><!-- /.main -->

<script>
// ── State ──────────────────────────────────────────────────────────

let SPANS = [];
let TOTAL_MS = 0;
let CURRENT_MODE = 'full';
let CURRENT_VIEW = 'trace';
let CURRENT_DEPTH = 3;
let COLOR_SPREAD = 35;
let _saveTimer = null;

// ── Color management (single spread slider) ──────────────────────

const BASE_RGB = [13, 17, 23]; // #0d1117 — page background

function depthColor(level, spread) {
  const ratio = spread / 100;
  const t = (level / 8) * ratio;
  const r = Math.round(BASE_RGB[0] + (255 - BASE_RGB[0]) * t);
  const g = Math.round(BASE_RGB[1] + (255 - BASE_RGB[1]) * t);
  const b = Math.round(BASE_RGB[2] + (255 - BASE_RGB[2]) * t);
  return '#' + [r, g, b].map(function(c) { return c.toString(16).padStart(2, '0'); }).join('');
}

function updateSwatches() {
  const swatches = document.querySelectorAll('.spread-swatch-row .swatch');
  for (const el of swatches) {
    const d = parseInt(el.dataset.d, 10);
    el.style.background = depthColor(d, COLOR_SPREAD);
  }
}

async function loadColors() {
  try {
    const res = await fetch('/api/colors');
    const data = await res.json();
    COLOR_SPREAD = data.spread !== undefined ? data.spread : 35;
  } catch (_) { /* keep default */ }
  document.getElementById('spread-slider').value = COLOR_SPREAD;
  updateSwatches();
  applyColorStyle();
  if (CURRENT_VIEW === 'table' && SPANS.length) doRender();
}

function setSpread(val) {
  COLOR_SPREAD = parseInt(val, 10);
  document.getElementById('spread-label').textContent = COLOR_SPREAD;
  updateSwatches();
  applyColorStyle();
  if (CURRENT_VIEW === 'table') doRender();
  // debounce save to avoid flooding the server while dragging
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(function() {
    fetch('/api/colors', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({spread: COLOR_SPREAD})
    }).catch(function(){});
    _saveTimer = null;
  }, 300);
}

function applyColorStyle() {
  let style = document.getElementById('depth-color-style');
  if (!style) {
    style = document.createElement('style');
    style.id = 'depth-color-style';
    document.head.appendChild(style);
  }
  let css = '';
  for (let d = 0; d <= 8; d++) {
    css += '.table-view tbody tr[data-depth="' + d + '"] td { background: ' + depthColor(d, COLOR_SPREAD) + '; }\n';
  }
  style.textContent = css;
}

function setMode(mode) {
  CURRENT_MODE = mode;
  document.querySelectorAll('[data-mode]').forEach(function(el) {
    el.classList.toggle('active', el.dataset.mode === mode);
  });
  if (document.getElementById('file-picker').value) {
    loadFile();
  }
}

// ── View toggle ──────────────────────────────────────────────────

function setView(view) {
  CURRENT_VIEW = view;
  document.querySelectorAll('[data-view]').forEach(function(el) {
    el.classList.toggle('active', el.dataset.view === view);
  });
  document.getElementById('depth-section').style.display = view === 'table' ? '' : 'none';
  document.getElementById('color-section').style.display = view === 'table' ? '' : 'none';
  document.getElementById('search').disabled = view === 'table';
  if (view === 'table') {
    document.getElementById('search').value = '';
  }
  if (SPANS.length > 0) {
    doRender();
  }
}

function setDepth(depth) {
  CURRENT_DEPTH = parseInt(depth) || 3;
  if (SPANS.length > 0 && CURRENT_VIEW === 'table') {
    doRender();
  }
}

// ── File picker ────────────────────────────────────────────────────

async function init() {
  try {
    const res = await fetch('/api/files');
    const files = await res.json();
    const sel = document.getElementById('file-picker');
    sel.innerHTML = files.map((f, i) =>
      `<option value="${f.name}"${i === 0 ? ' selected' : ''}>${f.name}</option>`
    ).join('');
    if (files.length > 0) loadFile();
  } catch (e) {
    document.getElementById('empty-state').innerHTML =
      '<p style="color:#ff7b72">Failed to load file list</p>';
  }
}

// ── Load trace ─────────────────────────────────────────────────────

async function loadFile() {
  const file = document.getElementById('file-picker').value;
  if (!file) return;

  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('trace-table').style.display = 'none';
  document.getElementById('loading').style.display = 'block';
  document.getElementById('search').disabled = true;

  try {
    const res = await fetch('/api/trace?file=' + encodeURIComponent(file) + '&mode=' + CURRENT_MODE);
    const data = await res.json();

    document.getElementById('meta-dur').textContent = fmtDur(data.duration_ms);
    document.getElementById('meta-spans').textContent = data.total_spans;
    document.getElementById('meta-trace').textContent =
      (data.trace_id || '').slice(0, 16) + '…';

    SPANS = data.spans;
    TOTAL_MS = data.duration_ms;

    document.getElementById('trace-table').style.display = '';
    doRender();

    document.getElementById('loading').style.display = 'none';
    document.getElementById('search').disabled = false;
    document.getElementById('search').value = '';
    document.getElementById('search').focus();
  } catch (e) {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('empty-state').style.display = '';
    document.getElementById('empty-state').innerHTML =
      '<p style="color:#ff7b72">Error loading trace</p>';
  }
}

// ── Format ─────────────────────────────────────────────────────────

function truncate(str, max) {
  if (!str || str.length <= max) return str;
  return str.slice(0, max) + '…';
}

function fmtDur(ms) {
  if (ms >= 1000) return (ms / 1000).toFixed(2) + 's';
  if (ms >= 1) return ms.toFixed(0) + 'ms';
  return ms.toFixed(2) + 'ms';
}

function durClass(ms, total) {
  if (ms >= total * 0.1) return 'slow';
  if (ms >= total * 0.02) return 'warn';
  if (ms >= 1) return 'fast';
  return 'very-fast';
}

function durBarWidth(ms, total) {
  return Math.max(2, Math.min(100, (ms / total) * 100));
}

function durBarColor(ms, total) {
  if (ms >= total * 0.1) return '#da3633';
  if (ms >= total * 0.02) return '#d29922';
  if (ms >= 1) return '#3fb950';
  return '#6e7681';
}

// ── Render ─────────────────────────────────────────────────────────

function doRender() {
  if (CURRENT_VIEW === 'table') {
    renderTableView(SPANS, TOTAL_MS, CURRENT_DEPTH);
  } else {
    renderTraceView(SPANS, TOTAL_MS);
  }
}

function renderTableView(spans, totalMs, maxDepth) {
  const tbody = document.getElementById('trace-body');
  tbody.innerHTML = '';

  // Filter to maxDepth and build ancestor stack
  const filtered = [];
  const ancestorStack = []; // [{depth, span}]

  for (const s of spans) {
    if (s.depth >= maxDepth) continue;
    while (ancestorStack.length > 0 && ancestorStack[ancestorStack.length - 1].depth >= s.depth) {
      ancestorStack.pop();
    }
    ancestorStack.push({depth: s.depth, span: s});
    filtered.push({span: s, ancestors: [...ancestorStack]});
  }

  const table = document.getElementById('trace-table');
  table.className = 'table-view';
  table.style.width = (460 + maxDepth * 120) + 'px';

  const theadRow = table.querySelector('thead tr');
  theadRow.innerHTML = '<th class="name-th" style="width:460px">Item</th>'
    + '<th class="dur-th" colspan="' + maxDepth + '" style="width:' + (maxDepth * 120) + 'px">Duration</th>';

  // Remove colgroup if present — we use th widths exclusively
  const cg = table.querySelector('colgroup');
  if (cg) cg.remove();

  // ── Compute rowspans (deepest → shallowest) ──
  const spanMap = {};
  for (let i = 0; i < filtered.length; i++) {
    const row = filtered[i];
    for (let di = maxDepth - 1; di >= 0; di--) {
      const ancestor = row.ancestors.find(function(a) { return a.depth === di; });
      const key = i + '_' + di;
      if (!ancestor) { spanMap[key] = 1; continue; }
      if (i > 0) {
        const prev = filtered[i - 1].ancestors.find(function(a) { return a.depth === di; });
        if (prev && prev.span.span_id === ancestor.span.span_id) { spanMap[key] = 0; continue; }
      }
      let span = 1;
      for (let j = i + 1; j < filtered.length; j++) {
        const next = filtered[j].ancestors.find(function(a) { return a.depth === di; });
        if (next && next.span.span_id === ancestor.span.span_id) span++;
        else break;
      }
      spanMap[key] = span;
    }
  }

  // ── Render rows ──
  for (let i = 0; i < filtered.length; i++) {
    const row = filtered[i];
    const s = row.span;
    const ancestors = row.ancestors;
    const tr = document.createElement('tr');
    tr.setAttribute('data-depth', s.depth);

    const nameTd = document.createElement('td');
    nameTd.className = 'name-cell';
    nameTd.style.width = '460px';
    const nameWrap = document.createElement('div');
    nameWrap.className = 'name-wrap';
    nameWrap.style.paddingLeft = (s.depth * 20) + 'px';
    const nameMain = document.createElement('div');
    nameMain.className = 'name-main';
    nameMain.textContent = truncate(s.op, 60);
    nameMain.title = s.op;
    nameWrap.appendChild(nameMain);
    if (s.description && s.description !== s.op) {
      const desc = document.createElement('div');
      desc.className = 'name-desc';
      desc.textContent = truncate(s.description, 60);
      desc.title = s.description;
      nameWrap.appendChild(desc);
    }
    nameTd.appendChild(nameWrap);
    tr.appendChild(nameTd);

    for (let di = maxDepth - 1; di >= 0; di--) {
      const span = spanMap[i + '_' + di];
      const durTd = document.createElement('td');
      durTd.className = 'dur-cell';
      durTd.style.width = '120px';
      if (span > 1) durTd.setAttribute('rowspan', span);
      if (span > 0) {
        const ancestor = ancestors.find(function(a) { return a.depth === di; });
        if (ancestor) {
          const dur = ancestor.span.duration_ms;
          durTd.textContent = fmtDur(dur);
          durTd.classList.add(durClass(dur, totalMs));
        } else {
          // empty cell — no dash
        }
      }
      tr.appendChild(durTd);
    }

    tbody.appendChild(tr);
  }

  updateStats();
}

function renderTraceView(spans, totalMs) {
  const tbody = document.getElementById('trace-body');
  tbody.innerHTML = '';
  const table = document.getElementById('trace-table');
  table.className = '';
  table.style.width = '';
  const colgroup = table.querySelector('colgroup');
  if (colgroup) colgroup.remove();
  document.querySelector('#trace-table thead tr').innerHTML =
    '<th class="name-th">Name</th><th class="dur-th">Duration</th>';

  for (const s of spans) {
    const tr = document.createElement('tr');
    tr.dataset.spanId = s.span_id;
    tr.dataset.depth = s.depth;
    if (s.has_children) tr.dataset.hasChildren = '1';

    // ── Name cell ──
    const nameCell = document.createElement('div');
    nameCell.className = 'name-cell';

    const indent = document.createElement('span');
    indent.className = 'indent';
    indent.style.width = (s.depth * 20) + 'px';
    nameCell.appendChild(indent);

    const toggle = document.createElement('span');
    toggle.className = 'toggle' + (s.has_children ? '' : ' leaf');
    toggle.textContent = s.has_children ? '▶' : '─';
    if (s.has_children) {
      toggle.addEventListener('click', function (e) {
        e.stopPropagation();
        tr.classList.toggle('collapsed');
        this.textContent = tr.classList.contains('collapsed') ? '▶' : '▼';
        updateVisibility();
        updateStats();
      });
    }
    nameCell.appendChild(toggle);

    const opSpan = document.createElement('span');
    opSpan.className = 'op-label';
    opSpan.textContent = truncate(s.op, 60);
    opSpan.title = s.op;
    nameCell.appendChild(opSpan);

    if (s.description && s.description !== s.op) {
      const descSpan = document.createElement('span');
      descSpan.className = 'desc-label';
      descSpan.textContent = truncate(s.description, 60);
      descSpan.title = s.description;
      nameCell.appendChild(descSpan);
    }

    // ── Duration cell ──
    const durCell = document.createElement('div');
    durCell.className = 'dur-cell';

    const bar = document.createElement('span');
    bar.className = 'dur-bar';
    const fill = document.createElement('span');
    fill.className = 'dur-fill';
    fill.style.width = durBarWidth(s.duration_ms, totalMs) + '%';
    fill.style.background = durBarColor(s.duration_ms, totalMs);
    bar.appendChild(fill);

    const durText = document.createElement('span');
    durText.className = 'dur-text ' + durClass(s.duration_ms, totalMs);
    durText.textContent = fmtDur(s.duration_ms);

    durCell.appendChild(bar);
    durCell.appendChild(durText);

    // ── Assemble row ──
    const nameTd = document.createElement('td');
    const inner = document.createElement('div');
    inner.className = 'row-inner';
    inner.appendChild(nameCell);
    nameTd.appendChild(inner);

    const durTd = document.createElement('td');
    durTd.appendChild(durCell);

    tr.appendChild(nameTd);
    tr.appendChild(durTd);
    tbody.appendChild(tr);
  }

  // Collapse depth >= 2 by default
  document.querySelectorAll('#trace-body tr').forEach(function (row) {
    if (parseInt(row.dataset.depth) >= 2) row.classList.add('collapsed');
  });
  updateVisibility();
  updateStats();
}

// ── Export ────────────────────────────────────────────────────────

function exportPNG() {
  if (CURRENT_VIEW !== 'table') { alert('Export is only available in Table view.'); return; }
  if (!SPANS.length) return;

  const table = document.getElementById('trace-table');
  if (!table || table.style.display === 'none') return;

  // Clone the table and inline computed styles so Chrome can rasterize it reliably.
  const clone = table.cloneNode(true);
  const srcList = table.querySelectorAll('*');
  const dstList = clone.querySelectorAll('*');
  const props = [
    'background','background-color','background-clip','color','font-family',
    'font-size','font-weight','border-bottom','border-right','border-top',
    'border-left','border-collapse','padding','text-align','vertical-align',
    'white-space','overflow','text-overflow','width','height','min-width',
    'max-width','display','line-height','margin-left','margin-right','gap',
    'flex','flex-shrink','flex-grow','align-items','justify-content',
    'letter-spacing','text-transform','border-radius','cursor','user-select',
    'border-bottom-color','border-bottom-style','border-bottom-width',
    'border-right-color','border-right-style','border-right-width',
    'box-sizing','outline','opacity'
  ];
  function copyStyles(src, dst) {
    const cs = getComputedStyle(src);
    for (let p = 0; p < props.length; p++) {
      dst.style[props[p]] = cs[props[p]];
    }
  }
  copyStyles(table, clone);
  for (let i = 0; i < srcList.length; i++) copyStyles(srcList[i], dstList[i]);

  const html = new XMLSerializer().serializeToString(clone);
  const doc = '<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml">\n<head>\n'
    + '<meta charset="UTF-8">\n'
    + '<style>html,body{margin:0;padding:0;background:#0d1117;overflow:hidden}</style>\n'
    + '</head>\n<body>\n'
    + html
    + '\n</body>\n</html>';

  const rect = table.getBoundingClientRect();
  const width = Math.max(1, Math.ceil(rect.width));
  const height = Math.max(1, Math.ceil(rect.height));
  const currentFile = document.getElementById('file-picker').value || 'trace';

  fetch('/api/export-image', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({html: doc, file: currentFile, width: width, height: height})
  }).then(function(r) { return r.json(); }).then(function(data) {
    if (data.ok) {
      alert('Saved to ' + data.path);
    } else {
      alert('Error: ' + (data.error || 'unknown'));
    }
  }).catch(function(err) {
    alert('Export failed: ' + err.message);
  });
}


// ── Visibility ─────────────────────────────────────────────────────

function updateVisibility() {
  var rows = document.querySelectorAll('#trace-body tr');
  var stack = [];
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var depth = parseInt(row.dataset.depth);
    while (stack.length > 0 && stack[stack.length - 1] >= depth) stack.pop();
    var hidden = stack.length > 0;
    row.classList.toggle('hidden', hidden);
    if (!hidden && row.classList.contains('collapsed')) stack.push(depth);
  }
}

function updateStats() {
  var total = document.querySelectorAll('#trace-body tr').length;
  var visible = document.querySelectorAll('#trace-body tr:not(.hidden)').length;
  document.getElementById('stats').textContent = visible + ' / ' + total + ' spans';
}

// ── Search ─────────────────────────────────────────────────────────

document.getElementById('search').addEventListener('input', function () {
  var q = this.value.toLowerCase().trim();
  if (!q) {
    updateVisibility();
    updateStats();
    return;
  }

  var rows = document.querySelectorAll('#trace-body tr');
  var matchStack = [];

  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var depth = parseInt(row.dataset.depth);

    while (matchStack.length > 0 && matchStack[matchStack.length - 1] >= depth) {
      matchStack.pop();
    }

    var span = SPANS[i];
    var nameMatch = span.op.toLowerCase().includes(q)
                 || span.description.toLowerCase().includes(q);

    if (matchStack.length > 0) {
      row.style.display = '';
      row.classList.remove('hidden');
      if (nameMatch) matchStack.push(depth);
    } else if (nameMatch) {
      row.style.display = '';
      row.classList.remove('hidden');
      matchStack.push(depth);
    } else {
      row.style.display = 'none';
    }
  }

  var vis = 0;
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].style.display !== 'none') vis++;
  }
  document.getElementById('stats').textContent = vis + ' / ' + SPANS.length + ' spans (filtered)';
});

// ── Keyboard ───────────────────────────────────────────────────────

document.addEventListener('keydown', function (e) {
  if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
    e.preventDefault();
    document.getElementById('search').focus();
  }
});

// ── Go ─────────────────────────────────────────────────────────────

init();
loadColors();
// Initialize toggles
setMode('ttft');
setView('table');
</script>
</body>
</html>"""


# ── HTTP server ──────────────────────────────────────────────────────────


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/files':
            self._send_json(self._list_files())
        elif parsed.path == '/api/trace':
            self._send_trace(parsed)
        elif parsed.path == '/api/colors':
            self._send_json(load_colors())
        else:
            self._send_html()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/api/colors':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                colors = json.loads(body)
                saved = save_colors(colors)
                self._send_json({'ok': True, 'colors': saved})
            except (json.JSONDecodeError, ValueError) as e:
                self._send_json({'error': str(e)}, 400)
        elif parsed.path == '/api/export-image':
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                html_content = data.get('html', '')
                file_name = data.get('file', 'trace')
                width = int(data.get('width', 1200))
                height = int(data.get('height', 1600))
                stem = Path(file_name).stem
                ts = datetime.now().strftime('%Y%m%dT%H%M%S')
                SVG_DIR.mkdir(parents=True, exist_ok=True)
                out_path = SVG_DIR / f'{stem}-{ts}.png'
                with tempfile.NamedTemporaryFile('w', delete=False, suffix='.html', encoding='utf-8') as tmp:
                    tmp.write(html_content)
                    tmp_path = tmp.name
                try:
                    chrome = '/usr/bin/google-chrome'
                    cmd = [
                        chrome,
                        '--headless',
                        '--no-sandbox',
                        '--disable-gpu',
                        f'--screenshot={out_path}',
                        f'--window-size={width},{height}',
                        f'file://{tmp_path}',
                    ]
                    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                    if proc.returncode != 0:
                        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f'chrome exited {proc.returncode}')
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                self._send_json({'ok': True, 'path': str(out_path)})
            except (json.JSONDecodeError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as e:
                self._send_json({'error': str(e)}, 400)
        elif parsed.path == '/api/export-svg':
            self._send_json({'error': 'deprecated'}, 410)
        else:
            self._send_json({'error': 'not found'}, 404)

    # ── helpers ──

    def _list_files(self):
        files = []
        for p in sorted(SENTRY_DIR.glob('*.json')):
            files.append({
                'name': p.name,
                'size': p.stat().st_size,
            })
        return files

    def _send_trace(self, parsed):
        qs = urllib.parse.parse_qs(parsed.query)
        file = (qs.get('file') or [None])[0]
        if not file:
            self._send_json({'error': 'missing file param'}, 400)
            return

        path = (SENTRY_DIR / file).resolve()
        try:
            path.relative_to(SENTRY_DIR.resolve())
        except ValueError:
            self._send_json({'error': 'invalid file path'}, 403)
            return

        if not path.is_file():
            self._send_json({'error': 'file not found'}, 404)
            return

        with open(path) as f:
            data = json.load(f)

        spans = data.get('spans', [])
        flat = build_flat_tree(spans)
        qs = urllib.parse.parse_qs(parsed.query)
        mode = (qs.get('mode') or ['full'])[0]

        ttft_time = None
        if mode == 'ttft':
            flat, ttft_time = apply_ttft_mode(flat)

        flat = filter_spans(flat)
        flat = group_db_spans(flat)

        total_dur = data['timestamp'] - data['start_timestamp']
        if ttft_time is not None:
            total_dur = ttft_time - data['start_timestamp']

        result = {
            'trace_id': data.get('event_id'),
            'transaction': data.get('transaction'),
            'duration_ms': round(total_dur * 1000, 1),
            'total_spans': len(flat),
            'spans': flat,
            'filename': file,
            'mode': mode,
        }
        self._send_json(result)

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"  {args[0]} {args[1]} {args[2]}")


# ── entrypoint ──────────────────────────────────────────────────────────


if __name__ == '__main__':
    print(f"  ──────────────────────────────────────────")
    print(f"   Load Testing Formatter")
    print(f"   http://localhost:{PORT}")
    print(f"  ──────────────────────────────────────────")
    print(f"   Files: {SENTRY_DIR}")
    print(f"   Ctrl+C to stop")
    print()
    server = HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopping…")
        server.shutdown()
