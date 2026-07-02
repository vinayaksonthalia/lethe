# 07 · Cognee capability audit — what it can do, what we use, what we're leaving on the table

_Researched 2026-06-24 against **the docs** (docs.cognee.ai — llms-core / llms-api / llms-mcp / llms-cloud shards + guides) **and ground-truthed against our installed `cognee 1.1.3`** by probing the actual API (`inspect.signature`, the `SearchType` enum, the startup banner). Where the docs and our installed package disagreed, **the installed package wins** — every "available to us" claim below was verified in code, not paraphrased from docs._

> **Why this doc exists:** "Best Use of Cognee" is a judged criterion, scored on the **depth** of how we use the memory lifecycle. We were using a sliver of the API. This maps the whole surface so we can choose what to add — and it corrects two long-standing wrong beliefs in our own notes.

---

## 0. The headline corrections (read first)

**Correction 1 — our pinned `1.1.3` already IS "Cognee 1.0+".** The startup banner prints verbatim:

> _"Cognee 1.0 changes: New API — remember/recall/forget/improve (V1 add/cognify/search still work). Session memory enabled by default (CACHING=false to disable). Multi-user access control on by default (ENABLE_BACKEND_ACCESS_CONTROL=false to disable). Agents (@cognee.agent) auto-verified on registration."_

So `remember` / `recall` / `forget` / `improve` / `memify`, the new `SearchType`s (COT, CONTEXT_EXTENSION, DECOMPOSITION, TEMPORAL…), `temporal_cognify`, `graph_model`, NodeSet tagging, `only_context`, `feedback_influence` — **are all in our build right now.** Our prior notes that called these "almost certainly post-1.1.3" were wrong. We have them.

**Correction 2 — the 1.2.x pin is about ONE thing, not the API.** 1.2.x doesn't remove anything we use. The only failure was that 1.2.x's structured graph **build** returns an empty graph because `llama-3.3-70b` can't reliably emit the open-ended structured JSON that `cognify` asks for by default. That is **fixable from inside 1.1.3** (a tight custom `graph_model` shrinks the JSON target — see §5.3) — which means it may also be the key to *unblocking* the upgrade later. The pin is a model-vs-schema problem, not a "newer Cognee is scary" problem.

---

## 1. Ground truth — the API surface actually present in our `cognee 1.1.3`

Probed live (`./.venv/bin/python -c "import cognee, inspect; …"`):

**Top-level ops present:** `add`, `cognify`, `search`, `memify`, `forget`, `prune`, `delete`, `datasets.*`, `remember`, `recall`, `improve`, `config`, `visualize_graph`. (`add_data_points` is NOT top-level — it lives in a task/pipeline module.)

**`SearchType` enum (exactly these 16, nothing else):**
`SUMMARIES`, `CHUNKS`, `RAG_COMPLETION`, `TRIPLET_COMPLETION`, `GRAPH_COMPLETION`, `GRAPH_COMPLETION_DECOMPOSITION`, `GRAPH_SUMMARY_COMPLETION`, `CYPHER`, `NATURAL_LANGUAGE`, `GRAPH_COMPLETION_COT`, `GRAPH_COMPLETION_CONTEXT_EXTENSION`, `FEELING_LUCKY`, `TEMPORAL`, `CODING_RULES`, `CHUNKS_LEXICAL`, `AGENTIC_COMPLETION`.
> ⚠️ **NOT in our build** (the docs/blogs mention them; we don't have them): `INSIGHTS`, `HYBRID_COMPLETION`, `CODE` (it's `CODING_RULES` here), and there is **no `FEEDBACK` search type**. Don't build on these.

**`cognee.search(...)` — the params we actually have:**
`query_text, query_type=GRAPH_COMPLETION, user, datasets, dataset_ids, system_prompt_path, system_prompt, top_k=10, node_type=NodeSet, node_name, node_name_filter_operator="OR", only_context=False, session_id, wide_search_top_k=100, triplet_distance_penalty=6.5, feedback_influence=0.0, verbose=False, retriever_specific_config, neighborhood_depth, neighborhood_seed_top_k, skills, tools, max_iter`.
> ⚠️ The docs/blogs reference `save_interaction` and `include_references` — **those kwargs are NOT in our signature.** Provenance comes from `only_context` / `verbose` here, not an `include_references` flag.

