.PHONY: start setup restart
start:
	uv run server.py

setup:
	uv sync
	uv run playwright install chromium

restart:
	lsof -ti:8765 | xargs kill -9 2>/dev/null; sleep 1; uv run server.py &
