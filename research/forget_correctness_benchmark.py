"""forget_correctness_benchmark.py — a REAL, non-circular measurement that forgetting improves answer
CORRECTNESS (not just that a deleted word disappears).

WHY THIS REPLACES forget_benchmark.py: the old metric scored a "leak" as the substring "legacy-cache"
in the answer — so deleting the legacy-cache docs trivially drove it to 0. That proved "deletion deletes
a word," not "the advice got better." This benchmark fixes all three holes the critique named:
  1. NON-CIRCULAR METRIC: an INDEPENDENT blind LLM judge (a DIFFERENT model — Gemini) scores each answer
     0/1/2 for CORRECTNESS against the known-correct CURRENT guidance. It never sees before/after labels
     and never scores word-presence. The system-under-test answers with the demo model (Groq).
  2. NON-TOY CORPUS: ~12 systems / ~18 docs with THREE decommissions and planted stale cross-references,
     where the correct answer genuinely CHANGES (stale path -> current path), not just loses a token.
  3. SURGICAL CONTROL: control questions whose correct answer must NOT change — judged the same way.

Each stale-sensitive question has ONE ground-truth correct answer (the current-state truth). BEFORE the
forget, the stale runbook misleads the system -> it should score LOW. AFTER the forget, it should score
HIGH. The delta in *judged correctness* is the real result. Doubles as a SCALE stress-test.

SAFE: ingests into an ISOLATED dataset ("bench_scale"), never main_dataset. Judge uses Gemini via env
(GEMINI_TEST_KEY) so no key is written to disk. The runner restores golden (reset_demo) afterward.
Run from repo root:  GEMINI_TEST_KEY=... ./.venv/bin/python research/forget_correctness_benchmark.py
"""
import os, sys, json, asyncio, datetime, random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import incident_brain as ib  # the product's exact config (CACHING=false, Groq, etc.)
import litellm
litellm.suppress_debug_info = True
from cognee.modules.users.methods import get_default_user
import cognee
from cognee import SearchType

DATASET = "bench_scale"            # isolated from the golden main_dataset
DECOMMISSION = ["legacy-cache", "email-relay", "image-resizer", "old-logger", "ftp-dropzone", "cron-billing"]

# Optional system-under-test LLM override (cognee reads .env, so OS env does NOT override — must use the
# programmatic config setters, same as the app's BYO-key path). Used when Groq's daily cap is hit: point the
# SYSTEM at NVIDIA's llama-3.3-70b (the SAME model the demo runs) while the JUDGE stays Gemini (cross-family).
_SYS_MODEL = os.environ.get("BENCH_SYS_MODEL")
if _SYS_MODEL:
    cognee.config.set_llm_provider("custom")
    cognee.config.set_llm_model(_SYS_MODEL)
    if os.environ.get("BENCH_SYS_ENDPOINT"):
        cognee.config.set_llm_endpoint(os.environ["BENCH_SYS_ENDPOINT"])
    if os.environ.get("BENCH_SYS_KEY"):
        cognee.config.set_llm_api_key(os.environ["BENCH_SYS_KEY"])