**`cognee.cognify(...)`:** `datasets, user, graph_model=KnowledgeGraph, chunker=TextChunker, chunk_size, chunks_per_batch, config(ontology), vector_db_config, graph_db_config, run_in_background, incremental_loading=True, custom_prompt, temporal_cognify=False, data_per_batch=20`.

**`cognee.add(...)`:** `data, dataset_name, user, node_set: list[str], vector_db_config, graph_db_config, dataset_id, preferred_loaders, incremental_loading, data_per_batch, importance_weight=0.5, run_in_background`.
> **NodeSet tagging is done at `add` time** (`node_set=[...]`), not at `cognify`. There's also a per-item **`importance_weight`** (0.5 default).

**`cognee.forget(*, data_id, dataset, dataset_id, everything, memory_only, user) -> dict`.**
> Scopes: one item (`data_id`+`dataset`), a whole `dataset`, `everything`, or `memory_only` (wipe derived memory, keep source files for reprocessing). **There is NO NodeSet/`node_name` scope on forget** — you forget by id / dataset / everything. Our per-`data_id` forget is the correct mechanism; "forget a whole node-set in one call" is not a thing in 1.1.3.

**`cognee.memify(extraction_tasks, enrichment_tasks, data, dataset, node_type=NodeSet, node_name, …)`**, **`recall(…, auto_route=True, scope, only_context, feedback_influence, neighborhood_depth, …)`**, **`improve(dataset, node_name, session_ids, build_global_context_index, …)`**, **`remember(data, dataset_name, session_id, custom_prompt, self_improvement=True, …)`** — all present.

---

## 2. How Lethe uses Cognee TODAY (the honest baseline)

From `incident_brain.py`:

| Line | Call | Notes |
|---|---|---|
| `incident_brain.py:47` | `cognee.add(text)` | no `node_set`, no `importance_weight` |
| `incident_brain.py:51` | `cognee.cognify()` | **bare** — default `KnowledgeGraph` schema, no `custom_prompt`, no `graph_model`, no `temporal_cognify` |
| `incident_brain.py:123` | `cognee.search(query_text, query_type=GRAPH_COMPLETION, system_prompt=TRIAGE_PROMPT, **kw)` | **one search type only**; no `only_context`, no node filtering, no multi-hop |
| `incident_brain.py:139` | `cognee.forget(data_id=uid, dataset=dataset)` | ✅ we use the **unified forget** (per doc id) — correct |
| `incident_brain.py:145` | `cognee.prune.prune_data()` + `prune_system(metadata=True)` | full reset path |

(`app.py` adds `dataset_name=` for workspace isolation, and reads the graph via `get_graph_engine().get_graph_data()` for the viz.)

**In one line:** we use `add → cognify → GRAPH_COMPLETION search → forget`, all with defaults. That's the core loop done well — but it's ~20% of what the installed library offers.

---

## 3. The gap, ranked (everything verified-present in 1.1.3 unless noted)

| # | Capability | Available? | Effort | Lethe value | Verdict |
|---|---|---|---|---|---|
| 1 | ~~Multi-search-type routing (COT / CONTEXT_EXTENSION)~~ | ✅ in enum | — | none — **probed & killed** (114s for no gain, see §4.1) | **❌ DON'T** (TEMPORAL still open, needs rebuild) |
| 2 | **`only_context=True`** evidence panel | ✅ | LOW | MED–HIGH — "show the evidence" w/ no LLM call | **PROBE then maybe** |
| 3 | **Custom `graph_model`** (tight schema + `identity_fields`) | ✅ | LOW–MED | HIGH — extraction reliability + de-dup + **de-risks the 1.2.x upgrade** | **PROBE — highest strategic value** |
| 4 | **NodeSet tagging** via `add(node_set=[sys])` + filtered recall | ✅ | LOW | MED — native per-system grouping; cleaner than our ledger for scoped recall | **CONSIDER** |
| 5 | **MCP server** (remember/recall/forget over MCP) | ✅ (separate pkg) | MED (~1 day) | HIGH — biggest "depth of Cognee" signal; hero forget callable in Claude/Cursor | **STRONG CANDIDATE** |
| 6 | **`@cognee.agent_memory` decorator** | ✅ (docs) | LOW–MED | MED — idiomatic-cognee way to express our recall+prompt path | **CONSIDER** |
| 7 | **`feedback_influence` / `improve` / `self_improvement`** loop | ✅ | LOW | LOW–MED — BUT in tension with our anti-accumulation thesis (see §6) | **DELIBERATELY DE-EMPHASIZE** |
| 8 | **DeepEval / built-in eval** retrieval-quality number | ✅ (integration) | MED | MED — cheap Technical-Excellence credibility | **OPTIONAL** |
| 9 | **`memify` entity consolidation** | ✅ | LOW call / LLM cost | MED — merges fragmented entity descriptions | **OPTIONAL, post-ingest** |
| 10 | **Ontology** (`ontology_file`, OWL/RDF) | ✅ | MED–HIGH | MED — hard type-validation if we have a fixed service catalog | **SKIP for now** |
| 11 | **Cognee Cloud** (managed, connectors, teams) | n/a (hosted) | HIGH | LOW for our (self-hosted) track | **SKIP — at most a `cognee.serve()` "also works managed" beat** |
| — | `skills` / `tools` / `max_iter` agentic search; `INSIGHTS`/`HYBRID` | ⚠️ params exist but **undocumented** / type absent | — | unknown | **DON'T build the demo on these** |

