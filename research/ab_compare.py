"""A/B: does Cognee's GRAPH actually earn its place, or could plain vector RAG do it?

Same documents, same LLM. Only the retrieval differs:
  - CHUNKS          -> what plain vector search SEES (free, local embeddings, no LLM)
  - RAG_COMPLETION  -> plain vector RAG + LLM  (the "just use search + a smart LLM" baseline)
  - GRAPH_COMPLETION-> Cognee graph traversal + LLM

The answer (page **Sana**) lives ONLY in the bland ownership registry, which shares no
words with the question. Reaching her needs the chain Checkout->Payment->Auth->Redis->owner.
We check, honestly, whether each method names Sana.
"""
import os, asyncio, glob, warnings
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
Q = ("Tonight checkout is failing and users report flaky logins. "
     "Name the single engineer I should page, and the recent change that most likely caused this.")

def has(ans, name="sana"):
    return name.lower() in str(ans).lower()

def show(ans):
    r = ans[0] if isinstance(ans, list) and ans else ans
    if isinstance(r, dict):
        r = r.get("search_result", r)
    return r

async def main():
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    docs = sorted(glob.glob(os.path.join(DATA_DIR, "*.md")))
    print(f"Ingesting {len(docs)} docs: {[os.path.basename(d) for d in docs]}")
    for p in docs:
        with open(p) as f:
            await cognee.add(f.read())
    print("cognify...")
    await cognee.cognify()

    print("\nQUESTION:", Q, "\n")

    # 1) What does plain VECTOR retrieval even surface? (free, local)
    chunks = await cognee.search(query_text=Q, query_type=SearchType.CHUNKS)
    print("----- (A) PLAIN VECTOR retrieval — top chunks it sees -----")
    for c in (chunks[:5] if isinstance(chunks, list) else [chunks]):
        txt = (c.get("text") if isinstance(c, dict) else str(c))
        print("  •", str(txt)[:110].replace("\n", " "))
    print(f"  >> does retrieval surface the owner 'Sana'? {has(chunks)}")

    # 2) Plain RAG answer
    rag = await cognee.search(query_text=Q, query_type=SearchType.RAG_COMPLETION)
    print("\n----- (B) PLAIN RAG_COMPLETION answer -----")
    print(" ", show(rag))
    print(f"  >> names Sana? {has(rag)}")

    # 3) Graph answer
    graph = await cognee.search(query_text=Q, query_type=SearchType.GRAPH_COMPLETION)
    print("\n----- (C) GRAPH_COMPLETION answer -----")
    print(" ", show(graph))
    print(f"  >> names Sana? {has(graph)}")

    print("\n===== VERDICT =====")
    print(f"  plain RAG named the right owner: {has(rag)}")
    print(f"  graph named the right owner:     {has(graph)}")

if __name__ == "__main__":
    asyncio.run(main())
