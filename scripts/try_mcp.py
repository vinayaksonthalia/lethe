"""ONE-COMMAND MCP test — proves the Lethe MCP server works, no Claude setup needed.

It starts the MCP server exactly the way Claude Desktop would (over stdio), then calls a few
tools and prints the results. Read-only: it never forgets anything, so it's safe to run anytime.

  1) make sure the app is running:   ./.venv/bin/python -m uvicorn app:app --port 8077
  2) run this:                       ./.venv/bin/python try_mcp.py
"""
import asyncio
import os
import sys
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (this file lives in scripts/)
PY = os.path.join(HERE, ".venv/bin/python")
PARAMS = StdioServerParameters(command=PY, args=[os.path.join(HERE, "mcp_server.py")])


def _text(result):
    return "\n".join(getattr(c, "text", "") for c in result.content)


def _banner(msg):
    print("\n" + "─" * 70 + f"\n{msg}\n" + "─" * 70)


async def main():
    # 0) is the app up?
    try:
        httpx.get("http://127.0.0.1:8077/health", timeout=3)
    except Exception:
        print("❌ The Lethe app isn't running yet.\n"
              "   Start it in another terminal, then run this again:\n"
              "   ./.venv/bin/python -m uvicorn app:app --port 8077")
        sys.exit(1)

    print("✅ Lethe app is running. Connecting to the MCP server the way Claude would…")
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            _banner(f"🔌 Connected. The MCP server offers {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"   • {t.name}")

            _banner('🔎 triage("If auth-service latency is high, what should I check?")')
            print(_text(await s.call_tool("triage",
                  {"question": "If auth-service latency is high, what should I check?"})))

            _banner("📋 list_systems()")
            print(_text(await s.call_tool("list_systems", {"workspace": "incidents"})))

            _banner("🧹 curation_scan()  — free, 0 tokens")
            print(_text(await s.call_tool("curation_scan", {"workspace": "incidents"})))

            _banner("✅ It works! Every tool above ran through the real MCP protocol — "
                    "the same path\n   Claude Desktop / Claude Code uses. (No system was forgotten — this is read-only.)")


if __name__ == "__main__":
    asyncio.run(main())
