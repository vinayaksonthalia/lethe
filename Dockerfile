# Lethe — self-contained demo image. The GOLDEN graph is baked at build time
# (reset_demo.py file-copy; zero cognify, zero LLM quota) so the container serves
# instantly. Bring your own key at runtime:
#
#   docker build -t lethe .
#   docker run -p 8077:8077 \
#     -e LLM_API_KEY=<key> \
#     -e LLM_MODEL="openai/gemini-2.5-flash" \
#     -e LLM_ENDPOINT="https://generativelanguage.googleapis.com/v1beta/openai/" \
#     -e LETHE_AUTH_TOKEN=<token> -e LETHE_RATE_LIMIT=20/60 \
#     lethe
#
# LETHE_AUTH_TOKEN + LETHE_RATE_LIMIT are REQUIRED for any public deploy
# (single-tenant guard — see README security section).
FROM python:3.12-slim

WORKDIR /app

# Non-secret runtime config (mirrors .env.example; override any of these at run time).
# The LLM key is NEVER baked — pass LLM_API_KEY (and provider/model/endpoint) via env.
ENV PYTHONUNBUFFERED=1 \
    DATA_ROOT_DIRECTORY=/data/data \
    SYSTEM_ROOT_DIRECTORY=/data/system \
    LLM_PROVIDER=custom \
    LLM_MODEL=openai/llama-3.3-70b-versatile \
    LLM_ENDPOINT=https://api.groq.com/openai/v1 \
    LLM_ARGS='{"temperature": 0.0}' \
    COGNEE_SKIP_CONNECTION_TEST=true \
    EMBEDDING_PROVIDER=fastembed \
    EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 \
    EMBEDDING_DIMENSIONS=384 \
    DB_PROVIDER=sqlite \
    VECTOR_DB_PROVIDER=lancedb \
    GRAPH_DATABASE_PROVIDER=kuzu \
    LITELLM_LOG=ERROR \
    MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bake the golden graph (pure file copy into /data), repair the absolute DB
# paths cognee stored on the build machine (see scripts/fix_db_paths.py), and
# pre-download the local embedding model so first query never waits on a download.
RUN python scripts/reset_demo.py && \
    python scripts/fix_db_paths.py && \
    python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

EXPOSE 8077
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8077}