# ~12 systems / 18 docs. Three decommissioned systems each have a STALE runbook that wrongly recommends
# them, plus a CURRENT replacement system documented separately -> after forget, the correct answer shifts.
CORPUS = [
    ("api-gateway", "Runbook: api-gateway. The api-gateway routes all storefront traffic to backend services and enforces rate limits. On 5xx spikes, check upstream health and recent deploys."),
    ("auth-service", "Runbook: auth-service. The auth-service validates login tokens and is required by the payments-service. It reads session state from the primary session-store."),
    ("session-store", "Runbook: session-store. The session-store is the primary store for user session state; the auth-service reads session state directly from it. If auth-service latency is high, check the session-store connection pool and its hit rate."),
    ("legacy-cache", "Runbook: legacy-cache (memcached). When auth-service latency is high, the first thing to check is the legacy-cache: flush and resize the legacy-cache cluster to recover. The legacy-cache sits in front of the auth-service session reads."),
    ("legacy-cache", "Post-mortem 2024-05: a legacy-cache memory-eviction storm caused a platform-wide login outage; we flushed and resized the legacy-cache to recover."),
    ("payments-service", "Runbook: payments-service. The payments-service charges customers and calls the auth-service to validate each login token first. It connects to the payments-db via a bounded connection pool."),
    ("payments-db", "Runbook: payments-db. The payments-db stores transactions for the payments-service; on connection exhaustion, check the bounded pool size and long-running queries."),
    ("search-index", "Runbook: search-index. The search-index powers product search and is independent of login and payments."),
    ("notification-service", "Runbook: notification-service. The notification-service sends customer emails and SMS. It delivers all email through the cloud-mailer. On delivery failures, check the cloud-mailer status and the notification queue depth."),
    ("cloud-mailer", "Runbook: cloud-mailer. The cloud-mailer is the current email delivery provider for the notification-service. Check its API status page and bounce rate when emails are not being delivered."),
    ("email-relay", "Runbook: email-relay. When notifications are failing to send, restart the email-relay daemon and clear its outbound spool; the email-relay handles all outgoing platform email."),
    ("cdn", "Runbook: cdn. The cdn serves all static assets and product images to the storefront. On cache-miss storms, check origin health."),
    ("cdn-transform", "Runbook: cdn-transform. The cdn-transform service resizes and optimizes product images at the edge for the cdn. If product images load slowly or look wrong, check the cdn-transform edge workers and their queue."),
    ("image-resizer", "Runbook: image-resizer. If product images are loading slowly, check the image-resizer: it resizes product images before they are served. Restart the image-resizer workers to clear a backlog."),
    ("ownership", "Ownership: api-gateway and auth-service are owned by the core-platform team; payments-service by the payments team; search-index by the discovery team; notification-service by the growth team."),
    ("monitoring", "Runbook: monitoring. The monitoring stack scrapes metrics from every service; if dashboards go blank, check the metrics collector and remote-write endpoint."),
    ("deploy-pipeline", "Runbook: deploy-pipeline. The deploy-pipeline ships services to production; on a bad rollout, roll back to the previous release and check recent deploys."),
    ("rate-limiter", "Runbook: rate-limiter. The rate-limiter enforces per-client quotas at the api-gateway edge; on 429 spikes, check client quota config."),
    # --- 2026-07-04 scale-up: three MORE decommissioned systems (stale runbook + post-mortem) each with a
    # documented CURRENT replacement, so the correct answer genuinely shifts after forget. n=3 -> n=6.
    ("old-logger", "Runbook: old-logger. When application logs stop appearing, restart the old-logger shipper daemon on each host and clear its journal backlog; the old-logger ships every service's logs."),
    ("old-logger", "Post-mortem 2024-11: the old-logger shipper daemon wedged on a full journal and we lost four hours of logs; we restarted the old-logger fleet to recover."),
    ("log-pipeline", "Runbook: log-pipeline. The log-pipeline collector is the current path for all service logs. If logs stop appearing, check the log-pipeline collector intake lag and its dead-letter queue."),
    ("ftp-dropzone", "Runbook: ftp-dropzone. When partner file uploads are missing, check the ftp-dropzone: restart the ftp-dropzone daemon and re-scan its incoming directory; all partner files arrive via the ftp-dropzone."),
    ("s3-ingest", "Runbook: s3-ingest. Partner file uploads arrive through the s3-ingest bucket events. If partner files are missing, check the s3-ingest event notifications and the ingest queue depth."),
    ("cron-billing", "Runbook: cron-billing. If nightly invoices did not generate, re-run the cron-billing job on the billing host and check its crontab entry; cron-billing produces all customer invoices."),
    ("cron-billing", "Post-mortem 2025-02: cron-billing silently skipped a night after a timezone change on the billing host; invoices were regenerated by re-running the cron-billing job."),
    ("billing-scheduler", "Runbook: billing-scheduler. The billing-scheduler service generates all customer invoices on its nightly schedule. If invoices are missing, check the billing-scheduler run history and its failed-jobs queue."),
    ("data-warehouse", "Runbook: data-warehouse. The data-warehouse stores analytics tables; if dashboards show stale numbers, check the nightly load job status."),
]

