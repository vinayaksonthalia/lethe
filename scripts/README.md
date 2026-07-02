# scripts/

Developer utilities and tests for Lethe. **Run them from the repo root**, e.g.:

```bash
./.venv/bin/python scripts/verify_clean.py
```

| Script | What it does |
|---|---|
| `verify_clean.py` | Runs the hero beat before/after `forget`; prints answer **strings** to read (never substring-scores). |
| `smoke_web.py` | In-process smoke test of the `/ask` + `/forget` routes (no server/port). |
| `qa_harness.py` | Ingests larger/varied docs into a temp workspace to stress-test on real data. |
| `capture_screens.py` | Captures the README screenshots (needs the app on `:8077` + playwright chromium). |
| `try_mcp.py` | One-command MCP test — spawns the stdio server, lists + calls every tool. |
| `try_local_model.py` | Proves Lethe answers fully offline on a local Ollama model (run via the `.env` swap harness). |

Scripts that import the core (`incident_brain` / `app`) add the repo root to `sys.path`, so they work from `scripts/` as long as you launch them from the repo root.
