"""Incident Detective — Phase A skeleton.

Feeds 4 separate incident documents into Cognee, builds a knowledge graph,
then asks a question whose answer lives in NO single document — it only emerges
by connecting facts across all of them. That cross-document chain is the hero.
"""
import os, asyncio, glob, warnings
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# The answer to this is in none of the docs alone. It requires chaining:
#   recent change (Redis maxmemory reduced 06-19)
#   + past incident (low maxmemory -> eviction -> login + payment failures)
#   + architecture (Checkout -> Payment -> Auth -> Redis)
#   + runbook (escalate Redis to team-platform / Sana)
QUESTION = (
    "Tonight checkout is failing and users report flaky logins. "
    "What is the most likely cause, what recent change is responsible, "
    "and which team should I page? Explain the chain of reasoning."
)

async def main():
    # Fresh slate each run.
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    docs = sorted(glob.glob(os.path.join(DATA_DIR, "*.md")))
    print(f"Ingesting {len(docs)} documents: {[os.path.basename(d) for d in docs]}")
    for path in docs:
        with open(path) as f:
            await cognee.add(f.read())

    print("Building the knowledge graph (cognify)...")
    await cognee.cognify()

    print("\nQUESTION:\n", QUESTION)
    ans = await cognee.search(query_text=QUESTION, query_type=SearchType.GRAPH_COMPLETION)
    print("\n===== INCIDENT DETECTIVE ANSWER =====")
    result = ans[0] if isinstance(ans, list) and ans else ans
    if isinstance(result, dict):
        result = result.get("search_result", result)
    print(result)
    print("=====================================")

if __name__ == "__main__":
    asyncio.run(main())