# kind: "stale" = correct answer shifts from a decommissioned system to the current one after forget.
# "control" = correct answer must NOT change. `truth` = the known-correct CURRENT guidance (the judge's
# rubric). `trap` (stale only) = the decommissioned system the system might wrongly recommend before forget.
QUESTIONS = [
    {"q": "The auth-service latency is high — what should I check first?", "kind": "stale",
     "truth": "Check the primary session-store (its connection pool / hit rate), since the auth-service reads session state directly from the session-store.", "trap": "legacy-cache"},
    {"q": "Customer notification emails are failing to send — what should I check?", "kind": "stale",
     "truth": "Check the cloud-mailer (its API status / bounce rate), since the notification-service delivers email through the cloud-mailer.", "trap": "email-relay"},
    {"q": "Product images are loading slowly on the storefront — what should I check?", "kind": "stale",
     "truth": "Check the cdn-transform edge workers (and the cdn), since cdn-transform resizes/optimizes product images at the edge.", "trap": "image-resizer"},
    {"q": "What is in the auth-service read path for session state?", "kind": "stale",
     "truth": "The auth-service reads session state from the primary session-store.", "trap": "legacy-cache"},
    {"q": "How does the platform deliver outgoing customer email?", "kind": "stale",
     "truth": "Through the cloud-mailer, which is the notification-service's email delivery provider.", "trap": "email-relay"},
    {"q": "What service resizes and optimizes product images?", "kind": "stale",
     "truth": "The cdn-transform service resizes/optimizes product images at the edge for the cdn.", "trap": "image-resizer"},
    {"q": "Who owns the payments-service?", "kind": "control",
     "truth": "The payments team owns the payments-service.", "trap": None},
    {"q": "What must the payments-service call to validate a login token?", "kind": "control",
     "truth": "The auth-service (the payments-service calls auth-service to validate login tokens).", "trap": None},
    {"q": "What powers product search?", "kind": "control",
     "truth": "The search-index powers product search.", "trap": None},
    {"q": "What does the api-gateway do?", "kind": "control",
     "truth": "The api-gateway routes storefront traffic to backend services and enforces rate limits.", "trap": None},
    # ABSTENTION (C04): after forgetting these systems, asking about them should correctly say "not documented".
    # Judged with the OPPOSITE-polarity rubric (judge_abstain): BEFORE forget they ARE documented (-> abstain≈0,
    # the assistant describes them); AFTER forget they should abstain (-> abstain≈2). The rise is the 2nd number.
    {"q": "Tell me about the legacy-cache system — what is it and what should I check for it?", "kind": "abstention", "truth": None, "trap": "legacy-cache"},
    {"q": "Tell me about the email-relay system — what is it and what should I check for it?", "kind": "abstention", "truth": None, "trap": "email-relay"},
    {"q": "Tell me about the image-resizer system — what is it and what should I check for it?", "kind": "abstention", "truth": None, "trap": "image-resizer"},
    # --- 2026-07-04 scale-up: questions for the three new decommissions ---
    {"q": "Application logs stopped appearing — what should I check?", "kind": "stale",
     "truth": "Check the log-pipeline collector (its intake lag / dead-letter queue), since the log-pipeline is the current path for all service logs.", "trap": "old-logger"},
    {"q": "Partner file uploads are missing — what should I check?", "kind": "stale",
     "truth": "Check the s3-ingest bucket event notifications and the ingest queue depth, since partner files arrive through s3-ingest.", "trap": "ftp-dropzone"},
    {"q": "Nightly customer invoices did not generate — what should I check?", "kind": "stale",
     "truth": "Check the billing-scheduler run history and its failed-jobs queue, since the billing-scheduler generates all customer invoices.", "trap": "cron-billing"},
    {"q": "Where do analytics dashboards read their tables from?", "kind": "control",
     "truth": "The data-warehouse stores the analytics tables (check its nightly load job when numbers are stale).", "trap": None},
    {"q": "Tell me about the old-logger system — what is it and what should I check for it?", "kind": "abstention", "truth": None, "trap": "old-logger"},
    {"q": "Tell me about the ftp-dropzone system — what is it and what should I check for it?", "kind": "abstention", "truth": None, "trap": "ftp-dropzone"},
    {"q": "Tell me about the cron-billing system — what is it and what should I check for it?", "kind": "abstention", "truth": None, "trap": "cron-billing"},
]