---

## 4. The high-value opportunities, with code shape

### 4.1 Multi-search-type routing — biggest bang, lowest effort (DO)
We call exactly one `SearchType`. The library ships several that map cleanly onto on-call question *shapes*, all already in our enum:

- **`GRAPH_COMPLETION_COT`** — chain-of-thought across graph hops → **root-cause "why" chains** ("alert → which deploy → which service → owner"). Higher latency/cost; reserve for explicit "why" questions.
- **`GRAPH_COMPLETION_CONTEXT_EXTENSION`** — iteratively expands the subgraph → **blast-radius / "what else is affected?"** questions.
- **`TEMPORAL`** — time-aware retrieval & ranking (before/after/between). Pairs with our **Memory Timeline** feature. ⚠️ needs `cognify(..., temporal_cognify=True)` at ingest to extract event timestamps — **probe whether our golden graph has them** before promising timeline-aware answers. Note: `temporal_cognify=True` **ignores `custom_prompt`** (documented trap).
- **`SUMMARIES`** — pre-computed summaries, no LLM at query time → instant "what is service X" cards.
- **`RAG_COMPLETION`** — plain vector RAG → a built-in **A/B baseline** to *show on screen* that the graph adds value (this is the "graph beats RAG" demo, for free).

```python
# a tiny router in ask(): pick the SearchType from the question shape
qt = SearchType.GRAPH_COMPLETION
if re.search(r"\bwhy\b|root cause|caused", q):      qt = SearchType.GRAPH_COMPLETION_COT
elif re.search(r"what else|impact|affected|blast", q): qt = SearchType.GRAPH_COMPLETION_CONTEXT_EXTENSION
elif re.search(r"\bwhen\b|before|after|timeline",  q): qt = SearchType.TEMPORAL
r = await cognee.search(query_text=q, query_type=qt, system_prompt=TRIAGE_PROMPT, **kw)
```
> **❌ PROBED & KILLED 2026-06-24 — do not wire COT / CONTEXT_EXTENSION.** Ran both against the golden graph at temp 0, read the real answer strings (`probe_searchtypes.py`, since deleted):
> - *Root-cause "why":* `GRAPH_COMPLETION` 3.0s vs `GRAPH_COMPLETION_COT` **114.1s** (38× slower, multiple LLM calls) → **near-identical answer**, ~10 extra words. 114s would destroy the demo UX.
> - *Blast-radius "what else":* `GRAPH_COMPLETION` 2.2s already named the login system + auth-service + the past outage; `CONTEXT_EXTENSION` 10.2s (5× slower) returned the *same*, slightly *less* detailed answer.
> - **Conclusion:** plain `GRAPH_COMPLETION` already does the multi-entity/blast-radius reasoning at our scale. The heavier types add large latency for zero gain — confirms the earlier "multi-hop ties RAG" research. **Routing NOT shipped.** (`TEMPORAL` untested — needs a `temporal_cognify=True` rebuild; lower priority.) The probe-first rule paid off: it caught a 38× latency regression before it shipped.

