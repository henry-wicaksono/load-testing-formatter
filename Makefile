.PHONY: start setup
start:
	uv run server.py

setup:
	uv sync
	uv run playwright install chromium
