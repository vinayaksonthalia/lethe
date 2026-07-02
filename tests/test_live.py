"""Opt-in LIVE end-to-end test (the forget hero).

SKIPPED by default — runs ONLY when RUN_LIVE=1 is set AND you invoke `-m live`
(the default addopts is `-m "not live"`, so a normal `pytest` run never collects it).

It exercises the real remember -> recall -> forget loop against real cognee on a
THROWAWAY dataset (NEVER main_dataset / the golden graph): ingest 2 contradictory
docs, ask, forget one, ask again, and assert the answer changed.

IMPORTANT: this does real graph/LLM work and grabs the kuzu lock, so the app on
:8077 MUST be stopped and valid LLM creds (.env) must be present before running it.
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.live

# A unique, disposable dataset name. NEVER 'main_dataset' (the golden) — asserted below.
THROWAWAY_DATASET = f"lethe_test_{uuid.uuid4().hex[:8]}"


@pytest.mark.skipif(os.environ.get("RUN_LIVE") != "1",
                    reason="live test — set RUN_LIVE=1 (and stop the :8077 server) to run")
def test_hero_flip():
    import asyncio
    import cognee
    import incident_brain as ib

    assert THROWAWAY_DATASET != "main_dataset", "guard: must never run against the golden dataset"

    sys_name = "throwaway-cache"
    doc_a = (f"Runbook: {sys_name}. When the api is slow, the FIRST thing to check is the "
             f"{sys_name}: flush and resize the {sys_name} cluster to recover.")
    doc_b = (f"Post-mortem: a {sys_name} eviction storm caused an outage; we flushed the "
             f"{sys_name} to recover.")
    question = f"If the api is slow, what is the first thing to check?"

    async def run():
        ledger = {}
        for txt in (doc_a, doc_b):
            r = await cognee.add(txt, dataset_name=THROWAWAY_DATASET)
            info = getattr(r, "data_ingestion_info", None) or []
            did = info[0].get("data_id") if info and isinstance(info[0], dict) else None
            ledger.setdefault(sys_name, []).append(str(did) if did else None)
        await cognee.cognify(datasets=[THROWAWAY_DATASET])

        before = await ib.ask(question, dataset=THROWAWAY_DATASET)
        await ib.forget_system(sys_name, ledger, dataset=THROWAWAY_DATASET)
        after = await ib.ask(question, dataset=THROWAWAY_DATASET)

        # Cleanup the throwaway dataset (best-effort) so we leave nothing behind.
        try:
            await cognee.delete(dataset_name=THROWAWAY_DATASET)
        except Exception:
            pass
        return before, after

    before, after = asyncio.run(run())
    assert before.strip() != after.strip(), (
        "answer should change after forgetting the system\n"
        f"BEFORE: {before!r}\nAFTER:  {after!r}"
    )
    # The forgotten system name should be gone from the post-forget answer.
    assert sys_name.lower() not in after.lower()
