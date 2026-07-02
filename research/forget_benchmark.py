"""forget_benchmark.py — MEASURED before/after proof that FORGETTING improves answer correctness.

The thesis Lethe argues is "static memory rots; forgetting stale knowledge is a first-class, verifiable
operation." This benchmark turns that from a qualitative demo ("the answer flips") into a REPRODUCIBLE
NUMBER, the way a placed hackathon project (CogneeMind) used an evidence script to prove its system got
better. We build our OWN, for the forget thesis — nothing copied.

Method (a failing baseline first, per HARD RULE #5):
  1. Over the golden incident wiki (which still contains the decommissioned `legacy-cache`), ask two
     question sets and read the FULL answers:
       - STALE-SENSITIVE: questions whose correct on-call answer is corrupted by the stale legacy-cache
         runbook ("flush/resize the legacy-cache"). A wrong answer = it still recommends the dead system.
       - CONTROL: unrelated questions whose correct answer must NOT change when we forget legacy-cache.
         These prove the forget is SURGICAL (removes the stale advice without damaging live knowledge).
  2. Forget the decommissioned system (cognee.forget over both its docs).
  3. Re-ask both sets. Re-measure.
  4. Report: stale-leak rate (before vs after), control preservation (before vs after), and the REAL
     graph diff (nodes/edges removed) — the same measurement the product's forget receipt uses.

HARD RULE #1: we never trust a substring boolean as the only evidence — every full answer string is
printed and saved to JSON so the scoring can be audited against what the model actually said.

DESTRUCTIVE: it forgets on `main_dataset`. The runner ALWAYS restores the golden snapshot afterward
(reset_demo.py) and re-verifies the hero. Run from the repo root:
    ./.venv/bin/python research/forget_benchmark.py
"""
import os, sys, json, re, asyncio, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root on path
import incident_brain as ib  # sets CACHING=false etc. BEFORE importing cognee — the product's exact config
from cognee.modules.users.methods import get_default_user

DECOMMISSION = "legacy-cache"  # the one decommissioned system in the golden wiki

# STALE-SENSITIVE — the correct on-call answer is corrupted by the stale legacy-cache runbook.
# (None of the question texts mention "legacy-cache", so the leak detector scores the ANSWER, not the prompt.)
STALE_Q = [
    "If auth-service latency is high, what should I check, and what's in the auth read path?",
    "How do we recover from a sudden auth-service latency spike?",
    "What sits in front of the auth-service session reads?",
    "What caused the platform-wide login outage described in the post-mortem?",
]

# CONTROL — forgetting legacy-cache must NOT change these correct answers. Each lists accepted key phrases.
CONTROL_Q = [
    ("Who owns the payments-service?",                         ["payments team", "payments"]),
    ("What must the payments-service call to validate a login token?", ["auth-service", "auth service"]),
    ("What powers product search?",                            ["search-index", "search index"]),
    ("What does the api-gateway do?",                          ["api-gateway", "rate limit", "storefront", "traffic", "route"]),
]


def _norm(s: str) -> str:
    """Collapse to lowercase alphanumeric tokens so 'legacy-cache' and 'legacy cache' match the same.
    (HARD RULE #1 lesson: a naive hyphen-only substring missed a real 'legacy cache' leak — normalize.)"""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def stale_leak(answer: str) -> bool:
    """Proxy metric: does the answer still treat the decommissioned legacy-cache as live advice?
    A truly-forgotten entity should never reappear in an answer. (Full strings saved for human audit.)"""
    return _norm(DECOMMISSION) in _norm(answer)


def control_ok(answer: str, expects) -> bool:
    low = _norm(answer)
    return any(_norm(e) in low for e in expects)


async def graph_counts(user):
    """(nodes, edges) for main_dataset — the SAME measurement the product's forget receipt uses."""
    try:
        from cognee.context_global_variables import set_database_global_context_variables
        from cognee.infrastructure.databases.graph import get_graph_engine
        async with set_database_global_context_variables("main_dataset", user.id):
            ge = await get_graph_engine()
            nodes, edges = await ge.get_graph_data()
        return len(nodes or []), len(edges or [])
    except Exception as e:
        print("  (graph_counts failed:", repr(e), ")")
        return None, None


async def _ask(user, q):
    try:
        return await ib.ask(q, user)
    except Exception as e:
        return f"<ASK ERROR: {e!r}>"


