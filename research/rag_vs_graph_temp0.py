"""THE FORK: at temp 0 on the realistic corpus, GRAPH was 20/20 CORRECT. Is that a WIN?
Only if plain RAG does WORSE on the same query. Run RAG_COMPLETION N=20 at temp 0 and compare.

  RAG mostly WRONG/HEDGE  -> "Cognee's graph ignores stale docs; RAG gets fooled" = candidate HEADLINE
                            (deterministic, realistic-data win, stronger than 5-vs-4 blast-radius)
  RAG also ~correct       -> no realistic graph advantage on this query; blast-radius stays headline

Existing graph (no re-cognify), temperature 0.0 (.env LLM_ARGS), Groq constant.
"""
import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType
from cognee.modules.users.methods import get_default_user

TARGET = "API latency is high right now. What's the likely cause and the fix?"
N = 12

def verdict(a):
    t = str(a).lower()
    pool = any(k in t for k in ["pgbouncer", "connection pool", "pool saturation", "pool exhaustion",
                                "read replica", "postgres-replica", "pool size"])
    mem = "memcached" in t
    if pool and not mem: return "CORRECT"
    if mem and not pool: return "WRONG"
    if pool and mem: return "HEDGE"
    return "?"

def flat(x):
    if isinstance(x, list): return " || ".join(flat(i) for i in x)
    if isinstance(x, dict): return flat(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)

async def main():
    user = await get_default_user()
    c = {"CORRECT": 0, "HEDGE": 0, "WRONG": 0, "?": 0}
    sample = None
    for i in range(N):
        a = flat(await cognee.recall(query_text=TARGET, query_type=SearchType.RAG_COMPLETION,
                                     user=user, top_k=10))
        c[verdict(a)] += 1
        if sample is None: sample = a[:220]
    print(f"RAG_COMPLETION (temp 0, N={N}):", c)
    print("  sample answer:", sample)
    print("\n  GRAPH_COMPLETION was: CORRECT 20/20 (established)")
    print("=" * 58)
    if c["CORRECT"] <= N * 0.5:
        print(">>> RAG gets fooled while GRAPH stays correct → graph-vs-RAG WIN on REALISTIC data.")
        print(">>> 'Cognee knows what's current; RAG trusts the stale runbook' = candidate HEADLINE.")
    else:
        print(">>> RAG also mostly correct → no realistic graph advantage here; blast-radius stays headline.")
    print("=" * 58)

asyncio.run(main())
