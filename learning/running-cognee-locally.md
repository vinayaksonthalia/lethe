# Running Cognee locally (latest)

Latest release: **v1.2.1**. Commands below are from Cognee's official repo (`topoteretes/cognee`) — I haven't run the Docker flow on this machine, so treat them as the documented path and check the repo README if anything has moved.

## Option A — just the library (what Lethe uses today)
```bash
uv pip install cognee        # or: pip install cognee   (gets latest, currently 1.2.1)
```
Then in Python: `cognee.add(text)` → `cognee.cognify()` → `cognee.search(...)` → `cognee.forget(...)`.
**Note:** Lethe is pinned to **1.1.3** on purpose. We **TESTED 1.2.1 in isolation (2026-06-26)** — it's a **breaking upgrade with an unresolved extraction wall**, not a drop-in:
1. config now requires `EMBEDDING_DIMENSIONS` (e.g. `384` for bge-small-en-v1.5);
2. **fastembed is no longer bundled** — install the extra: `uv pip install 'cognee[fastembed]'`;
3. a **new DB-migration subsystem** aborts on a fresh DB ("Write aborted… would mix id schemes");
4. **`forget()` now requires `dataset`/`dataset_id`** alongside `data_id`.

cognify's `SummarizedContent` structured extraction failed `InstructorRetryException` with both Groq llama-3.3-70b AND `gemma4:31b-cloud` (Ollama) → empty graph. **BUT it WORKS with `gemini-2.5-flash`** (via its OpenAI-compat endpoint): cognee 1.2.1 builds a real graph and the **full forget hero flips correctly** (verified — see `scratchpad/test_cognee12.py` with `GEMINI_TEST_KEY`). So the blocker is the **model/instructor path, not 1.2.x** — a native structured-output model (Gemini) clears it. To actually move Lethe to 1.2.x you'd: set `EMBEDDING_DIMENSIONS`, `uv pip install 'cognee[fastembed]'`, point the LLM at Gemini (note free-tier rate limits for a live demo), pass `dataset=` to `forget()`, **rebuild the golden graph** (1.2.x uses a different id/DB scheme — can't reuse the 1.1.3 snapshot), and **re-verify every feature**. It's a real migration, not a pin bump → the demo **stays on 1.1.3** unless we do that work in the build window.

## Option B — the full platform locally (the dashboard you saw at platform.cognee.ai)
Clone the repo, add a `.env` (LLM key etc.), then Docker Compose:
```bash
git clone https://github.com/topoteretes/cognee && cd cognee
cp .env.template .env          # fill LLM_API_KEY etc.
docker compose up                       # API server  → http://localhost:8000
docker compose --profile ui up          # + frontend  → http://localhost:3000   (the dashboard UI)
docker compose --profile mcp up         # + MCP server→ http://localhost:8001
docker compose --profile postgres up    # + Postgres/PGVector backend
docker compose --profile neo4j up       # + Neo4j graph backend
```
Or the prebuilt image (no build):
```bash
docker run --env-file ./.env -p 8000:8000 --rm -it cognee/cognee:main
docker run -e TRANSPORT_MODE=http --env-file ./.env -p 8000:8000 --rm -it cognee/cognee-mcp:main
```
Verify: `curl localhost:8000/health`, open `localhost:3000`.

## Fully offline (local LLM + local embeddings)
Embeddings are already local (fastembed `BAAI/bge-small-en-v1.5`). For the LLM, use Ollama:
```
LLM_PROVIDER="ollama"
LLM_MODEL="gemma4:e4b"             # what we verified locally; qwen2.5:14b/7b recommended for INGEST
LLM_ENDPOINT="http://localhost:11434/v1"
LLM_API_KEY="ollama"
LLM_INSTRUCTOR_MODE="json_schema_mode"          # cognify needs structured/JSON output
HUGGINGFACE_TOKENIZER="BAAI/bge-small-en-v1.5"  # REQUIRED on 1.1.3's ollama path (see gotcha)
```
**Gotcha (found 2026-06-23):** on cognee 1.1.3 the *ollama* import path eagerly validates `LLMConfig` and throws `"set some but not all of the required environment variables for embeddings … Missing: ['HUGGINGFACE_TOKENIZER']"` unless `HUGGINGFACE_TOKENIZER` is also set (the cloud/`custom` path doesn't hit this). Set it to your embedding model id.

**✅ Verified offline (Lethe, 2026-06-23):** switched `.env` to `ollama` + `gemma4:e4b` (already installed) and ran `try_local_model.py` — cognee resolved provider=`ollama`, model=`gemma4:e4b`, endpoint=`localhost:11434`, and `/ask` answered the hero question correctly over the **golden graph with no internet** (the `forget` BEFORE-state held: "flush and resize the legacy-cache…"). The build does NOT affect answer quality, so you can keep the cloud-built golden graph and answer over it with a local model. `try_local_model.py` is a non-destructive harness (swap-and-restore `.env`) to reproduce.
**Model recommendation:** cognify's extraction is the demanding step (it must return schema-valid JSON). Prefer a model strong at structured output — **`qwen2.5:14b`** or **`qwen2.5:7b`** tend to do JSON well; `llama3.1:8b-instruct` works but is weaker. Small models can produce a thin/empty graph (same failure class we hit on 1.2.x). Bigger = better extraction. Completion (answering) is easy for any decent model. Pull with `ollama pull qwen2.5:14b`.

## For Lethe specifically (this project)
- Self-hosted (open track): what we run now — local DBs (Kùzu + LanceDB + SQLite), OpenRouter LLM, pinned 1.1.3.
- Cloud track: point the same app at Cognee Cloud (their managed LLM does structured output, so the 1.2.x extraction issue won't bite there); re-verify the forget beat on whatever it runs.