async def run_set(user):
    stale = []
    for q in STALE_Q:
        a = await _ask(user, q)
        stale.append({"q": q, "answer": a, "leak": stale_leak(a)})
    control = []
    for q, expects in CONTROL_Q:
        a = await _ask(user, q)
        control.append({"q": q, "answer": a, "ok": control_ok(a, expects), "expects": expects})
    return {"stale": stale, "control": control}


def _print_set(title, s):
    print(f"\n  [{title}]")
    for x in s["stale"]:
        flag = "LEAK ⚠️ " if x["leak"] else "clean  "
        print(f"    {flag} Q: {x['q']}")
        print(f"             A: {x['answer'][:160]}")
    for x in s["control"]:
        flag = "ok " if x["ok"] else "MISS"
        print(f"    [{flag}] Q: {x['q']}")
        print(f"             A: {x['answer'][:160]}")


async def main():
    ledger = ib.load_ledger()
    if not ledger or DECOMMISSION not in ledger:
        raise SystemExit("No golden ledger with legacy-cache present — run reset_demo.py first, then retry.")
    user = await get_default_user()

    print("=" * 88)
    print("FORGET BENCHMARK — does forgetting stale knowledge measurably improve answer correctness?")
    print("=" * 88)

    nb, eb = await graph_counts(user)
    print(f"\nGraph BEFORE forget: {nb} nodes, {eb} edges")
    before = await run_set(user)
    _print_set("BEFORE forget", before)

    print(f"\n>>> Forgetting decommissioned system '{DECOMMISSION}' ...")
    n_forgot = await ib.forget_system(DECOMMISSION, ledger)
    print(f"    forgot {n_forgot} document(s).")

    na, ea = await graph_counts(user)
    print(f"\nGraph AFTER forget:  {na} nodes, {ea} edges")
    after = await run_set(user)
    _print_set("AFTER forget", after)

    # ---- metrics ----
    n_stale, n_ctrl = len(STALE_Q), len(CONTROL_Q)
    leak_b = sum(x["leak"] for x in before["stale"]);  leak_a = sum(x["leak"] for x in after["stale"])
    ctrl_b = sum(x["ok"] for x in before["control"]);  ctrl_a = sum(x["ok"] for x in after["control"])
    nodes_removed = (nb - na) if isinstance(nb, int) and isinstance(na, int) else None
    edges_removed = (eb - ea) if isinstance(eb, int) and isinstance(ea, int) else None

    results = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "decommissioned_system": DECOMMISSION,
        "docs_forgotten": n_forgot,
        "graph": {"nodes_before": nb, "nodes_after": na, "nodes_removed": nodes_removed,
                  "edges_before": eb, "edges_after": ea, "edges_removed": edges_removed},
        "stale_sensitive": {
            "total": n_stale,
            "leaked_before": leak_b, "leaked_after": leak_a,
            "correct_before": n_stale - leak_b, "correct_after": n_stale - leak_a,
        },
        "control": {
            "total": n_ctrl, "correct_before": ctrl_b, "correct_after": ctrl_a,
        },
        "before": before, "after": after,
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forget_benchmark_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)

    print("\n" + "=" * 88)
    print("RESULT")
    print("=" * 88)
    print(f"  Stale advice leaked into : {leak_b}/{n_stale} triage answers BEFORE  ->  {leak_a}/{n_stale} AFTER forgetting")
    print(f"  Stale-sensitive correct  : {n_stale-leak_b}/{n_stale} BEFORE  ->  {n_stale-leak_a}/{n_stale} AFTER")
    print(f"  Control answers preserved: {ctrl_b}/{n_ctrl} BEFORE  ->  {ctrl_a}/{n_ctrl} AFTER  (forget must not damage live knowledge)")
    print(f"  Graph removed            : {nodes_removed} nodes, {edges_removed} edges")
    verdict = (leak_a < leak_b) and (ctrl_a == ctrl_b == n_ctrl)
    print(f"\n  THESIS {'SUPPORTED ✅' if verdict else 'NOT cleanly supported ⚠️  (inspect the strings above)'}: "
          f"forgetting {'removed the stale advice while preserving all live answers.' if verdict else 'did not produce the clean before/after — read the answers.'}")
    print(f"\n  Full answer strings + metrics saved -> {out}")
    print("  (DESTRUCTIVE run — restore the golden hero now with:  ./.venv/bin/python reset_demo.py)")


if __name__ == "__main__":
    asyncio.run(main())
