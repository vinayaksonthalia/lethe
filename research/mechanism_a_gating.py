"""Mechanism-A gating experiment — does Cognee's FEEDBACK-weighted retrieval
MEASURABLY change the answer, or rerank invisibly? This decides whether
self-improvement can HEADLINE (native, beats the BFS on "best use of Cognee")
or stays a footnote behind blast-radius.

THE ONE HARD RULE (the rake that's bitten this project twice): self-improvement
can only be SHOWN if there is a FAILING BASELINE to fix. At a small/easy corpus,
un-weighted retrieval already surfaces the right context -> feedback changes
nothing visible -> a NULL result that is a TEST-DESIGN ARTIFACT, not a verdict.
So Phase 0 ABORTS if the baseline already passes.

Config is driven by THIS project's .env (Groq via custom provider for the LLM +
local fastembed embeddings = no embedding quota). API flow verified against the
installed cognee 1.1.3 signatures.
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

# ── Corpus: the answer lives in DOC_CORRECT, but DOC_DECOY is title-similar to
#    the query so un-weighted retrieval may grab it first = the failing baseline.
# CORRECT doc: the truth, but phrased WITHOUT the query's words ("checkout latency
# spiking / likely cause / fix") so it does NOT win on vector similarity.
DOC_CORRECT = """Payments-gateway capacity notes (CURRENT, Payments team).
The payments-gateway holds a bounded connection pool to the payments database. Under
sustained load this pool reaches exhaustion and requests queue, degrading transactions.
Remedy: raise the payments-gateway pool size, and if saturation persists, fail over to
the payments-gateway-replica. Never blind-restart the gateway — it drops in-flight work."""

# DECOY doc: STALE/decommissioned, but it mirrors the query's exact wording so
# un-weighted retrieval grabs it first → the failing baseline. Only feedback (a human
# saying "this source is wrong") can demote it; there is no textual signal to.
DOC_DECOY = """Checkout Latency Spikes — Likely Cause and Fix (ARCHIVED 2023).
When checkout latency is spiking, the likely cause is the legacy-cache service evicting
hot keys, and the fix is to flush and resize legacy-cache. NOTE: legacy-cache was
decommissioned in 2024 and no longer exists in production."""

CORPUS = [DOC_CORRECT, DOC_DECOY]
TEACH = [
    {"query": "What is the current runbook procedure for payments-gateway problems?", "score": 5,
     "note": "Correct current runbook — reward."},
    {"query": "Summarize the archived legacy-cache post-mortem.", "score": 1,
     "note": "Decoy archived/decommissioned source — penalize."},
]
TARGET_QUERY = "Checkout latency is spiking right now. What's the likely cause and the fix?"
BETA_SWEEP = [0.0, 0.3, 0.5, 0.7, 1.0]
SESSION_ID = "mechanism_a_gating"


def score_answer(answer_text: str):
    t = answer_text.lower()
    names_correct = any(k in t for k in ["connection pool", "pool exhaustion", "payments-gateway", "replica"])
    centers_decoy = ("legacy-cache" in t) or ("legacy cache" in t)
    if names_correct and not centers_decoy:
        return True, "names current cause/fix, does not center legacy-cache"
    if centers_decoy and not names_correct:
        return False, "centers the decommissioned legacy-cache (stale/wrong)"
    if not names_correct:
        return False, "does not name the current cause/fix"
    return False, "mixes in the stale legacy-cache answer"


def flat(x):
    if isinstance(x, list):
        return " || ".join(flat(i) for i in x)
    if isinstance(x, dict):
        return flat(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)


async def ask(query, user, feedback_influence=0.0, session_id=None):
    kw = {"session_id": session_id} if session_id else {}
    return flat(await cognee.recall(query_text=query, query_type=SearchType.GRAPH_COMPLETION,
                                    user=user, feedback_influence=feedback_influence, **kw))


async def main():
    print("=== Ingesting corpus (2 docs) ===")
    await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)
    await cognee.remember(CORPUS, self_improvement=False)
    user = await get_default_user()

    print("\n=== Phase 0: FAILING-BASELINE GATE (feedback_influence=0.0) ===")
    base = await ask(TARGET_QUERY, user, feedback_influence=0.0)
    ok, why = score_answer(base)
    print("  answer:", base[:280])
    print(f"  baseline: {'PASS' if ok else 'FAIL'} — {why}")
    if ok:
        print("\n>>> ABORT: baseline already PASSES → corpus too easy. A post-feedback result\n"
              ">>> here would prove NOTHING. Harden the decoy/overlap and rerun. NOT a verdict.")
        return

    print("\n=== Phase 1: teaching feedback (baseline failed → proceed) ===")
    for it in TEACH:
        await ask(it["query"], user, session_id=SESSION_ID)
        sess = await cognee_session.get_session(session_id=SESSION_ID, last_n=1, user=user)
        if not sess:
            print("  !! session empty — recall didn't log the QA; cannot teach. Investigate.")
            return
        await cognee_session.add_feedback(session_id=SESSION_ID, qa_id=sess[0].qa_id,
                                          feedback_score=it["score"], feedback_text=it["note"], user=user)
        print(f"  fed score={it['score']}: {it['query'][:55]}")

    print("\n=== Phase 2: applying feedback weights into the graph ===")
    await apply_feedback_weights_pipeline(user=user, session_ids=[SESSION_ID], alpha=0.9)

    print("\n=== Phase 3: target query across feedback_influence sweep ===")
    first_flip = None
    for beta in BETA_SWEEP:
        ans = await ask(TARGET_QUERY, user, feedback_influence=beta)
        good, r = score_answer(ans)
        print(f"\n  beta={beta:.1f}: {'PASS' if good else 'FAIL'} — {r}")
        print("    ", ans[:200])
        if good and first_flip is None and beta > 0.0:
            first_flip = beta

    print("\n" + "=" * 60)
    if first_flip is not None:
        print(f">>> CLEAN FLIP at beta={first_flip:.1f}: baseline failed, feedback fixed it.\n"
              ">>> Self-improvement is DEMOABLE and NATIVE → it can HEADLINE.")
    else:
        print(">>> NO LIFT: baseline failed and stayed failed across every beta.\n"
              ">>> → blast-radius headlines; self-improvement is a footnote.\n"
              ">>> (First check Phase-1 surfaced the right nodes before calling it dead.)")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
