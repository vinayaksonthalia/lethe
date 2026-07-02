"""Mechanism-A on a REALISTIC corpus — the honest test.

Question: does the feedback flip happen on a natural on-call wiki WITHOUT
hand-tuning the stale doc's wording to force the baseline to fail?
  - baseline PASSES  -> stale doc wasn't competitive enough (gate working); report it,
                        try a slightly more competitive but still-realistic stale doc.
                        DO NOT force it with artificial wording.
  - baseline FAILS   -> teach feedback (down stale, up correct) -> sweep -> does it flip?

Docs are written naturally. The stale runbook is a realistic hazard: an OLD latency
runbook pointing at a memcached cache that the 2024 architecture simplification removed.
It is NOT worded to dominate retrieval — that's the thing under test.
Model held constant: Groq + local fastembed (from .env).
"""
import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType
from cognee.modules.users.methods import get_default_user
from cognee.memify_pipelines.apply_feedback_weights import apply_feedback_weights_pipeline
from cognee.api.v1.session import session as cognee_session

CORPUS = [
"""Runbook: API latency (current). When API p99 latency rises, the most common cause now is
connection pool saturation on api-service: the pgbouncer pool to postgres-primary fills under
load and queries queue. Mitigation: increase the pgbouncer pool size and shift read traffic to
postgres-replica. Escalate to the core-api team if it persists.""",

"""Runbook: elevated 5xx errors (current). For a spike in 5xx responses, check the load-balancer
health checks and the most recent api-service deploy. If a deploy correlates, roll it back.""",

"""Architecture overview. api-service serves the storefront API. It reads and writes through a
pgbouncer connection pool to postgres-primary (writes) and postgres-replica (reads). After the
2024 simplification there is no separate in-memory caching tier in the request path.""",

"""Ownership registry. api-service is owned by the core-api team. The postgres databases and the
pgbouncer pooler are owned by the data-platform team. The storefront frontend is owned by web.""",

"""Runbook: deploy rollback (current). To roll back api-service, redeploy the previous release tag
from the deploy dashboard and confirm health checks recover before closing the incident.""",

"""Runbook: API Latency Troubleshooting. When API latency is high, the primary cause is the
memcached cluster evicting hot keys under memory pressure, which forces cache misses and slow
upstream reads. Step 1: check memcached eviction metrics. Step 2: flush the memcached cluster.
Step 3: raise the memcached memory allocation and restart the nodes. Step 4: monitor API latency
until it recovers. This is the standard first response for API latency incidents.""",

"""Post-mortem 2024-08-03: API latency incident. Root cause: the memcached cluster ran out of
memory and evicted hot keys, causing widespread high API latency. Resolution: flushed and resized
the memcached cluster. Action item: keep memcached memory headroom above 30%.""",

"""Post-mortem 2026-02-14: API latency incident. Root cause was pgbouncer connection pool
exhaustion during a traffic spike. Fix: raised the pool size and added a read replica. No cache
was involved.""",

"""FAQ. The storefront supports 12 languages, checkout uses Stripe, and the public status page is
at status.example.com. For access requests, file a ticket with the platform team.""",
]

TEACH = [
    {"query": "What is the current runbook for API latency caused by database load?", "score": 5,
     "note": "Correct current runbook (connection pool) — reward."},
    {"query": "Summarize the older memcached latency troubleshooting runbook.", "score": 1,
     "note": "Stale runbook — memcached was removed in 2024 — penalize."},
]
TARGET = "API latency is high right now. What's the likely cause and the fix?"
BETA_SWEEP = [0.0, 0.3, 0.5, 0.7, 1.0]
SESSION_ID = "ma_realistic"


def score(answer):
    t = str(answer).lower()
    correct = any(k in t for k in ["pgbouncer", "connection pool", "pool saturation",
                                   "pool exhaustion", "read replica", "postgres-replica", "pool size"])
    decoy = "memcached" in t
    if correct and not decoy:
        return True, "names connection-pool cause/fix, not memcached"
    if decoy and not correct:
        return False, "centers the removed memcached cache (stale)"
    if not correct:
        return False, "does not name the current cause/fix"
    return False, "mixes in the stale memcached answer"


def flat(x):
    if isinstance(x, list):
        return " || ".join(flat(i) for i in x)
    if isinstance(x, dict):
        return flat(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)


async def ask(q, user, fb=0.0, sid=None):
    kw = {"session_id": sid} if sid else {}
    return flat(await cognee.recall(query_text=q, query_type=SearchType.GRAPH_COMPLETION,
                                    user=user, feedback_influence=fb, **kw))


async def main():
    print(f"=== Ingesting realistic corpus ({len(CORPUS)} docs) ===")
    await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)
    await cognee.remember(CORPUS, self_improvement=False)
    user = await get_default_user()

    print("\n=== Phase 0: baseline (feedback_influence=0) — NO wording tuning ===")
    base = await ask(TARGET, user, fb=0.0)
    ok, why = score(base)
    print("  answer:", base[:300])
    print(f"  baseline: {'PASS' if ok else 'FAIL'} — {why}")
    if ok:
        print("\n>>> Baseline PASSES on the realistic corpus: the stale memcached runbook was NOT\n"
              ">>> competitive enough for retrieval to grab it. That's the gate working, not a flip.\n"
              ">>> FINDING: at this scale retrieval already prefers the current doc. Next: a slightly\n"
              ">>> more competitive (still realistic) stale doc, or accept that a bigger corpus is\n"
              ">>> needed to show feedback's value. NOT faking it.")
        return

    print("\n=== Phase 1: teach feedback (baseline failed organically) ===")
    for it in TEACH:
        await ask(it["query"], user, sid=SESSION_ID)
        sess = await cognee_session.get_session(session_id=SESSION_ID, last_n=1, user=user)
        if not sess:
            print("  !! session empty — cannot teach."); return
        await cognee_session.add_feedback(session_id=SESSION_ID, qa_id=sess[0].qa_id,
                                          feedback_score=it["score"], feedback_text=it["note"], user=user)
        print(f"  fed score={it['score']}: {it['query'][:55]}")

    print("\n=== Phase 2: apply feedback weights ===")
    await apply_feedback_weights_pipeline(user=user, session_ids=[SESSION_ID], alpha=0.9)

    print("\n=== Phase 3: target across feedback_influence sweep ===")
    first_flip = None
    for b in BETA_SWEEP:
        a = await ask(TARGET, user, fb=b)
        good, r = score(a)
        print(f"\n  beta={b:.1f}: {'PASS' if good else 'FAIL'} — {r}")
        print("    ", a[:200])
        if good and first_flip is None and b > 0.0:
            first_flip = b

    print("\n" + "=" * 60)
    if first_flip is not None:
        print(f">>> ORGANIC FLIP at beta={first_flip:.1f} on a REALISTIC corpus (no wording tuning).\n"
              ">>> Hero is demo-ready, not just mechanism-proven. Next: variance across runs.")
    else:
        print(">>> Baseline failed but feedback did NOT flip it. Honest finding: feedback too weak /\n"
              ">>> stale doc too dominant at this scale. Check Phase-1 surfaced the right nodes.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
