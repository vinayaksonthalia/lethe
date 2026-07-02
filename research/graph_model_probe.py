"""graph_model_probe.py — does a CUSTOM, type-constrained graph_model give cleaner, deduplicated
extraction than Cognee's default schema? (And is that the real fix for "Cognee Lint" dedup + the
gateway to the 1.2.x upgrade?)

WHY: the default extraction schema lets the LLM invent free-text entity types, so the golden graph
ends up with near-duplicate / fragment types like `service` vs `services`. A post-hoc dedup scanner
can't safely merge them on 1.1.3 (no merge API; forget is doc-level). The upstream fix is a custom
`graph_model` whose `type` is a fixed Enum — the LLM must map every entity to a canonical type, so
duplicates never form. This A/B measures whether that actually happens.

SAFETY: runs in a throwaway TEMP cognee data dir (loaded from the real .env for LLM creds, then the
data/system roots are overridden to a temp path BEFORE importing cognee). The golden graph is never
touched — nothing to reset. Run from anywhere:  ./.venv/bin/python research/graph_model_probe.py
"""
import os, sys, json, asyncio, tempfile, shutil
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(os.path.dirname(HERE), ".env")
load_dotenv(ENV)                                   # LLM creds (+ golden data dirs, which we override next)
_TMP = tempfile.mkdtemp(prefix="gm_probe_")
os.environ["DATA_ROOT_DIRECTORY"] = os.path.join(_TMP, "data")      # os.environ > .env in pydantic-settings,
os.environ["SYSTEM_ROOT_DIRECTORY"] = os.path.join(_TMP, "system")  # so cognee writes ONLY to the temp dir
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["CACHING"] = "false"

# Optional LLM override — e.g. test whether a STRONGER structured-output model (via Ollama) builds the
# constrained custom schema where the default Groq model produced an empty graph. Set PROBE_LLM_MODEL.
_LLM_USED = os.environ.get("LLM_MODEL", "?")
if os.environ.get("PROBE_LLM_MODEL"):
    os.environ["LLM_PROVIDER"] = os.environ.get("PROBE_LLM_PROVIDER", "ollama")
    os.environ["LLM_MODEL"] = os.environ["PROBE_LLM_MODEL"]
    os.environ["LLM_ENDPOINT"] = os.environ.get("PROBE_LLM_ENDPOINT", "http://localhost:11434/v1")
    os.environ["LLM_API_KEY"] = os.environ.get("PROBE_LLM_API_KEY", "ollama")
    os.environ["LLM_INSTRUCTOR_MODE"] = os.environ.get("PROBE_LLM_INSTRUCTOR_MODE", "json_schema_mode")
    os.environ.setdefault("HUGGINGFACE_TOKENIZER", "BAAI/bge-small-en-v1.5")  # 1.1.3 ollama path requires this
    _LLM_USED = os.environ["LLM_MODEL"]

import cognee
from enum import Enum
from pydantic import BaseModel, Field

# Same incident wiki the product uses (copied so we don't import incident_brain, which imports cognee
# against the golden data dir before our override could take effect).
WIKI = [
    "Runbook: api-gateway. The api-gateway routes all storefront traffic to backend services and enforces rate limits. On 5xx spikes, check upstream health and recent deploys.",
    "Runbook: auth-service. The auth-service validates login tokens and is required by the payments-service. It reads session state from the primary session store.",
    "Runbook: payments-service. The payments-service charges customers and calls the auth-service to validate each login token first. It connects to the payments database via a bounded connection pool.",
    "Runbook: legacy-cache (memcached). When auth-service latency is high, the first thing to check is the legacy-cache: flush and resize the legacy-cache cluster to recover. The legacy-cache sits in front of the auth-service session reads.",
    "Post-mortem 2024-05: a legacy-cache memory-eviction storm caused a platform-wide login outage; we flushed and resized the legacy-cache to recover.",
    "Runbook: search-index. The search-index powers product search and is independent of login and payments.",
    "Ownership: api-gateway and auth-service are owned by the core-platform team; payments-service by the payments team; search-index by the discovery team.",
]


