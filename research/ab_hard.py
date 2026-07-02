"""Hard A/B that verifies the PATH, not just the answer string.

The owner (Sana) is reachable only by a 2-hop chain across dissociated docs:
  Checkout->Payment->Auth->eu-west-redis  (architecture, symptom-near)
  eu-west-redis -> team platform-reliability  (service catalog, dissociated)
  platform-reliability -> Sana  (on-call roster, dissociated)

We prove:
  (1) RAG's RETRIEVED CONTEXT does not even contain the roster/Sana -> RAG structurally can't answer.
  (2) RAG's answer therefore punts or names the wrong person.
  (3) GRAPH's context contains the redis->team->Sana triplets, and it names Sana.
A tie proves nothing; we want RAG-fails / GRAPH-wins, with receipts.
"""
import os, asyncio, glob, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
Q = ("Tonight checkout is failing and users report flaky logins. "
     "Name the single on-call engineer I should page right now.")
TOP_K = 5  # a standard production RAG retrieval budget

def s(x):
    """Flatten a cognee search result to a plain string for inspection."""
    if isinstance(x, list):
        return " || ".join(s(i) for i in x)
    if isinstance(x, dict):
        return s(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)

def has(text, *names):
    t = str(text).lower()
    return all(n.lower() in t for n in names)

async def main():
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    docs = sorted(glob.glob(os.path.join(DATA_DIR, "*.md")))
    print(f"Ingesting {len(docs)} docs.")
    for p in docs:
        with open(p) as f:
            await cognee.add(f.read())
    print("cognify...")
    await cognee.cognify()
    print("\nQUESTION:", Q, "(top_k =", TOP_K, ")\n")

    # ---- RAG: inspect the retrieved context (the PATH check) ----
    rag_ctx = s(await cognee.search(query_text=Q, query_type=SearchType.RAG_COMPLETION,
                                    top_k=TOP_K, only_context=True))
    rag_ans = s(await cognee.search(query_text=Q, query_type=SearchType.RAG_COMPLETION, top_k=TOP_K))
    print("----- RAG retrieved context (first 500 chars) -----")
    print(" ", rag_ctx[:500])
    rag_saw_owner = ("sana" in rag_ctx.lower()) or ("on-call" in rag_ctx.lower()) or ("roster" in rag_ctx.lower())
    print(f"  >> did RAG even RETRIEVE the owner/roster info? {rag_saw_owner}")
    print("----- RAG answer -----")
    print(" ", rag_ans[:300])
    print(f"  >> RAG named 'Sana'? {'sana' in rag_ans.lower()}")

    # ---- GRAPH: inspect the traversed triplets (the PATH check) ----
    g_ctx = s(await cognee.search(query_text=Q, query_type=SearchType.GRAPH_COMPLETION, only_context=True))
    g_ans = s(await cognee.search(query_text=Q, query_type=SearchType.GRAPH_COMPLETION))
    print("\n----- GRAPH retrieved context (first 700 chars) -----")
    print(" ", g_ctx[:700])
    print(f"  >> GRAPH context shows the redis->team->Sana path? {has(g_ctx,'sana') and has(g_ctx,'platform-reliability')}")
    print("----- GRAPH answer -----")
    print(" ", g_ans[:300])
    print(f"  >> GRAPH named 'Sana'? {'sana' in g_ans.lower()}")

    print("\n===== RECEIPT =====")
    print(f"  RAG retrieved the owner doc:  {rag_saw_owner}   (want False)")
    print(f"  RAG named Sana:               {'sana' in rag_ans.lower()}   (want False)")
    print(f"  GRAPH named Sana:             {'sana' in g_ans.lower()}   (want True)")
    win = (not rag_saw_owner) and ('sana' not in rag_ans.lower()) and ('sana' in g_ans.lower())
    print(f"  >>> CLEAN graph-wins / RAG-fails split? {win}")

if __name__ == "__main__":
    asyncio.run(main())
