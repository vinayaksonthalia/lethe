"""DECISIVE validation: does feedback robustly move the correct-rate at meaningful N
and low temperature? This decides whether self-improvement HEADLINES or cedes to blast-radius.

Clean design: uses the EXISTING graph (one feedback application already applied last run).
  - before: feedback_influence=0  -> IGNORES weights entirely (genuine clean baseline)
  - after:  feedback_influence=0.7 -> uses that one applied weight set (no re-apply, no compounding)
Temperature 0.0 (set in .env LLM_ARGS). Model constant (Groq). No re-cognify.

DECISION RULE (stated before running, held after):
  HEADLINE  = before CORRECT < ~30%  AND after CORRECT > ~70%  (clean, repeatable flip)
  DEMOTE    = small gap, or after still hedges a lot  -> blast-radius headlines, this is a supporting beat
"""
import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType
from cognee.modules.users.methods import get_default_user

TARGET = "API latency is high right now. What's the likely cause and the fix?"
N = 20

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

async def tally(user, fb):
    counts = {"CORRECT": 0, "HEDGE": 0, "WRONG": 0, "?": 0}
    for _ in range(N):
        a = flat(await cognee.recall(query_text=TARGET, query_type=SearchType.GRAPH_COMPLETION,
                                     user=user, feedback_influence=fb))
        counts[verdict(a)] += 1
    return counts

async def main():
    user = await get_default_user()
    print(f"N={N} per condition, temperature=0.0, existing graph (one feedback application applied).\n")
    print("=== BEFORE (feedback_influence=0.0) ===")
    before = await tally(user, 0.0)
    print("  ", before)
    print("\n=== AFTER (feedback_influence=0.7) ===")
    after = await tally(user, 0.7)
    print("  ", after)

    bc, ac = before["CORRECT"], after["CORRECT"]
    print("\n" + "=" * 60)
    print(f"  CORRECT rate: before {bc}/{N} ({bc*5}%)  ->  after {ac}/{N} ({ac*5}%)")
    headline = (bc <= N*0.3) and (ac >= N*0.7)
    if headline:
        print(">>> HEADLINE-WORTHY: clean flip (before mostly not-correct, after mostly correct).")
        print(">>> Self-improvement HEADLINES.")
    else:
        print(">>> NOT a clean flip by the rule (need before<=30% AND after>=70%).")
        print(">>> Self-improvement DEMOTES to supporting beat; blast-radius headlines.")
    print("=" * 60)

asyncio.run(main())
