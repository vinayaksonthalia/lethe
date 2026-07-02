"""One-time build (also the RESET command): ingest the incident wiki into the graph and
persist ledger.json. After this, the web app starts INSTANTLY (no cognify at serve time).

Run once / to reset:  ./.venv/bin/python scripts/setup.py
Then start the app:   ./.venv/bin/python -m uvicorn app:app --port 8077
"""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root, for incident_brain
import incident_brain as ib  # sets env (CACHING=false etc.) BEFORE importing cognee
import cognee


async def main():
    print("Building the incident graph (one-time, ~1 min)…")
    await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)
    led = await ib.ingest()
    print("Done. Systems:", list(led.keys()))
    print("Ledger -> ledger.json")
    print("Start the app:  ./.venv/bin/python -m uvicorn app:app --port 8077")


if __name__ == "__main__":
    asyncio.run(main())
