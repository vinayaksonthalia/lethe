"""Snapshot the CURRENT cognee graph + ledger as the 'golden' demo build.

Run ONCE after a clean build (legacy-cache present, demo question answers fluently),
with the SERVER STOPPED. Thereafter, reset_demo.py RESTORES this snapshot instead of
re-running setup.py — deterministic, instant, ZERO LLM quota, no cognify gamble.

  ./.venv/bin/python snapshot_golden.py
"""
import os, json, shutil
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (this script lives in scripts/)
load_dotenv(os.path.join(ROOT, ".env"))

DATA = os.environ["DATA_ROOT_DIRECTORY"]
SYSTEM = os.environ["SYSTEM_ROOT_DIRECTORY"]
LEDGER = os.path.join(ROOT, "ledger.json")
GOLDEN = os.path.join(ROOT, "golden_snapshot")


def main():
    if not os.path.exists(LEDGER):
        raise SystemExit("No ledger.json — build a graph first with setup.py.")
    if os.path.exists(GOLDEN):
        shutil.rmtree(GOLDEN)
    os.makedirs(GOLDEN)
    shutil.copytree(DATA, os.path.join(GOLDEN, "data"))
    shutil.copytree(SYSTEM, os.path.join(GOLDEN, "system"))
    shutil.copy2(LEDGER, os.path.join(GOLDEN, "ledger.json"))

    led = json.load(open(LEDGER))
    print("Golden snapshot written ->", GOLDEN)
    print("  systems:", list(led.keys()))
    print("  legacy-cache docs captured:", len(led.get("legacy-cache", [])))
    print("Reset the demo any time with:  ./.venv/bin/python reset_demo.py")


if __name__ == "__main__":
    main()
