"""Verify the FIXED ask() returns CLEAN answers (no object dump), and read the real strings.
Runs the actual HERO beat (the auth-latency triage question) before/after forget, plus a direct
name-lookup of the forgotten system. Prints full strings for human reading — no substring scoring.
"""
import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"; os.environ["CACHING"] = "false"
warnings.filterwarnings("ignore")
import sys; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (this file lives in scripts/)
import incident_brain as ib
import cognee
from cognee.modules.users.methods import get_default_user

HERO = "If auth-service latency is high, what should I check, and what's in the auth read path?"
LOOKUP = "What is the legacy-cache and what depends on it?"

async def main():
    await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)
    ledger = await ib.ingest()
    user = await get_default_user()

    print("HERO QUERY:", HERO)
    print("\n[1] BEFORE forget:\n   ", await ib.ask(HERO, user))

    print("\n[2] forget legacy-cache:", await ib.forget_system("legacy-cache", ledger), "doc(s)")

    print("\n[3] AFTER forget (hero query):\n   ", await ib.ask(HERO, user))

    print("\n[4] AFTER forget — direct name lookup:", LOOKUP, "\n   ", await ib.ask(LOOKUP, user))

asyncio.run(main())

