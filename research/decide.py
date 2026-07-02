"""THE DECIDING RUN.

1. Build the graph from 16 docs (prose, consistent entity names).
2. REACHABILITY: from the node the question lands on (Auth/Redis), can we actually
   traverse the graph all the way to Sana? (edge-existence is not enough; reachability is.)
3. Only if reachable, the A/B is meaningful: RAG (top_k=10, real default) vs GRAPH.

Landing spots (both fine, per the agreed line):
  - reachable AND graph names Sana while RAG can't  -> ship the demo.
  - reachable BUT graph still ties/loses           -> honest: multi-hop isn't the hero here.
  - NOT reachable                                   -> the graph still isn't being built; report it plainly.
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
Q = ("Tonight checkout is failing and users report flaky logins. "
     "Name the single on-call engineer I should page right now.")

def nid(n):
    v = getattr(n, "id", None)
    if v is None and isinstance(n, dict): v = n.get("id")
    return str(v)

def nlabel(n):
    for a in ("name", "text", "description"):
        v = getattr(n, a, None)
        if v is None and isinstance(n, dict): v = n.get(a)
        if v: return str(v)
    return str(n)

def edge_ends(e):
    if isinstance(e, (list, tuple)):
        return str(e[0]), str(e[1])
    return str(getattr(e, "source_node_id", "")), str(getattr(e, "target_node_id", ""))

def find(nodes, *kw):
    out = []
    for n in nodes:
        lab = nlabel(n).lower()
        if any(k.lower() in lab for k in kw):
            out.append((nid(n), nlabel(n)[:50]))
    return out

def bfs(adj, starts, goal_ids):
    seen, q = set(starts), deque([(s, [s]) for s in starts])
    while q:
        cur, path = q.popleft()
        if cur in goal_ids:
            return path
        for nb in adj.get(cur, ()):
            if nb not in seen:
                seen.add(nb); q.append((nb, path + [nb]))
    return None

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
    print(f"\nGRAPH: {len(nodes)} nodes, {len(edges)} edges")

    labels = {nid(n): nlabel(n) for n in nodes}
    adj = {}
    for e in edges:
        a, b = edge_ends(e)
        adj.setdefault(a, []).append(b); adj.setdefault(b, []).append(a)  # undirected reachability

    sana = [i for i, _ in find(nodes, "sana")]
    redis = [i for i, _ in find(nodes, "eu-west-redis", "redis")]
    auth = [i for i, _ in find(nodes, "auth")]
    print("nodes ~ 'sana' :", find(nodes, "sana"))
    print("nodes ~ 'redis':", find(nodes, "redis")[:3])
    print("nodes ~ 'platform-reliability':", find(nodes, "platform-reliability")[:3])

    print("\n===== REACHABILITY (the decisive check) =====")
    for name, starts in [("Redis", redis), ("Auth", auth)]:
        path = bfs(adj, starts, set(sana)) if (starts and sana) else None
        if path:
            print(f"  {name} -> Sana: REACHABLE in {len(path)-1} hops:")
            print("    " + " -> ".join(labels.get(p, p)[:28] for p in path))
        else:
            print(f"  {name} -> Sana: NOT reachable")

    print("\n===== A/B (top_k = 10, real default) =====")
    rag_ctx = s(await cognee.search(query_text=Q, query_type=SearchType.RAG_COMPLETION, top_k=10, only_context=True))
    rag_ans = s(await cognee.search(query_text=Q, query_type=SearchType.RAG_COMPLETION, top_k=10))
    g_ans = s(await cognee.search(query_text=Q, query_type=SearchType.GRAPH_COMPLETION))
    print("  RAG saw 'platform-reliability' (the bridge)?", "platform-reliability" in rag_ctx.lower())
    print("  RAG answer:", rag_ans[:160])
    print("  RAG named Sana?", "sana" in rag_ans.lower())
    print("  GRAPH answer:", g_ans[:160])
    print("  GRAPH named Sana?", "sana" in g_ans.lower())

    print("\n===== VERDICT =====")
    reach = bool(bfs(adj, redis, set(sana))) if (redis and sana) else False
    print(f"  chain reachable from Redis: {reach}")
    print(f"  RAG named Sana: {'sana' in rag_ans.lower()} | GRAPH named Sana: {'sana' in g_ans.lower()}")

asyncio.run(main())
