"""Lethe MCP server — exposes the on-call incident memory (remember / recall / FORGET) to any MCP
client (Claude Desktop, Claude Code, Cursor). It is a THIN PROXY to the running Lethe app
(FastAPI on :8077) so there is ONE source of truth — every tool just forwards to an endpoint we
already have, reusing all the graph logic, workspace state and the forget receipt.

Run (stdio): ./.venv/bin/python mcp_server.py
The Lethe app must be running first:   ./.venv/bin/python -m uvicorn app:app --port 8077
Override the target with the LETHE_URL env var (default http://127.0.0.1:8077).
"""
import os
import sys
try:
    import httpx
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as _e:  # the plugin may launch with a python that lacks these
    sys.stderr.write(
        f"lethe MCP server: missing dependency ({_e.name}). Install with:\n"
        f'    pip install "mcp[cli]" httpx\n'
        f"  or point this server's command at the project venv python "
        f"(./.venv/bin/python).\n")
    sys.exit(1)

LETHE_URL = os.environ.get("LETHE_URL", "http://127.0.0.1:8077").rstrip("/")
# If the target app is token-protected (LETHE_AUTH_TOKEN set), attach it to every proxied call.
_TOKEN = os.environ.get("LETHE_AUTH_TOKEN", "").strip()
_HEADERS = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}
mcp = FastMCP("lethe")

_DOWN = (f"⚠️ Lethe isn't reachable at {LETHE_URL}. Start it first:\n"
         f"    ./.venv/bin/python -m uvicorn app:app --port 8077")


def _err(e):
    """Friendly message for a non-transport failure. A 401 means the app has LETHE_AUTH_TOKEN set."""
    if isinstance(e, httpx.HTTPStatusError) and e.response is not None and e.response.status_code == 401:
        return ("⚠️ Lethe rejected the request (401 Unauthorized). The app has LETHE_AUTH_TOKEN set — "
                "set the same token in this MCP server's LETHE_AUTH_TOKEN env var.")
    return f"⚠️ Lethe error: {e}"


async def _get(path: str, params: dict | None = None, timeout: float = 30.0):
    async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS) as c:
        r = await c.get(f"{LETHE_URL}{path}", params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict, timeout: float = 180.0):
    async with httpx.AsyncClient(timeout=timeout, headers=_HEADERS) as c:
        r = await c.post(f"{LETHE_URL}{path}", json=body)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def triage(question: str, workspace: str = "incidents") -> str:
    """Ask Lethe's incident memory what to check / who owns what / what depends on what.
    Use this for any on-call or runbook question (e.g. "auth-service latency is high, what do I check?").
    Answers are grounded in the knowledge graph built from your runbooks — Lethe will say "not documented"
    rather than guess. `workspace` selects a knowledge base (default the main "incidents" one)."""
    try:
        d = await _post("/ask", {"query": question, "history": [], "workspace": workspace})
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    answer = d.get("answer") or "(no answer)"
    cites = d.get("citations") or []
    if cites:
        answer += "\n\nSources: " + ", ".join(cites)
    return answer


@mcp.tool()
async def decommission_system(system: str, workspace: str = "incidents") -> str:
    """DECOMMISSION (forget) a system: hard-delete its runbooks from the graph + vectors so Lethe stops
    giving stale advice about it. Returns a verifiable receipt (documents, graph nodes and relationships
    removed) plus a re-query proof showing the system now reads as "not documented". This is the hero
    operation — use it when a system is retired/replaced. Irreversible by design (verifiable deletion)."""
    try:
        d = await _post("/forget", {"system": system, "workspace": workspace})
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    msg = d.get("message", "")
    r = d.get("receipt")
    if not r:
        return msg or "(no result)"
    return (f"{msg}\n\nProof of Forgetting — measured removal:\n"
            f"  • documents removed: {r.get('docs')}\n"
            f"  • graph nodes removed: {r.get('nodes_removed')}\n"
            f"  • relationships removed: {r.get('edges_removed')}\n"
            f"Re-query proof — \"{r.get('proof_query')}\":\n  {r.get('proof_answer')}")


