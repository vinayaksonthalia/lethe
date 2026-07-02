---
name: incident-triage
description: On-call incident triage backed by Lethe's verifiable memory (via the lethe MCP server). Use when diagnosing an incident or answering a runbook question (what to check, who owns a system, what depends on it), when a system is retired and its stale advice must be forgotten, or when auditing whether the runbook memory has gone stale or self-contradictory.
---

# Incident triage with Lethe

Lethe is an on-call memory that **remembers** your runbooks and **forgets** decommissioned systems so it never gives stale advice. Its tools are exposed over MCP under the server name `lethe`.

**Prerequisite:** the Lethe app must be running — `./.venv/bin/python -m uvicorn app:app --port 8077`. The MCP server is a thin proxy to it; set `LETHE_URL` if the app runs elsewhere. If a tool reports Lethe is unreachable, the app isn't running.

## When to use which tool

- **Any on-call / runbook question** ("auth-service latency is high — what do I check?", "who owns payments?", "what breaks if X fails?") → `lethe:triage`. Answers are grounded in the knowledge graph; Lethe says *"not documented"* rather than guessing, and returns its sources.
- **A system was retired / replaced** → `lethe:decommission_system`. This is the hero op: it **hard-deletes** the system's runbooks from the graph and vectors and returns a **measured receipt** (documents, nodes, relationships removed) plus a re-query proof that the system now reads *"not documented."* Stale advice about it can never resurface. **Irreversible by design — confirm with the user before forgetting.**
- **Audit memory rot** → `lethe:curation_scan` first (free, 0-token: stale references + aging runbooks), then `lethe:check_conflicts` only when you specifically want contradiction detection (it spends model tokens).
- **After verifying an aging runbook is still accurate** → `lethe:mark_reviewed` (clears it from the overdue list and logs the timeline).
- **"What changed in our memory and when?"** → `lethe:memory_timeline` (forget events carry their receipt).
- **Orient** → `lethe:list_systems`, `lethe:list_workspaces`. Pass a `workspace` to scope any tool (default `incidents`).

## The signature beat (great for a demo)

1. `triage("If auth-service latency is high, what should I check?")` → it recommends a system (e.g. `legacy-cache`).
2. `decommission_system("legacy-cache")` → a measured receipt (documents / graph nodes / relationships removed) plus a re-query proof.
3. Ask the **exact same question again** → the answer has **flipped** to the live system, and `triage("What is legacy-cache?")` now returns *"not documented."*

Same question, different answer — visible proof the memory actually forgot.

## Guardrails

- Don't fabricate system names — call `list_systems` to see what exists.
- Decommission is **permanent** (verifiable hard-delete is the whole point). Always confirm intent first.
- Lethe only answers from ingested runbooks — never from the open web — so "not documented" is a correct, honest answer, not a failure.