### 4.2 `only_context` / `verbose` — evidence panel — ✅ PROBED 2026-06-24, ❌ NOT shipped (keep current citations)
**Finding (verified, `probe_context.py`, since deleted):** `verbose=True` on our normal `GRAPH_COMPLETION` call returns, in ONE call, `text_result` (the answer) **plus** `context_result` (a "Nodes:/Connections:" blob *with* graph triplets like `legacy-cache --[contains]--> auth-service`) **plus** `objects_result` (real `Edge` objects with `relationship_name`). `only_context=True` returns the same context but as a *separate* answer-less call. So graph-grounded evidence **is** obtainable for free (via `verbose`, same call).
**Why we did NOT ship it:**
1. **Breaks our trust property.** Off-domain / "not documented" answers still retrieve nearby nodes, so `context_result` is *always* populated — showing it would surface "evidence" for ungrounded answers. Our `_citations()` suppresses correctly because it gates on the **answer text**, not retrieval. That gate is the right design; raw retrieval would defeat it.
2. **Overlaps the graph node-detail panel** (Flow 8.5 / unit shipped 2026-06-24) which already shows the clean domain triplets.
3. **Touches the sacred hero path:** `ask()` parses `r[0]["search_result"]`, but `verbose=True` puts the answer in `text_result` — so enabling it changes the hero's answer extraction. Medium risk for no clear gain.
**Recorded mechanism for the future:** if we ever want chat-answer evidence, use `verbose=True` (free), read `objects_result`, **filter to domain edges** (drop `contains`/`made_from`/`is a`), and **gate on `_citations()` being non-empty** so off-domain answers still show nothing. Until then: keep the deterministic answer-based citations + the graph panel.

### 4.3 Custom `graph_model` — the strategic one (PROBE FIRST)
Passing a tight Pydantic schema makes *that schema the structured-output target the LLM must fill* — "it can't invent an arbitrary shape." A small, simple schema is a far smaller target than the default open-ended `KnowledgeGraph`, so a **weak model (llama-3.3-70b) succeeds far more often.** This is the exact lever that could (a) harden our demo's build determinism and (b) **unblock the 1.2.x upgrade** that currently bricks on empty graphs.

```python
from cognee.infrastructure.engine import DataPoint   # verify exact import path on 1.1.3

class Owner(DataPoint):
    name: str
    metadata: dict = {"index_fields": ["name"], "identity_fields": ["name"]}  # deterministic UUID5 → no dupes

class Service(DataPoint):
    name: str
    owned_by: Owner                 # field name → edge label "owned_by"
    depends_on: list["Service"]     # edge "depends_on"
    metadata: dict = {"index_fields": ["name"], "identity_fields": ["name"]}

await cognee.cognify(graph_model=Service, custom_prompt=STRICT_EXTRACTION_PROMPT)
```
**`identity_fields` gives deterministic UUIDs** → re-ingesting the same service *updates* the node instead of duplicating it (fights fragmentation). **Probe path:** build a throwaway dataset with a 3-class schema, cognify our 6 runbooks, count nodes/edges and eyeball quality vs the current default-schema graph. *Do not* touch the golden graph — this is a side experiment. If it's cleaner, it's a real upgrade and an upgrade-unblocker; if not, we lost an hour. **Reminder:** `custom_prompt` is the **build-time** extraction prompt — totally separate from our query-time `TRIAGE_PROMPT` (`system_prompt`); mixing them up "silently has no effect."

### 4.4 NodeSet tagging (CONSIDER)
`cognee.add(text, dataset_name=ws, node_set=[system])` tags everything for a system as a first-class graph group; then recall can filter `node_type=NodeSet, node_name=[system]` (only on graph-completion search types). Today we track system→doc_ids in our own ledger, which already powers forget. NodeSet would add *native* graph-level grouping and scoped recall — nice-to-have, not load-bearing. **Note:** it does **not** enable scoped-forget (forget has no node scope), so it complements but doesn't replace the ledger.