# ---- independent blind judge (a DIFFERENT model family from the system under test), with key rotation ----
# Default judge = Gemini (GEMINI_TEST_KEYS / GEMINI_TEST_KEY). Overridable via BENCH_JUDGE_MODEL /
# BENCH_JUDGE_ENDPOINT / BENCH_JUDGE_KEYS — used 2026-07-04 with judge=openai/gpt-oss-120b on Cerebras while
# the system under test ran openai/zai-glm-4.7 (the live demo's model): different families, judge stays blind.
JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "openai/gemini-2.5-flash")
JUDGE_ENDPOINT = os.environ.get("BENCH_JUDGE_ENDPOINT", "https://generativelanguage.googleapis.com/v1beta/openai/")
_GKEYS = [k.strip() for k in os.environ.get("BENCH_JUDGE_KEYS", os.environ.get("GEMINI_TEST_KEYS", "")).split(",") if k.strip()]
if not _GKEYS and os.environ.get("GEMINI_TEST_KEY"):
    _GKEYS = [os.environ["GEMINI_TEST_KEY"].strip()]
_kidx = [0]


def _next_key():
    k = _GKEYS[_kidx[0] % len(_GKEYS)]
    _kidx[0] += 1
    return k


# Rubric A — correctness vs known-correct CURRENT guidance (stale/control). "Not documented" is WRONG here
# because there IS a correct current answer.
JUDGE_SYS = (
    "You are a strict grader of an on-call assistant's answer. You are given the QUESTION, the KNOWN-CORRECT "
    "current guidance, and the ASSISTANT'S ANSWER. Score how correct the assistant's answer is versus the "
    "known-correct guidance:\n"
    "0 = wrong, contradicts it, recommends a decommissioned/incorrect system, or says it is not documented;\n"
    "1 = partially correct or vague (mentions the right area but misses the key system/action);\n"
    "2 = correct (matches the known-correct guidance, names the right system/action).\n"
    "Judge ONLY correctness against the known-correct guidance. Reply with ONLY the single digit 0, 1, or 2."
)

# Rubric B — ABSTENTION (C04): the OPPOSITE polarity. Here "not documented" is the CORRECT answer (the
# system was decommissioned), so the old Rubric A would mis-score it 0. This rubric scores abstention right.
JUDGE_ABSTAIN = (
    "You are grading whether an on-call assistant correctly ABSTAINED about a system it was asked about. "
    "Score ONLY the ASSISTANT'S ANSWER:\n"
    "2 = it clearly says the system is NOT documented / not found / unknown / decommissioned (a correct abstention);\n"
    "0 = it describes the system or recommends checking/flushing/restarting it (it treated the system as documented).\n"
    "Reply with ONLY the single digit 0 or 2."
)


async def _gemini(messages, valid="012"):
    if not _GKEYS:
        return None
    try:
        r = await asyncio.wait_for(litellm.acompletion(
            model=JUDGE_MODEL,
            api_base=JUDGE_ENDPOINT,
            api_key=_next_key(), temperature=0, max_tokens=256,  # thinking models need output headroom
            messages=messages,
        ), timeout=40)
        txt = (r.choices[0].message.content or "").strip()
        for ch in txt:
            if ch in valid:
                return int(ch)
        return None
    except Exception as e:
        return f"ERR:{e!r}"


async def judge(q, truth, answer):
    return await _gemini([{"role": "system", "content": JUDGE_SYS},
                          {"role": "user", "content": f"QUESTION: {q}\nKNOWN-CORRECT GUIDANCE: {truth}\nASSISTANT'S ANSWER: {answer}"}])


async def judge_abstain(q, answer):
    return await _gemini([{"role": "system", "content": JUDGE_ABSTAIN},
                          {"role": "user", "content": f"QUESTION: {q}\nASSISTANT'S ANSWER: {answer}"}], valid="02")


async def ingest_corpus():
    """Batched add+cognify: a 27-doc single cognify slams free-tier rate limits and docs silently drop
    (caught 2026-07-04: half the corpus had NO chunks before forget → garbage run). Small batches with
    recovery pauses keep every doc inside the provider's rate window."""
    ledger = {}
    BATCH = int(os.environ.get("BENCH_INGEST_BATCH", "5"))
    PAUSE = float(os.environ.get("BENCH_INGEST_PAUSE", "25"))
    docs = list(CORPUS)
    for i in range(0, len(docs), BATCH):
        for system, text in docs[i:i + BATCH]:
            r = await cognee.add(text, dataset_name=DATASET)
            info = getattr(r, "data_ingestion_info", None) or []
            did = info[0].get("data_id") if info and isinstance(info[0], dict) else None
            ledger.setdefault(system, []).append(str(did) if did else None)
        await cognee.cognify(datasets=[DATASET])
        print(f"  cognified batch {i // BATCH + 1}/{(len(docs) + BATCH - 1) // BATCH}")
        if i + BATCH < len(docs):
            await asyncio.sleep(PAUSE)
    return ledger


