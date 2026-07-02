# Lethe — one-command judge path.
#   make install   → create venv + install deps
#   make build     → one-time cold build (needs an LLM key in .env; ~1 min)
#   make run       → start the app on :8077
#   make reset     → restore the golden demo (instant, zero quota)
#   make demo      → install + run (assumes .env + a prior build exist)
PY = ./.venv/bin/python

.PHONY: install build run reset demo mcp

install:
	uv venv && uv pip install -r requirements.txt

build:
	$(PY) scripts/setup.py

run:
	$(PY) -m uvicorn app:app --port 8077

reset:
	$(PY) scripts/reset_demo.py

demo: install run

mcp:
	$(PY) scripts/try_mcp.py
