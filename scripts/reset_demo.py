"""Reset the demo to the GOLDEN build by RESTORING the snapshot (no re-ingest).

Deterministic, instant, zero LLM quota — restores the exact known-good graph,
including the legacy-cache docs a prior demo forgot. This REPLACES `setup.py` as the
demo reset: setup.py re-ingests (~1 min, burns quota, and the build is irrelevant to
answer quality anyway); this just copies files back. Run with the SERVER STOPPED
(the DBs are file-locked while uvicorn holds them).

  ./.venv/bin/python reset_demo.py
  ./.venv/bin/python -m uvicorn app:app --port 8077   # then start the app (instant)
"""
import os, shutil
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (this script lives in scripts/)
load_dotenv(os.path.join(ROOT, ".env"))

DATA = os.environ["DATA_ROOT_DIRECTORY"]
SYSTEM = os.environ["SYSTEM_ROOT_DIRECTORY"]
LEDGER = os.path.join(ROOT, "ledger.json")
GOLDEN = os.path.join(ROOT, "golden_snapshot")


def _restore(src, dst):
    if os.path.exists(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def main():
    if not os.path.isdir(GOLDEN):
        raise SystemExit("No golden_snapshot/ — create it first with snapshot_golden.py.")
    _restore(os.path.join(GOLDEN, "data"), DATA)
    _restore(os.path.join(GOLDEN, "system"), SYSTEM)
    shutil.copy2(os.path.join(GOLDEN, "ledger.json"), LEDGER)
    print("Demo reset to the golden build (restored from snapshot — no re-ingest, no quota).")
    print("Start the app:  ./.venv/bin/python -m uvicorn app:app --port 8077")


if __name__ == "__main__":
    main()
