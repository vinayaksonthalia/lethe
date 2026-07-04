# Deploying Lethe

Lethe ships as a single Docker image with the demo knowledge graph **baked in at build time** — the container serves instantly, needs no database setup, and a restart always returns it to a known-good state. One LLM key is the only external dependency (embeddings run locally, $0).

## The three modes

| Mode | Env | Who it's for |
|---|---|---|
| **Local** (default) | *(nothing)* | Your machine. Loopback traffic is fully open; remote callers are refused (fail-closed). |
| **Private deploy** | `LETHE_AUTH_TOKEN=<secret>` | A team box. Every API route requires `Authorization: Bearer <secret>`; the web UI prompts once and remembers it. |
| **Hosted demo** | `LETHE_PUBLIC_DEMO=1` | A public URL. Anonymous access, per-IP rate limiting, and the model config is locked so a visitor can't rewire the shared instance. |

Always pair a non-local deploy with `LETHE_RATE_LIMIT="20/60"` (20 requests per 60s per IP on the mutating/quota-spending routes).

## Docker (any host)

```bash
docker build -t lethe .
docker run -p 8077:8077 \
  -e LLM_API_KEY=<your-key> \
  -e LLM_MODEL="openai/zai-glm-4.7" \
  -e LLM_ENDPOINT="https://api.cerebras.ai/v1" \
  -e LETHE_PUBLIC_DEMO=1 -e LETHE_RATE_LIMIT=20/60 \
  lethe
```

Any OpenAI-compatible provider works (`LLM_MODEL` uses litellm's `openai/<model>` form for custom endpoints). The build runs `scripts/reset_demo.py` (bakes the golden graph — a pure file copy, zero LLM quota) and `scripts/fix_db_paths.py` (rewrites the absolute vector-store path Cognee records per dataset — without this, a relocated snapshot quietly serves an empty index; found the hard way).

## Hugging Face Space (how the live demo runs)

The [live demo](https://vinayaksonthalia-lethe.hf.space) is a free **Docker Space** (16 GB RAM):

1. Create a Space → SDK **Docker** → push this repo's files (the Space needs `README.md` front-matter with `sdk: docker` and `app_port: 8077`).
2. Settings → **Variables**: `LLM_MODEL`, `LLM_ENDPOINT`, `LETHE_PUBLIC_DEMO=1`, `LETHE_RATE_LIMIT=20/60`. **Secrets**: `LLM_API_KEY`.
3. A **restart rebuilds from the image → instant reset to the golden graph** (~40s). Between restarts, the in-app **Re-arm** button recovers the demo after a destructive forget.
4. Free Spaces sleep after ~48h idle — this repo's `.github/workflows/keepalive.yml` pings `/health` every 10 minutes once the `LETHE_URL` repo variable is set.

## Render (blueprint included)

`render.yaml` defines the service (Docker runtime, `/health` check). **Honest note:** Render's free 512 MB tier OOM-kills the query path under this stack even after memory tuning (`MALLOC_ARENA_MAX=2`, `OMP_NUM_THREADS=1` — both baked into the image; measured peak ~405 MB locally, their accounting is stricter). Use a ≥1 GB plan on Render, or the free HF Space above.

## Sizing & notes

- **RAM:** ~350 MB idle, ~500 MB peak per query (kuzu + LanceDB + local ONNX embeddings). 1 GB is comfortable; 512 MB is not.
- **Persistence:** none required. The golden graph lives in the image; user-created workspaces are ephemeral on a hosted demo (the UI says so) — mount a volume over `/data` if you want them durable.
- **Model swap:** set the three `LLM_*` env vars; on a private deploy you can also swap at runtime in Settings (validated, auto-reverts on a bad key). Locked in hosted-demo mode.
