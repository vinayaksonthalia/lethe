# Using Lethe

A hands-on guide to every feature — on the [live demo](https://vinayaksonthalia-lethe.hf.space/app) (nothing to install, resets on restart) or your own instance.

## Run it locally (2 minutes)

```bash
git clone https://github.com/vinayaksonthalia/lethe.git && cd lethe
uv venv && uv pip install -r requirements.txt        # or python3.12 -m venv + pip
cp .env.example .env                                 # paste ONE LLM key (Groq free tier works)
./.venv/bin/python scripts/setup.py                  # one-time build (~1 min)
./.venv/bin/python -m uvicorn app:app --port 8077    # starts instantly after that
```

Open `http://localhost:8077/app`. No key yet? Everything except the chat, upload, and forget-proof still works — Systems, Graph, Timeline, and the free Curation scans are fully offline. Reset to the pristine demo state any time: `./.venv/bin/python scripts/reset_demo.py`.

Prefer Docker? See [docs/DEPLOY.md](https://github.com/vinayaksonthalia/lethe/blob/main/docs/DEPLOY.md).

## The 60-second demo (the point of the product)

1. **Triage** → ask: `If auth-service latency is high, what should I check?` → the answer recommends flushing **legacy-cache**, with source pills under it.
2. Click the **legacy-cache pill** → the original runbooks slide out (every citation opens its source).
3. **Systems** → `legacy-cache` → **Decommission** → read the receipt: documents, graph nodes, and relationships removed, plus a live re-query proving it's gone.
4. Ask the **exact same question** again → the advice flips to **session-store**. Nothing was re-prompted; the memory itself changed.
5. **Re-arm the demo** (the card that appears in Systems) re-ingests the two runbooks in ~30s so anyone can run the loop again.

## Each screen, in one line

| Screen | What it's for |
|---|---|
| **Triage** | Ask on-call questions in plain English; answers stream with citations and an optional connections map. |
| **Systems** | The inventory. Decommission (hard-delete with receipt), mark reviewed, re-arm the demo. |
| **Upload** | Feed your own runbooks — paste text or drop `.txt/.md/.json/.csv/.log`. Ingestion is a real `cognify` pass (~1 min). |
| **Graph** | The live knowledge graph. Drag nodes, hover to trace, click for blast-radius (1–3 hops), search box to jump to a node. |
| **Curation** | Memory hygiene: the health score, aging/stale/contradiction scans, and the decay cycle (demote reversibly → queue hard-deletes for your approval). |
| **Timeline** | The audit log — everything learned, reviewed, demoted, restored, and forgotten, with receipts. |

## Working with your own knowledge

The default **Incidents** workspace is read-only demo data. Create a **workspace** (switcher, top-left), then Upload your own docs into it — each workspace is an isolated graph + vector store. Ask questions, decommission your own systems, watch your own flip. On the hosted demo, workspaces are ephemeral (gone on restart); self-host for persistence.

## The two-layer decay loop (worth 2 minutes)

In **Curation**, hit **Run cycle**: a free preview shows which aging runbooks *would* sink (reversible demote) and which very-stale ones are queued for a **human-approved** hard delete — Lethe never permanently deletes on its own. **Apply**, then check Systems (amber `demoted · reversible` pills) and Graph (amber nodes). Mark a demoted runbook reviewed and it self-heals back. That's the whole thesis in one loop: stale advice sinks automatically, deletion always waits for a person, and everything lands on the Timeline.

## From your editor (MCP)

With the app running, Lethe's memory is callable from Claude Code / Cursor as 10 MCP tools (triage, decommission, curation, timeline…):

```bash
claude mcp add -s user lethe -- /abs/path/.venv/bin/python /abs/path/mcp_server.py
# sanity check without any client:
./.venv/bin/python scripts/try_mcp.py
```

## Good to know

- **Bring your own model** — Settings swaps provider/key at runtime (validated; auto-reverts if the key is bad). Fixed on the hosted demo.
- **Nothing needs the cloud** — embeddings are local; point it at Ollama/LM Studio and it runs fully offline.
- **Everything is auditable** — every destructive action produces a receipt on the Timeline; the source peek 404s for forgotten systems (the proof holds at every layer).
