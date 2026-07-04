# Newbie Glossary

**In one line:** Every nerdy word in this project, explained the way you'd explain it to a smart 10-year-old, with one line tying it back to Lethe.

## ELI10

Grown-ups invented a lot of fancy words for simple ideas. This page un-fancies them. Read any term you bumped into and you'll get a plain picture plus how *we* actually use it.

Terms are alphabetical. Each one ends with **→ in this project:** so you always see the real connection.

---

### CACHING (=false)
A **cache** is when a program saves an old answer so it doesn't have to think again. Fast — but dangerous if the truth changed. We turn caching **off** so a repeated question is always answered fresh.
**→ in this project:** if caching were on, asking the same question after we *forget* a system would replay the old stale answer and hide that forget worked. We set `CACHING=false` before importing cognee.

### cognify
The cognee command that **reads your plain-English documents and builds a brain out of them** — one LLM pass that turns sentences into a graph of entities and relationships, plus searchable vectors. You don't give it any structure; it figures the structure out.
**→ in this project:** `cognee.cognify()` reads our 7 runbooks/post-mortems and auto-builds the incident knowledge graph + vectors. We never wrote a schema.

### context window
The amount of text an LLM can "hold in its head" at once when answering. Everything you want it to consider has to fit inside this window.
**→ in this project:** the retrieved chunks + graph edges get packed into the context window alongside our prompt; if a fact didn't get retrieved, it's not in the window, so the model can't use it.

### cosine similarity
A way to measure how much two arrows point in the **same direction**. If two pieces of text mean similar things, their vectors point similarly, so cosine similarity is high. It cares about *direction* (meaning), not length.
**→ in this project:** vector search ranks chunks by cosine similarity to the question in 384-dimensional meaning space — that's how "high latency" can match a chunk that says "slow."

### embedding
Turning a piece of text into a **list of numbers** that captures its meaning, so a computer can compare meanings with math. Similar meanings → similar number lists.
**→ in this project:** every chunk and every question becomes a 384-number embedding via the local `BAAI/bge-small-en-v1.5` model — offline, zero API quota.

### entity
A **thing** the system recognizes as important — usually a noun like a service or a team. In a graph it becomes a node.
**→ in this project:** entities are systems and teams like `auth-service`, `payments-service`, `payments-team`, `legacy-cache`.

### entity resolution
Realizing that **two mentions are the same thing** and merging them into one. "auth-service" in three different documents should be **one** entity, not three.
**→ in this project:** this is why facts about `auth-service` scattered across 3 docs collapse into one connected node — and why blast-radius traversal works at all.

### GRAPH_COMPLETION
The cognee search mode we use. It does **vector search → graph traversal → feed both to the LLM → get one written answer.** "Completion" means the LLM completes/writes the answer.
**→ in this project:** `SearchType.GRAPH_COMPLETION` is the retrieval mode behind every `ask()` call.

### hallucination
When an LLM **makes something up** that sounds confident but isn't supported by the facts it was given. Two flavors: inventing to fill a gap (confabulation) or flatly contradicting what it was told (rare).
**→ in this project:** because retrieved context is *influence not law*, hallucination is possible — which is exactly why we verified forget by behavior, not just by checking the graph.

### knowledge graph
A map of **things connected by relationships** — dots (nodes) joined by labeled arrows (edges). It captures not just facts but how facts relate.
**→ in this project:** cognee builds one in Kùzu, e.g. `payments-service --[calls]--> auth-service`, so we can ask connection/dependency questions.

### ledger
Our little bookkeeping file, `ledger.json`, that maps **each system → the list of document ids** that describe it. Written during ingest.
**→ in this project:** `forget_system()` reads the ledger to find which `data_ids` to delete when you decommission a system like legacy-cache.

### node / edge
A **node** is a dot in the graph (an entity). An **edge** is an arrow connecting two nodes (a relationship). Cognee's defaults: `Node{id, name, type, description}` and `Edge{source_node_id, target_node_id, relationship_name, description}`.
**→ in this project:** `auth-service` is a node; `--[calls]-->` between two services is an edge.

### parametric knowledge
The stuff an LLM **already learned during training**, baked into its weights — separate from anything you give it at question time. It's the model's "general world memory."
**→ in this project:** forget deletes data from our *corpus*, NOT from the model's parametric knowledge — so a clean graph alone can't prove it forgot; the model might still recall from training. That's why we behavior-tested.

### prompt (system / user)
A **prompt** is the instructions/question you give the LLM. The **user prompt** is the actual question. The **system prompt** is the standing rules ("how to behave") that apply to every answer.
**→ in this project:** the user prompt is the on-call question; our **TRIAGE_PROMPT** is the system prompt that says "answer in plain runbook prose, never expose graph internals, say 'not documented' if unsure."

### RAG
**Retrieval-Augmented Generation.** Instead of answering from memory alone, the LLM first **retrieves** relevant text, then **generates** an answer using it. Plain RAG retrieves chunks only.
**→ in this project:** cognee is RAG *plus a graph* — it retrieves chunks **and** connected nodes/edges. At our small corpus, graph ≈ RAG for simple lookups; the graph's edge grows with scale and multi-hop questions.

### retrieval
The "go find the relevant stuff" step before answering. Here it's two-part: vectors find what's **relevant**, the graph finds what's **connected** to it.
**→ in this project:** retrieval assembles the context that gets concatenated into the prompt; `only_context=True` lets us inspect that context without paying for an answer.

### temperature 0
A dial on the LLM's randomness. **Zero** means "always pick the most likely next word" — answers become **repeatable** instead of varying run to run.
**→ in this project:** we pin temperature 0 so we can run controlled experiments where the **prompt is the only variable** and trust that any change in output came from the prompt.

### top-k
The "**keep the best k**" rule. Vector search returns only the *k* closest chunks, not everything.
**→ in this project:** top-k is a hard cap — if a needed fact lives in a chunk that didn't make the cut, the graph can't reach it and the LLM never sees it. This is the root of the prompt-leverage *ceiling*.

### vector
A list of numbers representing a point (or arrow) in space. An embedding **is** a vector. Similar meanings live near each other.
**→ in this project:** every chunk and question is a 384-dimensional vector; closeness in that space = similar meaning.

### vector database
A storage system built to hold lots of vectors and **quickly find the nearest ones** to a query vector.
**→ in this project:** we use **LanceDB** to store chunk embeddings and run the cosine-similarity top-k search.

---

## Related

- [judge-questions-answered.md](./judge-questions-answered.md) — the FAQ that uses all these terms.
- [honest-limits-what-we-dont-claim.md](./honest-limits-what-we-dont-claim.md) — the honest boundaries.
- [../01-how-it-works/](../01-how-it-works/) — see the terms in action across the pipeline.
- [../02-cognee-deep-dive/](../02-cognee-deep-dive/) — deeper on graph + vectors.