@mcp.tool()
async def list_systems(workspace: str = "incidents") -> str:
    """List every system currently in Lethe's memory, with how many documents back it and how long since
    its runbook was last reviewed. Systems overdue for review (>180 days) are flagged OVERDUE."""
    try:
        d = await _get("/systems", {"workspace": workspace})
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    systems = d.get("systems") or []
    if not systems:
        return "No systems in this workspace yet."
    stale = d.get("stale_days", 180)
    lines = []
    for s in systems:
        age = s.get("age_days")
        fresh = ""
        if age is not None:
            fresh = f" · reviewed {age}d ago" + (" — OVERDUE" if age > stale else "")
        docs = s.get("docs", 0)
        lines.append(f"  • {s['name']} — {docs} doc{'' if docs == 1 else 's'}{fresh}")
    return f"{len(systems)} system(s) in '{workspace}':\n" + "\n".join(lines)


@mcp.tool()
async def curation_scan(workspace: str = "incidents") -> str:
    """Run Lethe's FREE (0-token, deterministic) memory-hygiene scans on a workspace: (1) stale references —
    remaining runbooks that still mention a decommissioned system; (2) aging — runbooks overdue for review.
    Use this to audit whether the memory has rotted. For contradiction detection (which costs model tokens)
    use check_conflicts instead."""
    try:
        stale = await _get("/curation", {"workspace": workspace})
        aging = await _get("/curation/aging", {"workspace": workspace, "days": 180})
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    out = []
    refs = stale.get("findings") or []
    if refs:
        out.append(f"STALE REFERENCES ({len(refs)}): remaining runbooks still mention a decommissioned system —")
        for f in refs[:20]:
            out.append(f"  • {f.get('system')} still mentions '{f.get('stale_ref')}': …{f.get('snippet','')}…")
    else:
        out.append(f"STALE REFERENCES: clean — no remaining runbook references a decommissioned system "
                   f"({stale.get('scanned',0)} docs scanned).")
    aged = aging.get("aging") or []
    if aged:
        out.append(f"\nAGING ({len(aged)} overdue for review, >{aging.get('days',180)}d):")
        for a in aged:
            out.append(f"  • {a['system']} — last reviewed {a['reviewed']} ({a['age_days']}d ago)")
    else:
        out.append("\nAGING: all runbooks reviewed recently.")
    return "\n".join(out)


@mcp.tool()
async def check_conflicts(workspace: str = "incidents") -> str:
    """Scan for CONTRADICTORY runbooks (two docs that disagree on a fact). NOTE: this one USES YOUR MODEL
    (a few bounded LLM calls, capped) unlike the free scans in curation_scan — call it deliberately."""
    try:
        d = await _get("/curation/conflicts", {"workspace": workspace}, timeout=120.0)
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    conflicts = d.get("conflicts") or []
    if not conflicts:
        return f"No contradictions found ({d.get('pairs_checked',0)} runbook pair(s) checked)."
    lines = [f"{len(conflicts)} contradiction(s) found:"]
    for c in conflicts:
        lines.append(f"  • {c.get('a')} vs {c.get('b')}: {c.get('detail')}")
    return "\n".join(lines)


@mcp.tool()
async def mark_reviewed(system: str, workspace: str = "incidents") -> str:
    """Mark a system's runbook as reviewed today — it leaves the overdue list and the review is recorded on
    the memory timeline. Use after you've verified a stale/aging runbook is still accurate."""
    try:
        d = await _post("/curation/review", {"system": system, "workspace": workspace}, timeout=30.0)
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    if d.get("ok"):
        return f"✓ '{system}' marked reviewed on {d.get('reviewed')} — recorded on the timeline."
    return d.get("message", "Could not mark reviewed.")


