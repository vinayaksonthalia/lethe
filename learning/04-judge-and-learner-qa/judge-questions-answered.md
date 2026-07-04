# Judge & Newbie Questions, Answered

**In one line:** Every sharp question a hackathon judge (or a curious beginner) might fire at Lethe, with a short, honest, grounded answer.

## ELI10

Imagine you built a robot librarian that reads your team's notebooks and learns who depends on whom. A judge walks up and starts poking it: "How do you actually *know* this? What if you're wrong? What did you delete?" This page is the robot's honest answers to all those pokes — no bluffing.

---

## What is this project, in one breath?

**Lethe** is an on-call triage assistant. You feed it your team's incident knowledge (runbooks, post-mortems, ownership docs) as plain prose. It builds a memory you can ask questions like *"If auth-service latency is high, what should I check?"* — and, crucially, it can **forget** a system you decommission so it never gives you stale advice again.

The thesis: **everyone builds AI that remembers MORE; the real problem is AI that remembers the WRONG/STALE thing.** Our hero feature is **forget**.

---

## How does it turn messy docs into a graph?

We hand cognee plain English. Two calls do the work:

1. `cognee.add(text)` — stores the raw document. **No schema, no tags, no structure** required from us.
2. `cognee.cognify()` — runs **one LLM extraction pass** over the text chunks. Using cognee's built-in prompt (`generate_graph_prompt.txt`), the LLM is told to act like a "top-tier algorithm" that reads the prose and emits a **KnowledgeGraph**: typed **Nodes** (entities) and **Edges** (relationships).

The magic is that *we never define the graph.* Cognee **infers** the structure from the sentences. A line like "payments-service calls auth-service to validate each login token" becomes a `payments-service` node, an `auth-service` node, and a `calls` edge between them — automatically.

