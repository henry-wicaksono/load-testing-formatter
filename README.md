# Load Testing Formatter

Browser-based trace table viewer for Sentry JSON exports. Pick a trace file, inspect spans in a compact table, and export a PNG for use in docs.

## Prerequisites

- Python ≥ 3.10
- [uv](https://docs.astral.sh/uv/) (package manager, install once: `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Google Chrome (required for PNG export; the app will still run without it, but the Export button won't work)

## Quick start

```bash
git clone https://github.com/henry-wicaksono/load-testing-formatter.git
cd load-testing-formatter

# Launch
make start
```

Or without `make`:

```bash
uv run server.py
```

Open **http://localhost:8765** in your browser.

## Usage

1. **Place trace files** — Drop Sentry JSON event exports into the `sentry/` folder.
2. **Pick a file** — Choose it from the dropdown in the left sidebar.
3. **Choose mode** — `Full` shows the entire trace; `TTFT` (time-to-first-token) trims the trace at the first response.
4. **Choose view** — `Trace` shows a collapsible tree; `Table` shows a flat table with separate duration columns per depth level.
5. **Depth** — In Table view, select how many levels to show.
6. **Spread** — A single slider controls how much the depth background colors contrast (0 = all dark, 100 = maximum contrast, persisted to `table-colors.json`).
7. **Export** — Click `Export PNG` to save the current table view as a PNG in `results/`.

## File layout

```
sentry/           ← put your Sentry JSON files here
results/          ← exported PNGs (gitignored)
server.py         ← web server + UI (stdlib only — zero dependencies)
pyproject.toml
Makefile
```

## Sentry JSON format

The app expects a JSON object with a `spans` array. Each span should include:

- `span_id` / `parent_span_id` — hierarchy
- `op` — operation name (displayed as the primary label)
- `description` — optional detail (shown when different from `op`)
- `start_timestamp` / `timestamp` — timing
- `exclusive_time` — self time (microseconds)

## Export format

The `Export PNG` button serialises the table DOM with all computed styles inlined, saves it as a temporary HTML file, and renders it via headless Google Chrome (`/usr/bin/google-chrome`) into a PNG saved under `results/<tracefile>-<timestamp>.png`.