# ---- C03: retrieval-layer forget proof (SearchType.CHUNKS) — deterministic, NO LLM, NO Gemini quota ----
# After a forget, the forgotten doc's OWN chunk must be gone from the vector index — strictly stronger than
# the answer-phrasing check. Match a DISTINCTIVE phrase from each forgotten runbook so a surviving doc that
# merely MENTIONS the dead system does not false-positive (the trap the C03 probe caught).
RETRIEVAL_PROBE = {
    "legacy-cache": ["flush and resize the legacy-cache", "memory-eviction storm"],
    "email-relay": ["restart the email-relay daemon"],
    "image-resizer": ["restart the image-resizer workers"],
    "old-logger": ["restart the old-logger shipper daemon", "wedged on a full journal"],
    "ftp-dropzone": ["restart the ftp-dropzone daemon"],
    "cron-billing": ["re-run the cron-billing job", "silently skipped a night"],
}


async def _chunk_texts(user, query, top_k=10):
    res = await cognee.search(query_text=query, query_type=SearchType.CHUNKS, datasets=[DATASET], user=user, top_k=top_k)
    chunks = []
    for w in (res or []):
        if isinstance(w, dict) and "search_result" in w:
            chunks.extend(w.get("search_result") or [])
    return [(c.get("text") or "") for c in chunks if isinstance(c, dict)]


async def retrieval_presence(user):
    """For each decommissioned system: does its OWN runbook chunk survive a CHUNKS query? (distinctive-phrase
    match, queried by the system's own name to maximize retrieval if the chunk still exists)."""
    out = {}
    for sysname, phrases in RETRIEVAL_PROBE.items():
        texts = await _chunk_texts(user, sysname)
        out[sysname] = any(p.lower() in t.lower() for t in texts for p in phrases)
    return out


_ASK_TIMEOUT = 45   # bound EVERY call so a rate-limited retry-storm can never hang the run (the 35-min bug)
_THROTTLE = float(os.environ.get("BENCH_THROTTLE", "5"))   # seconds between calls (lower for fast providers)


async def run_phase(user, label):
    rows = []
    for item in QUESTIONS:
        ans = None
        for attempt in (1, 2):                      # one retry after a cool-down — a single rate-limit blip
            try:                                    # must not zero a question (caught 2026-07-04)
                ans = await asyncio.wait_for(ib.ask(item["q"], user, dataset=DATASET), timeout=_ASK_TIMEOUT)
                break
            except asyncio.TimeoutError:
                ans = "<ASK TIMEOUT — model rate-limited; aborted fast instead of retrying>"
                if attempt == 1:
                    await asyncio.sleep(30)
            except Exception as e:
                ans = f"<ASK ERR: {e!r}>"
                if attempt == 1:
                    await asyncio.sleep(30)
        await asyncio.sleep(_THROTTLE)
        if item["kind"] == "abstention":
            score = await judge_abstain(item["q"], ans)        # C04: opposite-polarity rubric
        else:
            score = await judge(item["q"], item["truth"], ans)
        await asyncio.sleep(_THROTTLE)
        rows.append({"q": item["q"], "kind": item["kind"], "trap": item.get("trap"),
                     "answer": ans, "score": score})
        sc = score if isinstance(score, int) else "?"
        print(f"  [{label}] ({item['kind'][:1]}) score={sc}  Q: {item['q'][:54]}")
        print(f"           A: {str(ans)[:150]}")
    return rows


def avg(rows, kind):
    xs = [r["score"] for r in rows if r["kind"] == kind and isinstance(r["score"], int)]
    return (sum(xs) / len(xs)) if xs else None


