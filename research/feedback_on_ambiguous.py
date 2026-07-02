"""Does feedback RESOLVE AMBIGUITY on the existing (already-built) ambiguous graph?

The realistic corpus is genuinely ambiguous (2 stale memcached docs vs 2 current
connection-pool docs) → the LLM hedges/varies. The honest value test: does feedback
(down the stale source, up the correct one) make the answer RELIABLY correct?
Measures variance: N runs before vs N runs after. No re-cognify (uses existing graph).
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

TARGET = "API latency is high right now. What's the likely cause and the fix?"
SID = "ambiguity_test"
N = 3

def verdict(a):
    t = str(a).lower()
    correct = any(k in t for k in ["pgbouncer", "connection pool", "pool saturation", "pool exhaustion", "read replica", "pool size"])
    poisoned = "memcached" in t
    return "CORRECT" if (correct and not poisoned) else ("POISONED" if (poisoned and not correct) else ("MIXED" if correct and poisoned else "?"))

def flat(x):
    if isinstance(x, list): return " || ".join(flat(i) for i in x)
    if isinstance(x, dict): return flat(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)

async def ask(user, fb=0.0, sid=None):
    kw = {"session_id": sid} if sid else {}
    return flat(await cognee.recall(query_text=TARGET, query_type=SearchType.GRAPH_COMPLETION, user=user, feedback_influence=fb, **kw))

async def tally(user, fb, label):
    res = [verdict(await ask(user, fb=fb)) for _ in range(N)]
    print(f"  {label} (fb={fb}): {res}  -> CORRECT {res.count('CORRECT')}/{N}")
    return res.count("CORRECT")

async def main():
    user = await get_default_user()
    print("Using the EXISTING 9-doc ambiguous graph (no re-cognify).\n")
    print("=== BEFORE feedback ===")
    before = await tally(user, 0.0, "baseline")

    print("\n=== Teach feedback (down stale memcached, up current connection-pool) ===")
    for q, sc, note in [
        ("What is the current runbook for API latency caused by database load?", 5, "current connection-pool runbook — reward"),
        ("Summarize the older memcached latency troubleshooting runbook.", 1, "stale memcached runbook — penalize"),
    ]:
        flat(await cognee.recall(query_text=q, query_type=SearchType.GRAPH_COMPLETION, user=user, session_id=SID))
        s = await cognee_session.get_session(session_id=SID, last_n=1, user=user)
        if not s: print("  !! session empty"); return
        await cognee_session.add_feedback(session_id=SID, qa_id=s[0].qa_id, feedback_score=sc, feedback_text=note, user=user)
        print(f"  fed {sc}: {q[:55]}")
    await apply_feedback_weights_pipeline(user=user, session_ids=[SID], alpha=0.9)

    print("\n=== AFTER feedback ===")
    after = await tally(user, 0.7, "fb=0.7")

    print("\n" + "=" * 56)
    print(f">>> CORRECT rate: before {before}/{N}  ->  after {after}/{N}")
    if after > before:
        print(">>> Feedback RESOLVED ambiguity (more consistently correct). Realistic value shown.")
    elif after == before == N:
        print(">>> Already correct before & after — no ambiguity to resolve in this run.")
    else:
        print(">>> Feedback did NOT improve consistency. Honest: weak on this corpus.")
    print("=" * 56)

asyncio.run(main())
