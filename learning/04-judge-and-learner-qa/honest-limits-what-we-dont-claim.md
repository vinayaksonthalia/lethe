# Honest Limits — What We Do NOT Claim

**In one line:** The boundaries of Lethe, stated plainly — because a project that knows its own edges is more trustworthy than one that pretends it has none.

## ELI10

A good scientist doesn't say "my volcano model is perfect." They say "it predicts the lava, but it can't tell you the exact day, and here's why." That honesty is what makes people *believe* the parts that DO work. This page is our list of "here's what we can't do" — and saying it out loud is on purpose.

## Why honesty is a strength

Readers have seen a hundred demos that overclaim. The fastest way to lose a sharp reader is to assert something the demo can't back up. We do the opposite: we draw a tight circle around what's **verified**, label everything else **by design** or **known limit**, and we *show* the limits ourselves before anyone has to find them. The hero beat (**forget**) is rock-solid precisely because we didn't water it down with claims we can't defend.

---

## Limit 1 — Not autonomous remediation (it's read-only advice)

**We do NOT claim:** the system fixes incidents, runs commands, restarts services, or takes any action.

**What it actually does:** it **reads** your incident knowledge and **tells you what to check.** A human on-call engineer reads the advice and acts. It's a triage *assistant*, not an *operator*.

Example output is advice, never an action:
> "To troubleshoot high auth-service latency, check the session-store connection pool and its hit rate… and also verify the payments-service…"

There is no execution path, no production credentials, no `kubectl`. This is intentional scope: an assistant you can trust to *talk* is a far safer demo (and product) than a bot you've wired to *touch* prod.

---

## Limit 2 — Off-corpus "facet" over-confidence (a documented LLM limit)

**We do NOT claim:** it always knows when it doesn't know.

It's **excellent** at admitting a **whole missing system** is gone. Real, verified example AFTER forgetting legacy-cache:
> "The legacy-cache is not documented in the runbooks, so its description and dependencies are unknown."

But it can be **over-confident about a *facet* of a system that *does* have a runbook.** Ask *"what's the deploy process for search-index?"* and it may point you at the search-index runbook as if it answered, instead of admitting the **deploy steps specifically aren't written down.**

**Why this happens:** the model struggles to tell apart "I have a document *about* X" from "this document answers *this specific sub-question* about X." That's an **LLM inference limit**, **NOT prompt-fixable** — we tried, hit the ceiling, and **stopped tuning** rather than chase a fix that doesn't exist.

**Why it's acceptable for the demo:** the demo is **hero-driven** (forget). This facet over-confidence is a **known, documented residual**, not on the hero path, and we say so out loud.

**Related:** [judge-questions-answered.md → What happens for questions outside the corpus?](./judge-questions-answered.md)

---

## Limit 3 — AFTER-forget wording isn't word-for-word stable (only the invariant is)

**We do NOT claim:** the post-forget answer is byte-identical every run.

We **do** claim the **invariant**: after forgetting legacy-cache, the answer to "If auth-service latency is high, what should I check?" **never mentions legacy-cache again**, and instead points to what's still in the corpus.

The exact phrasing can vary in surface form. The captured AFTER answer was:
> "To troubleshoot high auth-service latency, check the session-store connection pool and its hit rate, as the auth-service reads session state from it, and also verify the payments-service, which requires and calls the auth-service to validate login tokens."

The **load-bearing claim is "no legacy-cache,"** not "these exact words." (Temperature 0 keeps runs *stable* given identical context, but a fresh build or a rephrased question can shift wording — what stays invariant is the *absence of the forgotten system*.)

---

## Limit 4 — At this small corpus, the graph ≈ plain RAG for simple lookups

**We do NOT claim:** the knowledge graph beats plain vector RAG on every question at this scale.

Honest finding: with only 18 short docs, for **simple single-fact lookups**, the graph layer adds **little** over vectors alone — graph ≈ RAG. The graph's real advantage shows up with **scale** and on **relationship / multi-hop** questions ("what's the blast radius?", "what's in the read path?").

We tested **3 cognee differentiators vs plain RAG** at temperature 0. **Only forget held cleanly** as a decisive win at this corpus size. We report that result instead of inflating the other two. (This honesty is literally the spine of our blog.)

**What we still genuinely claim:** the *automatic prose-to-graph* construction (no schema, no tags) is real cognee value regardless of corpus size — see [judge-questions-answered.md → Why is this "Best Use of Cognee"?](./judge-questions-answered.md).

---

## Limit 5 — The prompt is powerful, but it has a ceiling

**We do NOT claim:** a better prompt can fix everything.

Our TRIAGE_PROMPT was the **single highest-leverage knob** — three controlled experiments (retrieval held constant, prompt the only variable) showed: terse → fluent, leaked `--[owns]-->` → clean prose, invents → admits "not documented." The prompt matters *because the LLM is the combiner* of graph + vector context.

But prompt leverage **cannot:**

- **Conjure a fact retrieval never fetched.** If the answer's chunk fell below the **top-k** cut, no prompt can surface it.
- **Fully eliminate hallucination.** It curbs over-confidence; it doesn't guarantee truth (see Limit 2).

We hit this ceiling on off-corpus over-confidence and **correctly stopped**. Knowing where a knob stops working is part of using it well.

---

## Limit 6 — forget removes from the CORPUS, not from the model's training priors

**We do NOT claim:** forgetting a system erases it from the LLM's brain.

`forget` is a **hard delete of our data** — the raw .txt file (verified: 18 → 16 files), the graph nodes/edges, and the vector embeddings (verified: 0 legacy-cache residue across 5 phrasings). That is real and complete **at the corpus layer.**

But the LLM still carries its **parametric (training) knowledge.** Generic facts about caches, memcached, login outages, etc. live in the model's weights and `forget` cannot touch them. So a forgotten *specific* system is gone from our memory, but the model's *general priors* remain.

**This is exactly why "the graph is clean" alone is NOT proof it forgot.** Retrieved context is **influence, not law** — the model *could* reach into priors. So we verified forget on **two layers:**

1. **Structural** — `only_context` shows 0 legacy-cache residue across 5 phrasings.
2. **Behavioral** — asked AFTER many times (the latency question, name-lookups, adversarial phrasings); legacy-cache **never resurfaced.**

Both checks were necessary. Passing both is what lets us claim forget *honestly*.

---

## The one-paragraph version (for a reader in a hurry)

It's **read-only advice**, not autonomous remediation. It reliably admits a **whole missing system** is gone, but can be **over-confident about a missing facet** of a system that has a runbook (a documented, non-prompt-fixable LLM limit). The post-forget answer's **invariant** ("no stale system") is what's stable, not its exact wording. At this **small corpus** the graph roughly ties plain RAG on simple lookups; **forget** is the clean, decisive differentiator. The prompt is our biggest lever but it **can't invent un-retrieved facts** or fully kill hallucination. And `forget` clears our **corpus**, not the model's **training priors** — which is why we proved it **both structurally and behaviorally.**

---

## Related

- [judge-questions-answered.md](./judge-questions-answered.md) — the full FAQ.
- [newbie-glossary.md](./newbie-glossary.md) — definitions for parametric knowledge, top-k, hallucination, etc.
- [../05-the-research-story/the-debugging-saga-and-lessons.md](../05-the-research-story/the-debugging-saga-and-lessons.md) — how we found these limits (the fragment-bug investigation).
- [../02-cognee-deep-dive/](../02-cognee-deep-dive/) — graph vs vector mechanics behind these limits.
