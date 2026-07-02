"""QA harness — stress-test the app on REAL, larger, varied data (the learning/ docs), not just the
tiny golden set. Creates a temp workspace, ingests several .md files, then exercises every feature and
prints a structured report. Leaves the workspace in place (pass its id to follow-up tests); delete with
  curl -s -XPOST localhost:8077/workspaces/delete -H 'Content-Type: application/json' -d '{"id":"<wid>"}'

Run: ./.venv/bin/python qa_harness.py        (app must be running on :8077, local mode)
"""
import time, json, httpx, sys

B = "http://127.0.0.1:8077"
FILES = [
    ("what-is-lethe", "learning/00-the-big-picture/what-is-this.md"),
    ("static-memory-problem", "learning/00-the-big-picture/the-problem-static-memory-rots.md"),
    ("what-is-cognee", "learning/02-cognee-deep-dive/what-is-cognee.md"),
    ("graph-vs-rag", "learning/02-cognee-deep-dive/why-cognee-not-just-rag.md"),
]
QUERIES = [
    "What is Cognee and what does it do?",
    "Why use a knowledge graph instead of plain RAG?",
    "What is the problem with static memory?",
]
c = httpx.Client(timeout=200)


def jp(r):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:200]}


def main():
    wid = jp(c.post(f"{B}/workspaces", json={"name": "qa-learning"})).get("workspace", {}).get("id")
    print("workspace:", wid)
    if not wid:
        print("FAILED to create workspace"); sys.exit(1)

    # ingest each file (sequential — the ingest lock is global)
    for sysname, path in FILES:
        text = open(path).read()
        up = jp(c.post(f"{B}/upload", json={"text": text, "system": sysname, "filename": path, "workspace": wid}))
        t0 = time.time()
        st = up
        for _ in range(90):
            st = jp(c.get(f"{B}/ingest-status"))
            if st.get("state") in ("done", "error"):
                break
            time.sleep(2)
        print(f"  ingest {sysname:24} {st.get('state'):6} {time.time()-t0:5.0f}s  ({len(text)} bytes)")

    print("\n=== SYSTEMS ===")
    print(json.dumps(jp(c.get(f"{B}/systems", params={"workspace": wid})), indent=1)[:600])

    g = jp(c.get(f"{B}/graph", params={"workspace": wid}))
    print(f"\n=== GRAPH ===  count={g.get('count')}  (golden was 65 nodes / 104 edges)")
    import collections
    types = collections.Counter(n.get("type") for n in g.get("nodes", []))
    print("  node types:", dict(types))
    ents = [n["label"] for n in g.get("nodes", []) if n.get("type") == "Entity"][:14]
    print("  sample entities:", ents)

    print("\n=== TIMELINE ===")
    ev = jp(c.get(f"{B}/timeline", params={"workspace": wid})).get("events", [])
    print("  events:", [(e["op"], e["system"]) for e in ev])

    print("\n=== CURATION (stale + aging) ===")
    print("  stale:", jp(c.get(f"{B}/curation", params={"workspace": wid})))
    print("  aging:", jp(c.get(f"{B}/curation/aging", params={"workspace": wid})))

    print("\n=== TRIAGE on the new corpus ===")
    for q in QUERIES:
        a = jp(c.post(f"{B}/ask", json={"query": q, "history": [], "workspace": wid}))
        print(f"\n  Q: {q}\n  A: {(a.get('answer') or '')[:240]}\n  sources: {a.get('citations')}")

    print("\n=== FORGET one system + receipt ===")
    fg = jp(c.post(f"{B}/forget", json={"system": "graph-vs-rag", "workspace": wid}))
    r = fg.get("receipt", {})
    print("  msg:", fg.get("message"))
    print("  receipt:", {k: r.get(k) for k in ("docs", "nodes_removed", "edges_removed")})
    print("  proof:", (r.get("proof_answer") or "")[:140])

    print("\n=== TIMELINE after forget ===")
    ev = jp(c.get(f"{B}/timeline", params={"workspace": wid})).get("events", [])
    print("  events:", [(e["op"], e["system"]) for e in ev])

    print("\nWORKSPACE LEFT IN PLACE:", wid)


if __name__ == "__main__":
    main()