### 4.5 MCP server (STRONG CANDIDATE — ~1 day)
The `cognee-mcp` package exposes `remember` / `recall` / `forget` over MCP (API mode points multiple clients at one backend — fits our existing local FastAPI). Our entire thesis is remember→recall(triage)→**forget(decommission)** — a 1:1 map to those three tools. Shipping even a minimal MCP server means an on-call responder triages *from inside Claude Code / Cursor*, and the **hero forget is callable in-IDE**. This is the highest "depth of Cognee" signal per hour of work. (Verify the exact Docker/transport config against the repo — two MCP doc pages 404'd during research.)

---

## 5. The honest "skip / careful" list

- **Cognee Cloud** — we're in the **self-hosted / Best-Use-of-Open-Source** track, and "air-gapped, local fastembed, BYO-key/Ollama" is a *feature* of our story. Chasing the separate "Best Use of Cognee Cloud" prize would mean a different demo (managed infra + connectors + teams) 5 days out. **Skip.** At most a one-line `cognee.serve()` "and it also syncs to a managed/team UI" beat.
- **Ontology (OWL/RDF)** — real, but fiddly to author/maintain and lower ROI than a custom `graph_model` for the same goal. Skip unless we want hard type-validation against a fixed catalog.
- **`memify` entity consolidation** — useful for merging fragmented descriptions, but it's **one LLM call per entity** (token cost, leans on the same weak model). Optional post-ingest enrichment, not a demo centerpiece.
- **Undocumented agentic surface** (`skills` / `tools` / `max_iter`, `@cognee.agent` auto-verify) — present in the signature/banner but **no public spec**. Don't build a demo on shifting, unspecified behavior. Safe to *mention* we're tracking it.

---

## 6. The one tension to manage: feedback / "it learns"

Cognee ships a real adaptive loop — `feedback_influence` on search, `improve()`, `remember(self_improvement=True)`, session memory on by default. It's tempting to pitch "Lethe gets smarter every incident."

**But that's in tension with our whole thesis.** Lethe's contrarian point is that *naive accumulation is the hangover* — memory that only grows, rots. Auto-growing the graph from every chat/feedback is exactly the anti-pattern we critique (and our notes already flag "auto-growing from chatter pollutes the graph"). So:
- **Keep `self_improvement` / auto-feedback OFF by default** (note: `remember()` defaults `self_improvement=True` — another reason we stay on explicit `add`+`cognify`, which don't).
- If we want a "learns" beat, make it **explicit and curated** — a deliberate "save this resolution to memory" action, not silent accumulation. That stays on-thesis (curation, not hoarding) and pairs with our forget/curation story.

This isn't "the feature is bad" — it's "using it silently would undercut our own argument." Use it consciously or not at all.

---

## 7. What this means for the hackathon

- **"Best Use of Cognee" depth** today = remember + recall + the rarely-used **forget**. We can deepen it credibly and cheaply with **multi-search-type routing** (§4.1) and, if we have a day, an **MCP server** (§4.5) — both show we use the lifecycle, not a thin wrapper.
- **Technical Excellence / de-risking** = the **custom `graph_model` probe** (§4.3) is the most valuable experiment we can run: it could harden the demo *and* unblock the version pin, and it shows we understand *why* extraction fails on weak models (a sophisticated answer for judges).
- **README/blog material** — this audit is the spine of an honest "how we use Cognee, and what we deliberately don't" section (the *don't* — Cloud, auto-feedback — is as credible as the *do*).

---

## 8. Recommended next moves (for the build queue, in order)

- ~~Multi-search-type routing (COT/CONTEXT_EXTENSION)~~ — **❌ probed & killed 2026-06-24** (114s for no gain; §4.1). Done with this one.
1. **MCP server** exposing remember/recall/forget — highest "depth of Cognee" signal; hero forget callable in Claude/Cursor. (§4.5)
2. **Probe `only_context`** — is a real graph-grounded evidence panel better than our deterministic citations? (§4.2)
3. **Probe `graph_model`** on a throwaway dataset — graph quality vs default; mainly a **1.2.x-upgrade de-risk**, not a current-demo win (the build doesn't affect answer quality). (§4.3)
4. **`temporal_cognify` + `SearchType.TEMPORAL`** — needs a rebuild; only if timeline-aware *answers* prove worth it. (§4.1)
5. Re-attempt the **1.2.x upgrade** behind a tight `graph_model`. (§0, §4.3)

> Every one of these is **probe-first** by design (build a tiny experiment on a throwaway dataset, read real output, protect golden) — consistent with our hard rules. Nothing here is "just wire it in."

_Sources: docs.cognee.ai (llms-core, llms-api, llms-mcp, llms-cognee-cloud, llms-integrations + guides/custom-graph-model, custom-prompts, ontology-support, custom-tasks-pipelines, node-sets, memify-*, agent-memory-decorator, time-awareness); the cognee-mcp repo README; and a live `inspect`/enum probe of the installed `cognee 1.1.3` (authoritative for availability). A few deep pages 404'd (temporal-awareness, tools-reference, vertical-ai-agents) — flagged inline where relevant._