async def main():
    if not _GKEYS:
        raise SystemExit("Set GEMINI_TEST_KEYS (comma-separated) or GEMINI_TEST_KEY (the independent judge model). Aborting — won't run a no-judge benchmark.")
    user = await get_default_user()
    print("=" * 92)
    print("FORGET CORRECTNESS BENCHMARK — independent blind judge scores CORRECTNESS (not word-presence)")
    print(f"corpus: {len(CORPUS)} docs / {len(set(s for s,_ in CORPUS))} systems; decommission: {DECOMMISSION}")
    print("=" * 92)

    print("\nIngesting the scaled corpus into isolated dataset 'bench_scale' (golden untouched)...")
    ledger = await ingest_corpus()
    print("  systems:", list(ledger.keys()))

    print("\n--- BEFORE forget ---")
    before = await run_phase(user, "BEFORE")

    ret_before = await retrieval_presence(user)   # C03: forgotten docs' chunks should be PRESENT now

    print(f"\n>>> Decommissioning {DECOMMISSION} ...")
    total = 0
    for sysname in DECOMMISSION:
        n = await ib.forget_system(sysname, ledger, dataset=DATASET)
        total += n
        print(f"    forgot {n} doc(s) for {sysname}")

    ret_after = await retrieval_presence(user)    # C03: ... and ABSENT after forget

    print("\n--- AFTER forget ---")
    after = await run_phase(user, "AFTER ")

    sb, sa = avg(before, "stale"), avg(after, "stale")
    cb, ca = avg(before, "control"), avg(after, "control")
    ab, aa = avg(before, "abstention"), avg(after, "abstention")        # C04
    retrieval_proof = [{"system": s, "own_chunk_before": ret_before.get(s), "own_chunk_after": ret_after.get(s)}
                       for s in DECOMMISSION]                            # C03
    retrieval_pass = all(ret_before.get(s) and not ret_after.get(s) for s in DECOMMISSION)
    results = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "system_model": _SYS_MODEL or os.environ.get("LLM_MODEL"), "judge_model": JUDGE_MODEL.split("/")[-1],
        "corpus_docs": len(CORPUS), "systems": len(set(s for s, _ in CORPUS)), "decommissioned": DECOMMISSION,
        "docs_forgotten": total,
        "stale_correctness_before": sb, "stale_correctness_after": sa,
        "control_correctness_before": cb, "control_correctness_after": ca,
        "abstention_correctness_before": ab, "abstention_correctness_after": aa,   # C04: 2nd clean number
        "retrieval_proof": retrieval_proof, "retrieval_proof_pass": retrieval_pass, # C03: deletion at the index
        "before": before, "after": after,
    }
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forget_correctness_results.json")
    json.dump(results, open(out, "w"), indent=2)

    print("\n" + "=" * 92)
    print(f"RESULT (judged correctness, 0=wrong .. 2=correct; independent {JUDGE_MODEL.split('/')[-1]} judge, blind to phase)")
    print("=" * 92)
    fmt = lambda x: f"{x:.2f}" if isinstance(x, float) else "n/a"
    print(f"  UPDATE correctness (stale Qs) : {fmt(sb)}  BEFORE  ->  {fmt(sa)}  AFTER   (RISE — forgetting fixes stale advice)")
    print(f"  ABSTENTION correctness        : {fmt(ab)}  BEFORE  ->  {fmt(aa)}  AFTER   (RISE — after forget it correctly says 'not documented')")
    print(f"  CONTROL correctness           : {fmt(cb)}  BEFORE  ->  {fmt(ca)}  AFTER   (STAY high — forget is surgical)")
    if isinstance(sb, float) and isinstance(sa, float):
        print(f"  -> update correctness improved by {fmt(sa - sb)} pts on a 0-2 scale across {len([r for r in before if r['kind']=='stale'])} questions / {len(DECOMMISSION)} decommissions.")
    print(f"\n  RETRIEVAL-LAYER forget proof  : {'PASS' if retrieval_pass else 'FAIL'}  (each forgotten doc's own chunk is gone from the vector index)")
    for rp in retrieval_proof:
        print(f"      {rp['system']:<14} own chunk in index:  before={rp['own_chunk_before']}  after={rp['own_chunk_after']}")
    print(f"\n  full per-question answers + scores -> {out}")
    print("  DESTRUCTIVE on dataset 'bench_scale' only; restore golden now:  ./.venv/bin/python reset_demo.py")


if __name__ == "__main__":
    asyncio.run(main())
