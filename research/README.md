# research/

One-off experiments and evidence from building Lethe — kept for **provenance**, not part of the running app.
Nothing here is imported by the product. These document the rigor behind the design — most notably the
finding that, tested at temperature 0, RAG-vs-graph and the feedback/`improve` leg didn't hold up, while
**`forget` did**.

Run any of them from the **repo root**, e.g.:

```bash
./.venv/bin/python research/rag_vs_graph_temp0.py
```

## ⭐ Headline evidence — `forget_correctness_benchmark.py`

A **non-circular** before/after measurement that forgetting improves answer **correctness**. Over a scaled
corpus (~18 docs / 12 systems, **3** decommissioned systems with planted stale cross-references) it asks
on-call questions, forgets the dead systems, and re-asks — and an **independent model (Gemini), blind to
before/after, scores each answer's correctness 0–2** against the known-correct *current* guidance.

```bash
GEMINI_TEST_KEY=... ./.venv/bin/python research/forget_correctness_benchmark.py   # DESTRUCTIVE on the bench_scale dataset only
./.venv/bin/python scripts/reset_demo.py                                           # restore golden after
```

It scores **correctness, not word-presence** — control questions that must stay correct prove the forget is
surgical. Results are saved to `forget_correctness_results.json` (and feed the landing's proof section via
`/evidence`). **It is LLM-quota-heavy** (an 18-doc cognify + ~40 model calls) → a run-once-capture artifact;
the free tiers throttle it, so it has per-call timeouts + throttling built in.

### Superseded — `forget_benchmark.py` (kept for provenance; **do not cite its number**)
The original benchmark scored a "leak" as the substring `"legacy-cache"` in the answer. That is **circular**:
deleting the legacy-cache docs trivially removes the word, so its `4/4 → 0/4` measured "deletion deletes a
word," not that advice improved. The graph-diff it reports (14 nodes / 26 edges removed) is real, but the
headline metric was retired in favour of `forget_correctness_benchmark.py` above.

## `graph_model_probe.py` — a negative result that corrects an assumption

Tests whether a **custom, type-constrained `graph_model`** (canonical entity types via an Enum) gives
cleaner, deduplicated extraction than Cognee's default free-text-type schema — the upstream fix for the
"Cognee Lint" dedup gap. A/B over the same wiki in an **isolated temp data dir** (golden untouched).

**Result (2026-06-26):** default schema → 61 nodes / 16 types (incl. the `service` vs `services` dup);
custom Enum schema → **0 nodes (empty graph)** — under Groq llama-3.3-70b **AND** under a strong model
(`gemma4:31b-cloud` via Ollama, which emits clean JSON). **A first read blamed the model; the strong-model
re-run disproved that** — the custom graph is empty regardless of model strength, while the default schema
builds fine with the same models. So the blocker is the **custom-schema ↔ cognee-1.1.3 interaction**
(likely the Enum-typed field / how cognify handles a custom `graph_model`), **not** model capability.
→ Dedup-by-construction via `graph_model` is a **dead end on 1.1.3** and was **not shipped**. Pass
`PROBE_LLM_MODEL=<ollama-model>` to re-run against a different model. (Separate question — does a strong
model unblock cognee **1.2.x**'s DEFAULT-schema extraction — is tested in an isolated 1.2.x venv.)

| Area | Scripts |
|---|---|
| **⭐ Measured forget evidence** | **`forget_benchmark.py`** → `forget_benchmark_results.json` |
| Custom graph_model A/B (negative result) | `graph_model_probe.py` → `graph_model_probe_results.json` |
| RAG-vs-graph / answer quality | `rag_vs_graph_temp0.py`, `ab_compare.py`, `ab_hard.py`, `agg_test.py` |
| Feedback / `improve` leg probes | `mechanism_a_gating.py`, `mechanism_a_realistic.py`, `feedback_on_ambiguous.py` |
| Forget validation / behavioral | `validate_n20.py`, `forget_smoketest.py` |
| Memory-poisoning probe | `poisoning_probe.py` |
| Early prototypes / debugging | `decide.py`, `debug_cognify.py`, `incident_detective.py` |
