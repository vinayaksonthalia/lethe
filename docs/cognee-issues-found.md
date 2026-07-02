# Cognee papercuts found while building Incident Detective

Real friction hit during development. These are **candidates** to verify against current `dev`
before raising anything — and per hackathon rules, get the issue assigned first, no typo-spam.

1. **No native `groq` LLM provider, but a `groq` extra exists.** `pyproject.toml` / docs list a
   `groq` install extra, yet `LLMProvider` enum only has openai/ollama/anthropic/custom/gemini/
   mistral/bedrock/azure. Setting `LLM_PROVIDER="groq"` mis-routes and hangs. Workaround: use
   `LLM_PROVIDER="custom"` + `LLM_ENDPOINT="https://api.groq.com/openai/v1"` + `LLM_MODEL="openai/<model>"`.
   → Candidate: add a first-class `groq` provider, or document the custom route. (Verify on dev.)

2. **Pre-flight `test_llm_connection` hangs ~30s on some providers.** It calls
   `acreate_structured_output(..., response_model=str)`; with a mis-set provider it retries until a
   30s timeout, with a confusing error. `COGNEE_SKIP_CONNECTION_TEST=true` bypasses it (works when
   exported). → Candidate: fail faster / clearer message; maybe make the test provider-agnostic.

3. **`DATA_ROOT_DIRECTORY` must be absolute, but it's only caught at runtime** via a pydantic
   `ValidationError` deep in a stack trace. → Candidate: validate + clear message at config load.

4. **`load_dotenv(override=True)` in `cognee/__init__` resolves `.env` from the install location,
   not the CWD.** Two projects sharing one editable install fight over one `.env` (we got a
   half-Gemini/half-Groq Frankenstein config). → Candidate: honor a `COGNEE_ENV_FILE` override or
   prefer CWD. (This is why each project should get its OWN venv.)

5. **LanceDB/Kuzu + paths with spaces** in `DATA_ROOT_DIRECTORY` silently misbehaved (no data
   stored, later `NoDataError`). → Candidate: validate/escape paths, or warn on spaces. (Verify.)

6. **FEATURE (not bug — frame it this way for merge odds): add a completeness/traversal search
   type that complements `GRAPH_COMPLETION`.** Evidence: for "list every service impacted if
   eu-west-redis fails", an explicit BFS over `get_graph_data()` returned the COMPLETE set (5/5),
   while `GRAPH_COMPLETION` returned 4/5 (silent miss, like RAG) because it's relevance-ranked
   (vector seed + local expansion), not exhaustive. Filing as "GRAPH_COMPLETION is broken" invites
   "working as designed — it's relevance-ranked"; filing as "a reachability/blast-radius search
   mode for completeness queries" is a mergeable feature. Strong PR candidate.

8. **`forget()` doesn't invalidate the query CACHE for the forgotten data** (discovered through real
   product use — the best kind of lead). With `caching=True` (default), `forget(data_id, dataset)` returns
   `status: success` and removes the data from the graph, BUT a subsequent identical query returns the STALE
   CACHED answer (still describing the forgotten system). Only with `CACHING=false` does the post-forget
   answer reflect the removal. → Candidate: `forget()`/`delete()` should evict cached answers referencing
   the forgotten data (or docs should warn). Frame as bug-or-feature-request; it's real. Strong PR lead.

7. **`cognify` is extremely token-hungry** (~6k tokens/doc; 20 tiny docs ≈ 125k tokens), which
   blows past free-tier daily caps fast (Gemini free = 20 req/day on 2.5-flash; Groq = 100k tok/day).
   → Candidate: a lighter/no-summary cognify mode + a token-cost estimate before running. (Verify.)

9. **Default GRAPH_COMPLETION prompt ("...Be as brief as possible.") returns bare-fragment answers for
   natural/under-specified questions.** "If auth-service latency is high, what should I check?" returned just
   `legacy-cache` (the bare node name) repeatedly at temp 0 — even though `only_context=True` showed the FULL
   runbook was retrieved, so retrieval was fine; the default prompt (`answer_simple_question.txt`) forced
   terseness. Passing a custom `system_prompt` to `search()` fixes it (concise full answer). → NOT a strong PR
   (cognee EXPOSES `system_prompt` precisely to override this — working as designed, not a bug). Keep as a
   BLOG line ("the bare-fragment answer wasn't my graph, it was the default 'be brief' prompt"). At most a
   docs nudge to surface `system_prompt` in the quickstart.
