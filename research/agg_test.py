"""TIME-BOXED graph-aggregation test (the graph's strongest claim, not its weakest).

Dependencies are now SCATTERED across separate docs:
  Checkout->Payment, Payment->Auth, Auth->eu-west-redis, Cart->Auth, OrderHistory->Auth
So "every service impacted if eu-west-redis fails" = {Checkout, Payment, Auth, Cart, OrderHistory} (5),
and no single chunk contains the full answer.

RAG returns the top-k most SIMILAR chunks -> it can under-report and not know it missed one.
A graph traversal reaches every dependent by construction. We measure completeness for each.
"""
import os, asyncio, glob, warnings
from collections import deque
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType
from cognee.infrastructure.databases.graph import get_graph_engine

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SERVICES = ["checkout", "payment", "auth", "cart", "orderhistory"]
Q = "List every service that would be impacted if the eu-west-redis cache failed. Name all of them."

def nid(n):
    if isinstance(n, (list, tuple)): return str(n[0])
    if isinstance(n, dict): return str(n.get("id"))
    return str(getattr(n, "id", n))

def nlabel(n):
    props = n[1] if isinstance(n, (list, tuple)) and len(n) > 1 else (n if isinstance(n, dict) else {})
    if isinstance(props, dict):
        return str(props.get("name") or props.get("text") or props.get("description") or props)
    return str(n)

def edge_ends(e):
    if isinstance(e, (list, tuple)): return str(e[0]), str(e[1])
    return str(getattr(e, "source_node_id", "")), str(getattr(e, "target_node_id", ""))

def s(x):
    if isinstance(x, list): return " || ".join(s(i) for i in x)
    if isinstance(x, dict): return s(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)

async def main():
    await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)
    docs = sorted(glob.glob(os.path.join(DATA_DIR, "*.md")))
    print(f"Ingesting {len(docs)} docs; cognify...")
    for p in docs:
        with open(p) as f: await cognee.add(f.read())
    await cognee.cognify()

    g = await get_graph_engine()
    nodes, edges = await g.get_graph_data()
    print(f"GRAPH: {len(nodes)} nodes, {len(edges)} edges")
    print("sample node:", repr(nodes[0])[:120])
    print("sample edge:", repr(edges[0])[:120])

    labels = {nid(n): nlabel(n).lower() for n in nodes}
    adj = {}
    for e in edges:
        a, b = edge_ends(e)
        adj.setdefault(a, []).append(b); adj.setdefault(b, []).append(a)

    redis_ids = [i for i, lab in labels.items() if "eu-west-redis" in lab or "redis" in lab]
    # BFS from redis, collect which service keywords are reachable
    seen, q = set(redis_ids), deque(redis_ids)
    reached = set()
    while q:
        cur = q.popleft()
        lab = labels.get(cur, "")
        for svc in SERVICES:
            if svc in lab: reached.add(svc)
        for nb in adj.get(cur, ()):
            if nb not in seen: seen.add(nb); q.append(nb)

    print("\n===== GRAPH (traversal from eu-west-redis) =====")
    print("  services reachable:", sorted(reached), f"-> {len(reached)}/5")

    rag = s(await cognee.search(query_text=Q, query_type=SearchType.RAG_COMPLETION, top_k=10)).lower()
    rag_hits = sorted([svc for svc in SERVICES if svc in rag])
    print("\n===== RAG_COMPLETION (top_k=10) =====")
    print("  answer:", rag[:200])
    print("  services named:", rag_hits, f"-> {len(rag_hits)}/5")

    gc = s(await cognee.search(query_text=Q, query_type=SearchType.GRAPH_COMPLETION)).lower()
    gc_hits = sorted([svc for svc in SERVICES if svc in gc])
    print("\n===== GRAPH_COMPLETION =====")
    print("  answer:", gc[:200])
    print("  services named:", gc_hits, f"-> {len(gc_hits)}/5")

    print("\n===== VERDICT =====")
    print(f"  graph traversal completeness: {len(reached)}/5")
    print(f"  RAG completeness:             {len(rag_hits)}/5")
    print(f"  >>> graph beats RAG on completeness? {len(reached) > len(rag_hits)}")

asyncio.run(main())
