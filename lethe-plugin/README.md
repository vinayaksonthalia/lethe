# Lethe — Claude Code plugin (MCP tools + incident-triage skill)

Drop Lethe's **on-call memory with verifiable forgetting** into your terminal agent. This plugin bundles:

- **The `lethe` MCP server** — 10 tools: `triage`, `decommission_system`, `list_systems`, `curation_scan`, `check_conflicts`, `mark_reviewed`, `memory_timeline`, `list_workspaces`, `run_curation_cycle`, `restore_system`.
- **An `incident-triage` skill** — so the agent automatically knows *when* to call each tool (the triage → decommission → forget-receipt beat), instead of you having to prompt it.

The MCP server is a thin proxy to the running Lethe app, so the graph logic stays in one place.

## Prerequisite

The Lethe app must be running:

```bash
./.venv/bin/python -m uvicorn app:app --port 8077
```

The MCP server talks to it over HTTP (`LETHE_URL`, default `http://127.0.0.1:8077`). The Python that launches the MCP server needs `mcp` and `httpx` — the project `./.venv` already has them; for a system `python3`, run `pip install "mcp[cli]" httpx`.

---

## Install in Claude Code (the plugin — gets tools **and** the skill)

From a repo that contains this `lethe-plugin/` (and the `.claude-plugin/marketplace.json` at its root):

```
/plugin marketplace add /Users/vinayak/Documents/devlopment/wemakdevs/cognee\ hackathon/cognee\ open/incident-detective
/plugin install lethe@lethe-marketplace
```

(or `/plugin marketplace add <github-owner>/<repo>` once it's pushed). Enable it, then `/plugin` to confirm — the skill shows up namespaced as `lethe:incident-triage` and the 10 tools auto-register.

> If `python3` on your PATH lacks `mcp`/`httpx`, either `pip install "mcp[cli]" httpx`, or edit `lethe-plugin/.mcp.json` to point `command` at your venv python.

---

## Use from other MCP clients (tools only — no plugin needed)

Same server, configured by hand. Point `command` at the project's venv python so the deps are already present. Paths below are for **this machine** — adjust if you move the repo.

### Cursor — `~/.cursor/mcp.json`
```json
{
  "mcpServers": {
    "lethe": {
      "command": "/Users/vinayak/Documents/devlopment/wemakdevs/cognee hackathon/cognee open/incident-detective/.venv/bin/python",
      "args": ["/Users/vinayak/Documents/devlopment/wemakdevs/cognee hackathon/cognee open/incident-detective/mcp_server.py"],
      "env": { "LETHE_URL": "http://127.0.0.1:8077" }
    }
  }
}
```

### Google Antigravity — its MCP settings (Settings → MCP / the agent's `mcp_config.json`)
Antigravity speaks MCP; add the **same server block** in its MCP config:
```json
{
  "mcpServers": {
    "lethe": {
      "command": "/Users/vinayak/Documents/devlopment/wemakdevs/cognee hackathon/cognee open/incident-detective/.venv/bin/python",
      "args": ["/Users/vinayak/Documents/devlopment/wemakdevs/cognee hackathon/cognee open/incident-detective/mcp_server.py"],
      "env": { "LETHE_URL": "http://127.0.0.1:8077" }
    }
  }
}
```

### Claude Desktop — `claude_desktop_config.json`
Identical `mcpServers` block as above.

---

## Try it (the demo beat)

In the agent: *"use lethe to triage: if auth-service latency is high, what should I check?"* → it recommends a system. *"now decommission legacy-cache"* → you get the measured receipt. *"ask the same triage question again"* → the answer **flips**, and *"what is legacy-cache?"* → "not documented." Same question, different answer — proof the memory forgot.

`mcp_server.py` here is a copy of the project's `../mcp_server.py` (a thin HTTP proxy) so the plugin is self-contained; keep them in sync if you change the proxy.