# --- the custom, type-constrained schema (the only difference from the default) ---
class NodeType(str, Enum):
    System = "System"        # api-gateway, auth-service, payments-service, legacy-cache, search-index
    Team = "Team"            # core-platform team, payments team, discovery team
    DataStore = "DataStore"  # primary session store, payments database, connection pool
    Incident = "Incident"    # memory-eviction storm, login outage
    Action = "Action"        # flush/resize, check deploys/upstream health
    Signal = "Signal"        # 5xx spikes, latency, rate limits
    Concept = "Concept"      # catch-all so the LLM is never forced to mis-type


class GNode(BaseModel):
    id: str
    name: str
    type: NodeType
    description: str
    label: str


class GEdge(BaseModel):
    source_node_id: str
    target_node_id: str
    relationship_name: str
    description: str | None = None


class IncidentGraph(BaseModel):
    """Knowledge graph with a FIXED node-type vocabulary (canonical types -> no fragment duplicates)."""
    summary: str
    description: str
    nodes: list[GNode] = Field(default_factory=list)
    edges: list[GEdge] = Field(default_factory=list)


async def build(ds, graph_model=None):
    for text in WIKI:
        await cognee.add(text, dataset_name=ds)
    if graph_model is not None:
        await cognee.cognify(datasets=[ds], graph_model=graph_model)
    else:
        await cognee.cognify(datasets=[ds])


async def dump(ds, user):
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.graph import get_graph_engine
    async with set_database_global_context_variables(ds, user.id):
        ge = await get_graph_engine()
        nodes, edges = await ge.get_graph_data()
    ents, types = [], []
    for nid, p in nodes:
        p = p if isinstance(p, dict) else {}
        t = p.get("type") or "?"
        nm = p.get("name") or ""
        if t == "Entity":
            ents.append(str(nm))
        elif t == "EntityType":
            types.append(str(nm))
    return {"n_nodes": len(nodes), "n_edges": len(edges), "entities": sorted(ents), "types": sorted(set(types))}


async def main():
    from cognee.modules.users.methods import get_default_user
    user = await get_default_user()
    print("temp data dir:", _TMP)
    print("LLM model under test:", _LLM_USED)
    try:
        print("\nBuilding DEFAULT-schema graph (control)...")
        await build("default_ds", None)
        d = await dump("default_ds", user)
        print(f"  DEFAULT: {d['n_nodes']} nodes, {d['n_edges']} edges")
        print(f"  DEFAULT entity TYPES ({len(d['types'])}): {d['types']}")

        print("\nBuilding CUSTOM-schema graph (constrained types)...")
        ok, err = True, None
        try:
            await build("custom_ds", IncidentGraph)
            c = await dump("custom_ds", user)
        except Exception as e:
            ok, err, c = False, repr(e), {"n_nodes": 0, "n_edges": 0, "entities": [], "types": []}
        print(f"  CUSTOM : {c['n_nodes']} nodes, {c['n_edges']} edges  (build_ok={ok})")
        if err:
            print("  CUSTOM build error:", err)
        print(f"  CUSTOM entity TYPES ({len(c['types'])}): {c['types']}")

        # verdict: did the custom schema (a) build a non-empty graph, and (b) shrink the type vocabulary
        # to (close to) the canonical set, with no singular/plural-style duplicates?
        canon = {t.value for t in NodeType}
        custom_types = set(c["types"])
        off_vocab = custom_types - canon
        built = c["n_nodes"] > 0 and c["n_edges"] > 0
        cleaner = built and len(custom_types) <= len(set(d["types"]))
        print("\n" + "=" * 80)
        print("VERDICT")
        print("=" * 80)
        print(f"  custom graph built non-empty : {built}")
        print(f"  custom types within canonical: {'all' if not off_vocab else f'NO — off-vocab: {sorted(off_vocab)}'}")
        print(f"  type-vocab size  default {len(set(d['types']))}  ->  custom {len(custom_types)} "
              f"({'cleaner/equal ✅' if cleaner else 'not cleaner ⚠️'})")
        safe = _LLM_USED.lower().replace(":", "_").replace("/", "_")
        out = os.path.join(HERE, f"graph_model_probe_{safe}.json")
        json.dump({"llm_model": _LLM_USED, "default": d, "custom": c, "custom_build_ok": ok,
                   "custom_build_error": err, "canonical_types": sorted(canon), "off_vocab": sorted(off_vocab)},
                  open(out, "w"), indent=2)
        print(f"\n  saved -> {out}")
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)   # throwaway data dir — golden was never touched
        print("  (temp data dir removed; golden untouched)")


if __name__ == "__main__":
    asyncio.run(main())
