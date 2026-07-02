# Tests

Focused regression suite over Lethe's load-bearing behaviors + security guards. Run from the repo root:

```bash
./.venv/bin/python -m pytest          # hermetic tier — NO API keys, NO real cognee/DB, NO :8077 lock
```

All tests mock cognee/LLM/DB and seed in-memory `app.S` state, so they never touch the golden demo data or the running server. (Importing `app` does `import cognee`, which is fine — cognee is installed.)

The one opt-in end-to-end test (the forget hero) does real cognee/LLM work on a throwaway dataset (never `main_dataset`). Stop the :8077 server first, ensure `.env` creds, then:

```bash
RUN_LIVE=1 ./.venv/bin/python -m pytest -m live
```