@mcp.tool()
async def memory_timeline(workspace: str = "incidents", limit: int = 15) -> str:
    """Show the memory timeline — a chronological audit log of what this workspace learned, forgot and
    reviewed, newest first. Forget events carry their removal receipt. Use to answer "what changed in our
    memory and when"."""
    try:
        d = await _get("/timeline", {"workspace": workspace})
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    events = (d.get("events") or [])[:limit]
    if not events:
        return "No memory events recorded yet for this workspace."
    lines = []
    for e in events:
        op = e.get("op", "?").upper()
        ts = (e.get("ts") or "")[:10]
        line = f"  • [{ts}] {op} {e.get('system','')}"
        det = e.get("detail") or {}
        if op == "FORGOTTEN" and det.get("nodes_removed") is not None:
            line += f" (removed {det.get('docs')} docs, {det.get('nodes_removed')} nodes, {det.get('edges_removed')} edges)"
        lines.append(line)
    return f"Memory timeline for '{workspace}' (newest first):\n" + "\n".join(lines)


@mcp.tool()
async def list_workspaces() -> str:
    """List Lethe's workspaces (separate knowledge bases, each with its own graph). Pass a workspace id to
    the other tools to scope them; the default is "incidents"."""
    try:
        d = await _get("/workspaces")
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    ws = d.get("workspaces") or []
    return "Workspaces:\n" + "\n".join(f"  • {w['id']}  ({w.get('name','')})" for w in ws)


@mcp.tool()
async def run_curation_cycle(workspace: str = "incidents", dry_run: bool = True) -> str:
    """Run ONE bounded curation/decay pass over a workspace. Aging runbooks are AUTO-DEMOTED (reversible —
    they sink in answers but stay restorable); very-stale ones are also QUEUED for human approval before any
    permanent delete — the cycle NEVER hard-deletes on its own. dry_run=True (default) previews the plan
    for 0 tokens without changing anything; set dry_run=False to apply the demotions. This is the
    memory-maintenance layer that complements the hero forget."""
    try:
        d = await _post("/curation/cycle", {"workspace": workspace, "dry_run": dry_run})
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    if d.get("error"):
        return f"⚠️ {d['error']}"
    h = d.get("health") or {}
    auto = [x.get("system") for x in (d.get("auto_demoted") or [])]
    queued = [x.get("system") for x in (d.get("queued_for_approval") or [])]
    verb = "Would demote" if dry_run else "Demoted"
    lines = [f"Curation cycle for '{workspace}' ({'dry run — nothing changed' if dry_run else 'applied'}):",
             f"  health: {h.get('systems','?')} systems · {h.get('overdue','?')} overdue"]
    lines.append(f"  {verb} (reversible): " + (", ".join(auto) if auto else "none"))
    lines.append("  Queued for your approval (hard-delete): " + (", ".join(queued) if queued else "none"))
    if dry_run and (auto or queued):
        lines.append("  Re-run with dry_run=false to apply the demotions; queued items still wait for a human.")
    return "\n".join(lines)


@mcp.tool()
async def restore_system(system: str, workspace: str = "incidents") -> str:
    """Restore a previously DEMOTED system to normal ranking (undo a demotion from the curation cycle). This
    is the reversible counterpart to demotion — use it when an aging runbook turns out to still be accurate.
    (Hard-deleted/decommissioned systems are gone for good and cannot be restored.)"""
    try:
        d = await _post("/curation/restore", {"system": system, "workspace": workspace}, timeout=60.0)
    except httpx.TransportError:
        return _DOWN
    except Exception as e:
        return _err(e)
    if d.get("ok"):
        return f"✓ Restored '{system}' to normal ranking — recorded on the timeline."
    return d.get("message", "Could not restore (it may not have been demoted).")


if __name__ == "__main__":
    mcp.run()
