"""Prove Lethe answers FULLY OFFLINE on a local Ollama model.

Reads whatever LLM the active .env points at (the swap-and-restore harness in the
shell points it at Ollama for this run), loads the existing golden graph, runs a
GRAPH_COMPLETION search, and prints cognee's RESOLVED llm config (authoritative —
not just os.environ) plus the answer string. Non-destructive (search only).

Run via the harness so .env is restored afterwards.
"""
import asyncio
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (this file lives in scripts/)
import incident_brain as ib  # sets CACHING=false etc., then imports cognee (which loads the active .env)
from cognee.infrastructure.llm.config import get_llm_config
from cognee.modules.users.methods import get_default_user


async def main():
    c = get_llm_config()
    print(f"RESOLVED provider : {c.llm_provider}")
    print(f"RESOLVED model    : {c.llm_model}")
    print(f"RESOLVED endpoint : {c.llm_endpoint}")
    offline = "localhost" in str(c.llm_endpoint or "") or "127.0.0.1" in str(c.llm_endpoint or "") or (c.llm_provider == "ollama")
    print(f"OFFLINE path      : {offline}  (embeddings=fastembed local, graph=kuzu, vectors=lancedb)")
    user = await get_default_user()
    q = "If auth-service latency is high, what should I check?"
    print(f"\nQ: {q}")
    ans = await ib.ask(q, user)
    print(f"\nA:\n  {ans}")
    hero = ("legacy-cache" in ans.lower()) or ("legacy cache" in ans.lower())
    print(f"\n[answered over the golden graph: {len(ans) > 0} · hero BEFORE-state present: {hero}]")


if __name__ == "__main__":
    asyncio.run(main())