**Related:** [How does it decide what's an entity?](#how-does-it-decide-whats-an-entity) · [../01-how-it-works/](../01-how-it-works/)

---

## How does it decide what's an entity?

The extraction prompt tells the LLM to pull out **entities as typed nodes with human-readable names**. In our corpus, the entities that surface are the systems and teams: `api-gateway`, `auth-service`, `payments-service`, `legacy-cache`, `search-index`, `primary session store`, `core-platform`, `payments-team`, `discovery-team`.

The exact node shape (cognee's default schema, from `cognee.shared.data_models`) is:

```
Node{ id, name, type, description }
```

So each entity gets an id, a readable `name` (e.g. "auth-service"), a `type` (e.g. a service or team), and a short `description` distilled from the text.

**Honest note:** what counts as an entity is the LLM's judgment, guided by the prompt. It's reliable for clear nouns like service names; it's not a deterministic parser.

---

## How does it link entities across documents?

Through **entity resolution** (also called coreference resolution). The extraction prompt explicitly instructs the LLM to **merge the same entity across docs into ONE node**.

This matters a lot here. `auth-service` is mentioned in its own runbook, in the payments-service runbook ("calls auth-service"), and in the legacy-cache runbook ("sits in front of auth-service session reads"). Entity resolution collapses all of those into a **single `auth-service` node**. That single node is now connected to everything that referenced it.

That's *why* a question like "what's in the auth read path?" can stitch together facts that lived in three different files, and why **blast-radius / traversal** works at all — the connections only exist because the duplicates got merged.

**Related:** [Why temperature 0?](#why-temperature-0) · [../02-cognee-deep-dive/](../02-cognee-deep-dive/)

---

## Does it store vectors or a graph? (Both?)

**Both — built from one cognify pass.** This hybrid is the heart of our "Best Use of Cognee" claim.

| Store | Engine | Holds | Used for |
|-------|--------|-------|----------|
| **Graph** | Kùzu | Nodes + edges | Structure / traversal / "what's connected" |
| **Vectors** | LanceDB | Chunk embeddings | Semantic recall / "what's relevant" |
| Relational metadata | SQLite | Bookkeeping | Tracking data records |

One `cognify()` call populates the graph **and** the vector store. They are two views of the same knowledge: vectors find what's *relevant*, the graph finds what's *connected to it*.

---

## When does it use graph vs vectors?

On **every** retrieval, it uses both, in this order (this is `SearchType.GRAPH_COMPLETION`, what we use):

1. **Vectors first** — embed the question, search LanceDB for the top-k nearest chunks. These are the **entry doors** into the knowledge.
2. **Graph second** — traverse from those entry nodes to pull connected nodes and edges (the relationships).
3. Both results get serialized to plain text, concatenated, and handed to the LLM, which writes one answer.

So it's not "vectors OR graph" per query — it's **vectors to find the door, graph to walk the rooms behind it.**

**Related:** [How are graph + vectors combined into one answer?](#how-are-graph--vectors-combined-into-one-answer)

---

## What embedding model does it use?

**`BAAI/bge-small-en-v1.5`**, run **locally via fastembed**, producing **384-dimensional** vectors.

This is a deliberate choice. Cognee's default is OpenAI's `text-embedding-3-small`, which **costs API quota** on every embed. We switched to local fastembed so:

- Embeddings run **offline, with ZERO API quota.**
- We could re-ingest the corpus **dozens of times** during testing without burning a cent on embeddings — only LLM completion calls cost quota.

**Verified:** the local model never touched a paid embedding API across the whole build/test cycle.

---

## How are graph + vectors combined into one answer?

This is the most important honest detail: **there is NO numeric fusion.** No re-ranking math, no weighted score blend.

The combination is **concatenation + an LLM**:

1. The top-k chunks (from vectors) are serialized to text.
2. The connected nodes/edges (from graph traversal) are serialized to text.
3. Both blobs are **glued together** into the completion prompt.
4. The **LLM reads the whole thing and writes one answer.**

**The LLM is the combiner.** The "merge" is literally just putting both pieces of context in front of the model and letting it reason. That's why the *prompt* turned out to be our highest-leverage knob (see below).

---

## How does vector retrieval pick which chunks?

It uses **cosine similarity in 384-dimensional "meaning space."**

- The question is embedded into a 384-d vector.
- Every stored chunk is already a 384-d vector.
- LanceDB returns the **top-k** chunks whose vectors point in the most similar direction (highest cosine similarity) to the question's vector.

Closeness here means *similar meaning*, not similar words — "high latency" can pull a chunk that says "slow response" even without shared keywords.

**Limit:** top-k is a fixed cap. If the fact you need lives in a chunk that didn't make the top-k cut, the graph can't traverse to it and the LLM never sees it. (This is the root of our "prompt-leverage ceiling" — see [honest-limits-what-we-dont-claim.md](./honest-limits-what-we-dont-claim.md).)

---

## What does a serialized edge actually look like?

When we call retrieval with `only_context=True` (which returns the assembled context **without** running the answer LLM — cheap, since embeddings are local), we can see the raw format. Real captured examples:

- **Nodes** render with their original text wrapped in markers:
  ```
  Node: auth-service ... __node_content_start__ <original runbook text> __node_content_end__
  ```
- **Relationships** render as triples:
  ```
  payments-team --[owns]---> payments-service
  ```
- **Tag lists** appear like:
  ```
  [team, ownership, api-gateway]
  ```

This raw syntax is exactly what we must **never leak to the user.** Early on, the answer to "Who owns the payments-service?" literally spat back `--[owns]-->`. Our TRIAGE_PROMPT now forbids exposing any graph internals, and the answer became clean: *"The payments-team owns the payments-service."*

---

## What does `forget` actually remove?

A **real, hard delete** — across all three layers. `incident_brain.forget_system(name, ledger)`:

1. Looks up the system's document `data_ids` in `ledger.json`.
2. Calls `cognee.forget(data_id=uid, dataset="main_dataset")` for each — **without** `memory_only`.

In cognee's source this routes to `_forget_data_item` → `delete_data`, which **hard-deletes**:

- the **data record**,
- the **raw .txt file on disk** (verified: file count in the data dir went **18 → 16** after forgetting legacy-cache's 2 docs),
- the **derived graph nodes/edges**,
- the **vector embeddings**.

**Verified:** after forgetting, `only_context` showed **0 legacy-cache residue across 5 different phrasings.**

**Related:** [Is it a real delete or just hidden?](#is-it-a-real-delete-or-just-hidden)

---

## Is it a real delete or just hidden?

**Real delete, not a soft hide.** This is a deliberate design point.

Cognee *also* offers `forget(..., memory_only=True)`, which removes the graph + vectors but **keeps the raw files**. We do **NOT** use that. We use the full delete so the source document is gone too.

Proof it's real and not cosmetic:
- The **raw .txt files physically disappear** from the data directory (18 → 16).
- The graph and vectors show **zero residue** under inspection.

A soft exclude would leave the data sitting there, retrievable by a different phrasing. A hard delete can't be re-surfaced because there's nothing left to surface.

---

## Why does the same question flip?

Two different reasons — don't confuse them:

1. **The forget demo (intended flip).** BEFORE forgetting legacy-cache, "If auth-service latency is high, what should I check?" answered *"check the legacy-cache..."*. AFTER, the **same** question answers *"Check the session-store connection pool and its hit rate... and verify the payments-service..."*. This flip is the **hero beat** — the corpus genuinely changed.

2. **The fragment bug (the saga, now fixed).** Early on, the same question would *sometimes* return a bare word like `legacy-cache` instead of a full sentence. We chased the build, the phrasing, the extraction — **all red herrings.** The real cause: cognee's **default completion prompt** (`answer_simple_question.txt`) literally says *"Answer the question using the provided context. Be as brief as possible."* On under-specified questions the LLM went terse. `only_context` proved retrieval was **always rich** — the problem was purely the prompt. Fixed with our **TRIAGE_PROMPT**.

**Related:** [Can the LLM contradict retrieved facts?](#can-the-llm-contradict-retrieved-facts)

---

## Can the LLM contradict retrieved facts?

**Yes — the retrieved context is INFLUENCE, not a hard constraint.** This is a real property of LLM-based retrieval and we don't hide it. Two failure modes:

- **Confabulation** — inventing facts to fill gaps (relatively common).
- **Flat contradiction** — negating clear context (rare).

Because of this, "the graph is clean after forget" is **NOT, by itself, proof that it forgot.** The model could still reach into its **parametric (training) knowledge.** That's exactly why we verified forget on **two layers**: structurally (graph/vector residue = 0) **and** behaviorally (asked AFTER many times, including adversarial phrasings — legacy-cache never resurfaced).

**Related:** [honest-limits-what-we-dont-claim.md](./honest-limits-what-we-dont-claim.md)

---

## What happens for questions outside the corpus?

We added a guardrail in the TRIAGE_PROMPT: if the context lacks the specific answer, say it is **"not documented in the runbooks."** This both preserves the forget hero and curbs hallucination.

It works well for **whole missing systems.** Real example AFTER forget — *"What is the legacy-cache?"* → *"The legacy-cache is not documented in the runbooks, so its description and dependencies are unknown."*

**Honest limit:** it can be **over-confident about a *facet* of a system that *does* have a runbook.** Ask "what's the deploy process for search-index?" and it may point to the search-index runbook instead of admitting the *deploy steps* aren't written down. Telling apart "I have a doc about X" from "this doc answers *this specific sub-question* about X" is an LLM inference limit, **not prompt-fixable.** We documented it and stopped tuning. See [honest-limits-what-we-dont-claim.md](./honest-limits-what-we-dont-claim.md).

---

## Why is this "Best Use of Cognee"?

Three reasons, in order of strength:

1. **Cognee turns messy PROSE into a graph automatically — no schema, no tagging.** Plain RAG only embeds chunks (no structure). A traditional knowledge graph needs a schema defined up front. Cognee **infers** the structure from the prose. That inference is the genuinely-Cognee magic.
2. **Hybrid graph + vector, queried together.** Vectors find what's relevant; the graph finds what's connected to it — and the LLM combines both into one answer.
3. **forget is the leg the official companybrain starter lacks.** Even cognee's own integrations shelf ships **no forget UX.** We built the missing third leg (remember → learn → **forget**) and made it the hero.

**Honest framing:** at our *small* corpus, the graph layer adds little over vectors for *simple lookups* (graph ≈ RAG); its edge grows with scale and on relationship / multi-hop questions. We tested 3 cognee differentiators vs RAG at temp 0; only **forget** held cleanly. That honesty is the spine of our blog.

---

## How is this different from companybrain?

`companybrain` is cognee's official starter. It demonstrates **remember** (and a weak "learn"). It does **not** demonstrate **forget**.

Lethe headlines **remember + forget**, with "learns/improve" as a weak supporting leg only. **Forget is our hero** — the one leg the starter lacks, and the one that directly answers the thesis (don't just remember more; forget the stale thing).

---

## Why temperature 0?

**Determinism for honest measurement.** Temperature 0 makes the LLM pick its highest-probability token every time, so the answer to a fixed question + fixed context is **stable**.

This let us run **three controlled experiments** with retrieval held constant and the **prompt as the ONLY variable**:

- terse → fluent,
- leaked `--[owns]-->` → clean plain language,
- invents → admits "not documented."

Because temperature was pinned at 0, any change in the answer could be attributed to the prompt — not to random sampling. That's how we *proved* the prompt is the high-leverage knob, given that the LLM is the combiner.

---

## What is `CACHING=false` for?

We set `os.environ["CACHING"]="false"` **before importing cognee** in `incident_brain.py`.

Without it, cognee could serve a **cached answer** for a question we asked before. That would be a disaster for the forget demo: we ask "If auth-service latency is high…?", forget legacy-cache, then ask the **same** question again. A cache would replay the **old (stale) answer**, hiding the fact that the corpus changed — making forget look broken when it actually worked.

Caching off guarantees the post-forget query is **freshly computed against the current graph.**

**Related:** [Why does the same question flip?](#why-does-the-same-question-flip)

---

## Related

- [newbie-glossary.md](./newbie-glossary.md) — plain-English definitions of every term used here.
- [honest-limits-what-we-dont-claim.md](./honest-limits-what-we-dont-claim.md) — what we deliberately do NOT claim.
- [../01-how-it-works/](../01-how-it-works/) — the end-to-end pipeline.
- [../02-cognee-deep-dive/](../02-cognee-deep-dive/) — cognee internals.
- [../05-the-research-story/the-debugging-saga-and-lessons.md](../05-the-research-story/the-debugging-saga-and-lessons.md) — the debugging story behind the fragment bug.
