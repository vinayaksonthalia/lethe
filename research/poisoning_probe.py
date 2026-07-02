"""Cheap probe on the ALREADY-BUILT graph (no re-cognify): does plain RAG get
POISONED by the stale memcached runbooks while Cognee's GRAPH reasoning stays correct?

If RAG -> memcached (wrong/stale) and GRAPH -> connection-pool (correct), the realistic
demoable hero is: 'Cognee's graph resists stale-doc poisoning that fools vector RAG' —
which is native (extraction + connected-context reasoning), realistic, and needs no
constructed feedback scenario.
"""
import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType
from cognee.modules.users.methods import get_default_user

TARGET = "API latency is high right now. What's the likely cause and the fix?"

def verdict(answer):
    t = str(answer).lower()
    correct = any(k in t for k in ["pgbouncer", "connection pool", "pool saturation",
                                   "pool exhaustion", "read replica", "postgres-replica", "pool size"])
    poisoned = "memcached" in t
    if poisoned and not correct: return "POISONED (stale memcached)"
    if correct and not poisoned: return "CORRECT (connection pool)"
    if correct and poisoned: return "MIXED (both)"
    return "neither"

def flat(x):
    if isinstance(x, list): return " || ".join(flat(i) for i in x)
    if isinstance(x, dict): return flat(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)

async def go(qt, user, **kw):
    return flat(await cognee.recall(query_text=TARGET, query_type=qt, user=user, **kw))

async def main():
    user = await get_default_user()
    print("Querying the EXISTING 9-doc graph (2 stale memcached docs + current connection-pool docs).\n")

    chunks = await go(SearchType.CHUNKS, user, top_k=5)
    print("--- (A) plain VECTOR retrieval (CHUNKS) — what does it surface? ---")
    print("   memcached present in top chunks?", "memcached" in chunks.lower(),
          "| connection-pool present?", any(k in chunks.lower() for k in ["pgbouncer", "connection pool"]))

    rag = await go(SearchType.RAG_COMPLETION, user, top_k=5)
    print("\n--- (B) RAG_COMPLETION (vector + LLM) ---")
    print("   ", rag[:240])
    print("   verdict:", verdict(rag))

    graph = await go(SearchType.GRAPH_COMPLETION, user)
    print("\n--- (C) GRAPH_COMPLETION (Cognee graph reasoning) ---")
    print("   ", graph[:240])
    print("   verdict:", verdict(graph))

    print("\n" + "=" * 60)
    rv, gv = verdict(rag), verdict(graph)
    if "POISONED" in rv and "CORRECT" in gv:
        print(">>> HERO FOUND: RAG gets POISONED by the stale docs; Cognee's GRAPH stays CORRECT.\n"
              ">>> Realistic, native, demoable — and needs NO constructed feedback scenario.")
    elif gv == rv:
        print(f">>> Both same ({gv}). No poisoning gap to demo here.")
    else:
        print(f">>> RAG={rv} | GRAPH={gv}. Inspect before concluding.")
    print("=" * 60)

asyncio.run(main())
