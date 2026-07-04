"""Lethe — incident-knowledge assistant (web app over the Cognee engine).

Run:  ./.venv/bin/python -m uvicorn app:app --port 8077   (then open http://localhost:8077)
Dashboard: Triage (chat) · Systems (per-system decommission/forget) · Upload (add your own docs).
Startup LOADS the prebuilt ledger (instant). Upload ingests new docs in the background.
"""
import os, re, json, asyncio, datetime, logging, socket, ipaddress, hmac, hashlib, time
from collections import deque
from urllib.parse import urlparse
import incident_brain as ib  # sets CACHING=false BEFORE importing cognee; reuses ingest/ask/forget
import cognee
import litellm  # already pulled in by cognee — used for the bounded conflict-detection calls
litellm.suppress_debug_info = True
from cognee.modules.users.methods import get_default_user
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("lethe")

# Serialize cognee MUTATIONS (forget / add+cognify / workspace-delete) so two writers can't corrupt the
# graph at once. Reads (/ask, /systems, /curation, /graph) stay lock-free. The S["ingest"] upload guard
# still gates upload-vs-upload at the HTTP layer; this lock is the lower, cross-route safety net.
_WRITE_LOCK = asyncio.Lock()

app = FastAPI(title="Lethe")

# Optional shared-secret auth — OFF by default so the local demo is unaffected. Set LETHE_AUTH_TOKEN
# to require `Authorization: Bearer <token>` on every API route before any PUBLIC deploy (protects the
# destructive /forget + the quota-spending /ask & /upload). Landing, /app and /health stay open so the
# page can load and prompt for the token; the web UI and the MCP server attach it automatically.
_AUTH_TOKEN = os.environ.get("LETHE_AUTH_TOKEN", "").strip()
# Opt-in HOSTED-DEMO mode: allow anonymous remote access with no token (pair it with LETHE_RATE_LIMIT).
# Deliberate and explicit — the fail-closed default below is unchanged unless this is set.
_PUBLIC_DEMO = os.environ.get("LETHE_PUBLIC_DEMO", "").strip().lower() in ("1", "true", "yes")
# Live retrieval influence for curation: >0 lets demoted (down-weighted) systems sink in answers.
# Proven hero-safe — with uniform weights (nothing demoted) results are identical to influence=0.
LIVE_FEEDBACK_INFLUENCE = float(os.environ.get("LETHE_FEEDBACK_INFLUENCE", "0.6"))
_AUTH_OPEN = {"/", "/app", "/health", "/evidence", "/favicon.ico", "/favicon.svg"}
_AUTH_OPEN_PREFIXES = ("/learn",)   # the docs/blog viewer is public content, like the landing
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}

def _is_authenticated(request: Request) -> bool:
    """True iff the request carries the correct bearer token (timing-safe). When no token is set, this is
    always False — callers must decide whether to allow based on the client being loopback."""
    if not _AUTH_TOKEN:
        return False
    h = request.headers.get("authorization", "")
    if not h.startswith("Bearer "):
        return False
    return hmac.compare_digest(h[len("Bearer "):], _AUTH_TOKEN)

def _is_loopback_client(request: Request) -> bool:
    return bool(request.client) and request.client.host in _LOOPBACK_HOSTS

@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    if request.url.path not in _AUTH_OPEN and not request.url.path.startswith("/shot/") \
            and not request.url.path.startswith(_AUTH_OPEN_PREFIXES):
        if _AUTH_TOKEN:
            # A token is configured — every sensitive route requires it (timing-safe compare).
            if not _is_authenticated(request):
                return JSONResponse({"detail": "Unauthorized — provide the Lethe access token."}, status_code=401)
        elif not _is_loopback_client(request) and not _PUBLIC_DEMO:
            # No token AND the caller is remote → FAIL CLOSED (don't leave /forget, /upload, /llm-config open).
            # The loopback local demo stays fully open (this branch is skipped for 127.0.0.1/::1).
            # LETHE_PUBLIC_DEMO=1 opts a HOSTED demo out of this gate (rate-limited, resettable by re-arm/restart).
            return JSONResponse(
                {"detail": "This Lethe instance is not configured for remote access. "
                           "Set LETHE_AUTH_TOKEN to allow non-local access."}, status_code=403)
    return await call_next(request)

# Optional per-IP rate limit — OFF by default (local demo unaffected). Set LETHE_RATE_LIMIT="N/S"
# (N requests per S seconds) before a public deploy to throttle the mutating/quota-spending routes.
def _parse_rate(s):
    try:
        n, w = s.strip().split("/"); n, w = int(n), float(w)
        return (n, w) if n > 0 and w > 0 else None
    except Exception:
        return None
_RATE = _parse_rate(os.environ.get("LETHE_RATE_LIMIT", ""))
_RATE_PATHS = ("/forget", "/upload", "/curation/cycle", "/curation/restore", "/curation/review", "/llm-config", "/demo/rearm")
_rate_hits = {}  # ip -> deque[monotonic timestamps]

@app.middleware("http")
async def _rate_limit(request: Request, call_next):
    if _RATE is not None and request.method == "POST" and any(request.url.path.startswith(p) for p in _RATE_PATHS):
        limit, window = _RATE
        ip = request.client.host if request.client else "?"
        now = time.monotonic()
        dq = _rate_hits.setdefault(ip, deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return JSONResponse({"error": "rate limited — try again shortly"}, status_code=429)
        dq.append(now)
        if len(_rate_hits) > 4096:  # bound memory: drop IPs with no live hits
            for k in [k for k, v in _rate_hits.items() if not v]:
                _rate_hits.pop(k, None)
    return await call_next(request)

@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    # Clickjacking + sniffing defenses. CSP is intentionally limited to frame-ancestors only — the app is
    # inline-script + CDN heavy, so a restrictive script-src/style-src would break the UI.
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resp
S = {"ledger": None, "user": None, "ready": False, "status": "starting",
     "ingest": {"state": "idle"}, "llm": None, "workspaces": None, "ws_ledgers": {}}

WORKSPACES_PATH = os.path.join(os.path.dirname(__file__), "workspaces.json")
LLM_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "llm_config.json")  # bring-your-own LLM (gitignored — holds an API key)
DEFAULT_WS = {"id": "incidents", "name": "Incidents", "dataset": "main_dataset"}

# "Last reviewed" date per golden runbook — a realistic spread for the Aging-knowledge scan (uploads get a
# REAL timestamp at ingest). Runbooks not reviewed in a while are likely stale by age, even if nothing was forgotten.
# NOTE: legacy-cache is deliberately FRESH. It is the forget-HERO's target (decommissioned manually in the demo),
# so the curation cycle must NOT auto-demote it — that would sink the hero's BEFORE-answer. Keeping it recently
# reviewed cleanly separates the two stories: forget = legacy-cache (manual); curation = the genuinely-stale others.
_REVIEWED_GOLDEN = {
    # Fresh (reviewed within ~180d of the sim date) — healthy runbooks (not flagged by curation).
    "api-gateway": "2026-05-10", "auth-service": "2026-04-20", "session-store": "2026-05-25",
    "payments-service": "2026-06-01", "payments-db": "2026-03-15", "cloud-mailer": "2026-05-05",
    "cdn-transform": "2026-04-01", "deploy-pipeline": "2026-03-01", "notification-service": "2026-02-10",
    "legacy-cache": "2026-06-15",  # FRESH on purpose — owned by the forget-hero, must not be curation-demoted.
    # Aging (>180d, <360d) — MILD auto-demote (reversible; still findable).
    "search-index": "2025-08-01", "cdn": "2025-12-01", "monitoring": "2025-10-01",
    "rate-limiter": "2025-09-15",
    # Very stale (>360d) — DEEP auto-demote + QUEUED for human-approved hard-delete.
    "image-resizer": "2025-01-20", "ownership": "2024-09-01", "email-relay": "2024-08-15",
}


_WS_ID_RE = re.compile(r"ws_[0-9a-f]{6,}\Z")

def _safe_wid(wid):
    """Defense-in-depth: a workspace id is f-stringed into on-disk filenames (ledger_<id>.json etc.), so it
    must never contain path separators or traversal. Only the golden 'incidents' id or 'ws_<hex>' are valid."""
    if wid == "incidents" or (isinstance(wid, str) and _WS_ID_RE.match(wid)):
        return wid
    raise ValueError(f"invalid workspace id: {wid!r}")

def _ws_ledger_path(wid):
    # the default workspace reuses the existing ledger.json (golden); others get their own file
    _safe_wid(wid)
    return ib.LEDGER_PATH if wid == "incidents" else os.path.join(os.path.dirname(ib.LEDGER_PATH), f"ledger_{wid}.json")

def _atomic_write(path, obj):
    """Write JSON durably: dump to `path.tmp`, flush + fsync, then atomically os.replace into place.
    A crash mid-write can never leave a half-written (corrupt) app-state file — the old file stays
    intact until the rename, so the golden ledger.json can't be truncated by an interrupted save."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def _write_secret(path, obj):
    """Like _atomic_write but tightens the file mode to 0600 — used for llm_config.json, which holds the
    bring-your-own API key. Self-host persistence is acceptable; 0600 keeps it owner-only on disk."""
    _atomic_write(path, obj)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

def _read_json(path, fallback):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.warning("corrupt JSON at %s — falling back: %r", path, e)
        except Exception:
            pass
    return fallback

def _save_workspaces():
    _atomic_write(WORKSPACES_PATH, S["workspaces"])

def _get_ws(wid):
    # fail CLOSED: an unknown id must NOT alias to the default (main_dataset/golden) — that would let a
    # stale workspace id route a /forget or /upload into the golden graph. Only empty/"incidents" -> default.
    if not wid or wid == "incidents":
        # resolve the golden by ID, not list position: if the workspaces list were ever reordered, [0]
        # could silently point the default at the wrong dataset. Fall back to DEFAULT_WS if missing.
        for w in (S["workspaces"] or []):
            if w["id"] == "incidents":
                return w
        return DEFAULT_WS
    for w in (S["workspaces"] or []):
        if w["id"] == wid:
            return w
    return None

def _ws_led(wid):
    return S["ws_ledgers"].setdefault(wid, {})

def _save_ws_led(wid):
    _atomic_write(_ws_ledger_path(wid), S["ws_ledgers"].get(wid, {}))

def _events_path(wid):
    _safe_wid(wid)
    return os.path.join(os.path.dirname(__file__), f"events_{wid}.json")

def _append_event(wid, op, system, detail=None):
    """Append one memory event (reviewed/added/forgotten) to the workspace timeline.
    The golden 'incidents' timeline is IN-MEMORY ONLY (not persisted): a reset_demo + restart
    rebuilds it from the seeded review checkpoints, so a demo forget never leaves a stale
    'legacy-cache forgotten' entry behind to contradict the restored golden graph (hero-safe)."""
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    # Stable per-event id (hero-safe addition): lets a forget event be addressed by URL for its
    # Certificate of Erasure. Derived from ts+op+system so it is deterministic, short and url-safe.
    eid = "ev_" + hashlib.sha256(f"{ts}|{op}|{system}".encode("utf-8")).hexdigest()[:12]
    ev = {"id": eid, "ts": ts, "op": op, "system": system, "detail": detail or {}}
    S.setdefault("events", {}).setdefault(wid, []).append(ev)
    if wid != "incidents":
        try:
            _atomic_write(_events_path(wid), S["events"][wid])
        except Exception:
            pass
    return ev

def _ws_curation_path(wid):
    _safe_wid(wid)
    return os.path.join(os.path.dirname(__file__), f"curation_{wid}.json")

def _save_ws_curation(wid):
    """Persist a user workspace's curation state (doc texts, tombstones, review dates) so curation
    scans + answer citations survive a restart. 'incidents' is golden/in-memory-seeded — never persisted
    (a reset_demo + restart must restore the clean baseline), so it's a no-op there."""
    if wid == "incidents":
        return
    try:
        _atomic_write(_ws_curation_path(wid), {
            "texts": S.get("texts", {}).get(wid, {}),
            "tombstones": sorted(S.get("tombstones", {}).get(wid, set())),
            "reviewed": S.get("reviewed", {}).get(wid, {}),
            "demoted": sorted(S.get("demoted", {}).get(wid, set())),
        })
    except Exception:
        pass


def _llm_info():
    """Which LLM is serving answers right now (read from env). Drives the in-app 'active model' badge."""
    prov = (os.environ.get("LLM_PROVIDER") or "custom").lower()
    model = os.environ.get("LLM_MODEL") or ""
    endp = os.environ.get("LLM_ENDPOINT") or ""
    local = prov == "ollama" or "localhost" in endp or "127.0.0.1" in endp
    label = model.split("/")[-1] if model else (prov or "model")  # llama-3.3-70b-instruct / gemma4:e4b
    return {"provider": prov, "model": model, "label": label, "local": local}


def _validate_llm_endpoint(url):
    """SSRF guard for a bring-your-own-key LLM endpoint. Returns (ok: bool, reason: str).

    The endpoint base_url drives the OUTBOUND request that carries the server's API key, so an attacker who
    can set it could (a) hit cloud-metadata (169.254.169.254) or internal RFC1918 hosts, or (b) DNS-rebind a
    public-looking name to loopback. We resolve the host and reject any private/link-local/reserved IP. Local
    Ollama is a supported feature, so loopback is allowed — but ONLY when the host is literally written as
    localhost/127.0.0.1/::1 (a NAME that merely resolves to loopback is rejected, defeating DNS rebinding).
    Empty url = no override (use the provider default) → allowed."""
    url = (url or "").strip()
    if not url:
        return True, ""
    try:
        p = urlparse(url)
    except Exception as e:
        return False, f"could not parse the endpoint URL ({e})"
    if p.scheme not in ("http", "https"):
        return False, "endpoint must use http or https"
    host = p.hostname
    if not host:
        return False, "endpoint is missing a hostname"
    literal_loopback = host in ("localhost", "127.0.0.1", "::1")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except Exception as e:
        return False, f"could not resolve the endpoint host ({e})"
    ips = {info[4][0] for info in infos}
    if not ips:
        return False, "endpoint host did not resolve to any address"
    for ip_s in ips:
        try:
            ip = ipaddress.ip_address(ip_s.split("%")[0])  # strip any IPv6 zone id (fe80::1%en0)
        except ValueError:
            return False, f"endpoint host resolved to an unparseable address ({ip_s})"
        if ip.is_loopback:
            # loopback is allowed ONLY for a literal localhost/127.0.0.1/::1 host (anti DNS-rebind)
            if literal_loopback:
                continue
            return False, "endpoint host resolves to loopback but is not a literal localhost address"
        if (ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast
                or ip.is_unspecified or getattr(ip, "is_site_local", False)):
            return False, f"endpoint resolves to a private/link-local/reserved address ({ip_s}) — refusing (SSRF guard)"
    # non-loopback hosts must use https (don't ship the key in cleartext to a public host)
    if not literal_loopback and p.scheme != "https":
        return False, "non-local endpoints must use https"
    return True, ""


def _apply_llm_config(c):
    """Apply an LLM config to cognee AT RUNTIME (bring-your-own-key) and mirror it into the env so the
    /health 'active model' badge reflects it. c = {provider, model, endpoint, api_key}."""
    prov = (c.get("provider") or "openai").strip()
    cognee.config.set_llm_provider(prov)
    os.environ["LLM_PROVIDER"] = prov
    if c.get("model"):
        cognee.config.set_llm_model(c["model"].strip()); os.environ["LLM_MODEL"] = c["model"].strip()
    ep = (c.get("endpoint") or "").strip()
    cognee.config.set_llm_endpoint(ep or None); os.environ["LLM_ENDPOINT"] = ep
    if c.get("api_key"):
        cognee.config.set_llm_api_key(c["api_key"].strip()); os.environ["LLM_API_KEY"] = c["api_key"].strip()


@app.on_event("startup")
async def _startup():
    if not _AUTH_TOKEN:
        logger.warning("LETHE_AUTH_TOKEN is NOT set — the API is open to LOOPBACK ONLY. Remote callers get 403. "
                       "Set LETHE_AUTH_TOKEN before any public/non-local deploy.")
    S["llm_default"] = {"provider": os.environ.get("LLM_PROVIDER", ""), "model": os.environ.get("LLM_MODEL", ""),
                        "endpoint": os.environ.get("LLM_ENDPOINT", ""), "api_key": os.environ.get("LLM_API_KEY", "")}
    _saved = _read_json(LLM_CONFIG_PATH, None)
    if _saved:
        ok, why = _validate_llm_endpoint(_saved.get("endpoint", ""))
        if not ok:
            logger.warning("saved llm_config.json endpoint rejected (SSRF guard) — skipping re-apply: %s", why)
        else:
            try:
                _apply_llm_config(_saved)
            except Exception as e:
                logger.warning("llm config apply failed: %r", e)
    S["llm"] = _llm_info()
    S["workspaces"] = _read_json(WORKSPACES_PATH, [DEFAULT_WS])
    # Drop any workspace with an invalid id — its id is used to build on-disk filenames (path-safety).
    _clean_ws = []
    for w in (S["workspaces"] or []):
        try:
            _safe_wid(w.get("id"))
            _clean_ws.append(w)
        except ValueError:
            logger.warning("dropping workspace with invalid id %r from %s", w.get("id"), WORKSPACES_PATH)
    S["workspaces"] = _clean_ws or [DEFAULT_WS]
    # The default golden workspace MUST be reachable by id "incidents" (that id routes /ask, /forget,
    # etc. to main_dataset). If it's missing, _get_ws falls back to DEFAULT_WS — log loudly either way.
    if not any(w.get("id") == "incidents" for w in (S["workspaces"] or [])):
        logger.warning("no workspace with id 'incidents' in %s — falling back to DEFAULT_WS for the golden", WORKSPACES_PATH)
    else:
        logger.info("default Incidents workspace present (id='incidents', dataset='%s').", DEFAULT_WS["dataset"])
    led = ib.load_ledger()  # the Incidents (main_dataset) ledger = ledger.json
    S["ws_ledgers"]["incidents"] = led or {}
    for w in S["workspaces"]:
        if w["id"] != "incidents":
            S["ws_ledgers"][w["id"]] = _read_json(_ws_ledger_path(w["id"]), {})
    # curation: keep the source-doc text per system so we can scan for references to decommissioned
    # systems WITHOUT calling the LLM (deterministic, zero tokens). Seed the golden set from the wiki.
    S["texts"] = {"incidents": {}}
    S["tombstones"] = {"incidents": set()}
    for _sys, _txt in ib.WIKI:
        if _sys in (led or {}):
            S["texts"]["incidents"].setdefault(_sys, []).append(_txt)
    S["reviewed"] = {"incidents": {k: v for k, v in _REVIEWED_GOLDEN.items() if k in (led or {})}}
    # demoted = systems the curation cycle has down-weighted (reversible). Golden starts NEUTRAL and the set
    # is in-memory only, so reset_demo (restores neutral graph weights) + restart is the single source of
    # truth for golden. User workspaces persist it so a demote survives a restart.
    S["demoted"] = {"incidents": set()}
    # user workspaces: reload their persisted curation state (texts/tombstones/reviewed/demoted) so curation
    # scans + citations survive a restart. (incidents stays the in-memory golden seed above.)
    for w in S["workspaces"]:
        if w["id"] != "incidents":
            cur = _read_json(_ws_curation_path(w["id"]), {})
            S["texts"][w["id"]] = cur.get("texts", {})
            S["tombstones"][w["id"]] = set(cur.get("tombstones", []))
            S["reviewed"][w["id"]] = cur.get("reviewed", {})
            S["demoted"][w["id"]] = set(cur.get("demoted", []))
    # Memory timeline — a durable audit log of memory events (review checkpoints + live add/forget).
    # Golden (incidents) is seeded IN-MEMORY from the known review dates and is NOT persisted, so a
    # reset_demo + restart returns the timeline to this clean baseline (mirrors the golden graph restore).
    S["events"] = {"incidents": [
        {"ts": v, "op": "reviewed", "system": k, "detail": {}}
        for k, v in S["reviewed"]["incidents"].items()
    ]}
    for w in S["workspaces"]:
        if w["id"] != "incidents":
            S["events"][w["id"]] = _read_json(_events_path(w["id"]), [])
    if led:
        S["ledger"] = led
        S["user"] = await get_default_user()
        S["ready"] = True
        S["status"] = "ready"
    else:
        S["status"] = "no graph built yet — run:  ./.venv/bin/python scripts/setup.py"


class AskReq(BaseModel):
    query: str
    history: list = []
    workspace: str = "incidents"

class ForgetReq(BaseModel):
    system: str
    workspace: str = "incidents"

class ReviewReq(BaseModel):
    system: str
    workspace: str = "incidents"

class RestoreReq(BaseModel):
    system: str
    workspace: str = "incidents"

class UploadReq(BaseModel):
    text: str
    system: str = "uploaded"
    filename: str = ""
    workspace: str = "incidents"

class WsReq(BaseModel):
    name: str

class WsDelReq(BaseModel):
    id: str

class LlmCfgReq(BaseModel):
    provider: str = "openai"
    model: str = ""
    endpoint: str = ""
    api_key: str = ""


@app.get("/health")
async def health(request: Request):
    out = {"ready": S["ready"], "status": S["status"], "auth": bool(_AUTH_TOKEN), "demo": _PUBLIC_DEMO}
    # Only expose the system inventory + active LLM when auth is OFF (local self-host) OR the caller is
    # authenticated. Avoids leaking the system list + provider/model to anonymous remote callers. The UI
    # always sends the token when one is set, so its badge + system count stay populated.
    if not _AUTH_TOKEN or _is_authenticated(request):
        out["systems"] = list((S["ledger"] or {}).keys())
        out["llm"] = S["llm"]
    return out


@app.get("/llm-config")
async def llm_config_get():
    info = S["llm"] or _llm_info()
    return {"provider": info["provider"], "model": info["model"], "endpoint": os.environ.get("LLM_ENDPOINT", ""),
            "label": info["label"], "local": info["local"], "has_key": bool(os.environ.get("LLM_API_KEY"))}


@app.post("/llm-config")
async def llm_config_set(r: LlmCfgReq):
    """Bring-your-own-key: apply the user's LLM at runtime. Validate the CANDIDATE first (a non-mutating
    probe), and only commit to the global config once it answers — so a bad key never replaces the working
    provider mid-request."""
    if _PUBLIC_DEMO:
        # Single-tenant: a model switch here is GLOBAL. On the shared hosted demo that would let any
        # visitor rewire (or break) the instance for everyone — so the demo runs a fixed model.
        return {"ok": False, "error": "The hosted demo runs a fixed model. Run Lethe yourself to bring your own key."}
    cfg = {"provider": r.provider, "model": r.model, "endpoint": r.endpoint, "api_key": r.api_key}
    # SSRF guard: the probe below (and every later /ask) makes an OUTBOUND request to this endpoint carrying
    # the API key, so validate the destination BEFORE any network call. Rejects cloud-metadata / RFC1918 /
    # DNS-rebind-to-loopback; allows the legit public providers + literal localhost Ollama.
    ok, why = _validate_llm_endpoint(r.endpoint)
    if not ok:
        return {"ok": False, "error": "That endpoint isn't allowed — " + why}
    # Probe the candidate WITHOUT touching globals: pass model/api_key/api_base directly to litellm
    # (mirrors the conflict-scan probe). Time-bounded — a bad key makes litellm retry for minutes.
    try:
        await asyncio.wait_for(litellm.acompletion(
            model=(r.model or "").strip(), api_key=(r.api_key or "").strip() or None,
            api_base=(r.endpoint or "").strip() or None, temperature=0, max_tokens=8,
            messages=[{"role": "user", "content": "ping"}]), timeout=25)
    except Exception as e:
        logger.warning("llm-config probe failed: %r", e)
        why = "no response in time — check the model name and endpoint" if isinstance(e, asyncio.TimeoutError) else "could not apply — check the provider, model name, key, and endpoint"
        return {"ok": False, "error": "That provider/key didn't work — not applied. (" + why + ")"}
    # Probe passed — NOW commit it to the global config.
    _apply_llm_config(cfg)
    _write_secret(LLM_CONFIG_PATH, cfg)  # 0600 — holds the API key
    S["llm"] = _llm_info()
    return {"ok": True, "llm": S["llm"]}


@app.post("/llm-config/reset")
async def llm_config_reset():
    """Forget the bring-your-own key and fall back to the server's default (.env) provider."""
    if _PUBLIC_DEMO:
        return {"ok": False, "error": "The hosted demo runs a fixed model. Run Lethe yourself to bring your own key."}
    _apply_llm_config(S.get("llm_default") or {})
    try:
        os.remove(LLM_CONFIG_PATH)
    except Exception:
        pass
    S["llm"] = _llm_info()
    return {"ok": True, "llm": S["llm"]}


@app.get("/systems")
async def systems(workspace: str = "incidents"):
    ws = _get_ws(workspace)
    if ws is None:
        return {"systems": []}
    led = _ws_led(ws["id"])
    reviewed = S.get("reviewed", {}).get(ws["id"], {})
    demoted = S.get("demoted", {}).get(ws["id"], set())
    today = datetime.date.today()
    out = []
    for k, v in led.items():
        iso = reviewed.get(k)
        age = None
        if iso:
            try:
                age = (today - datetime.date.fromisoformat(iso)).days
            except Exception:
                pass
        out.append({"name": k, "docs": len([d for d in v if d]), "reviewed": iso, "age_days": age, "demoted": k in demoted})
    return {"systems": out, "stale_days": 180}


@app.get("/source/{system}")
async def source(system: str, workspace: str = "incidents"):
    """Read-only peek at the original wiki docs behind a citation chip (golden 'incidents' only).
    404s when the system is NOT in the LIVE ledger — so after a forget the peek vanishes too and
    can never contradict Proof-of-Forgetting. Docs come from the static WIKI (cognee-free)."""
    if workspace != "incidents":
        return JSONResponse({"error": "source peek is only available for the demo workspace"}, status_code=404)
    if system not in _ws_led("incidents"):
        return JSONResponse({"error": "not in the live ledger"}, status_code=404)
    docs = []
    for tag, text in ib.WIKI:
        if tag != system:
            continue
        prefix = text.split(":", 1)[0].strip()
        title = ("Runbook: " + system) if prefix.lower() == "runbook" else prefix
        docs.append({"title": title, "text": text})
    if not docs:
        return JSONResponse({"error": "no source docs"}, status_code=404)
    return {"docs": docs}


@app.get("/curation")
async def curation(workspace: str = "incidents"):
    """Memory hygiene — scan the REMAINING docs for references to systems that were decommissioned.
    Pure text matching: deterministic, ZERO LLM calls (the first guardrail is spending no tokens at all)."""
    ws = _get_ws(workspace)
    if ws is None:
        return {"findings": [], "scanned": 0, "decommissioned": []}
    texts = S.get("texts", {}).get(ws["id"], {})
    tombs = S.get("tombstones", {}).get(ws["id"], set())
    findings, scanned = [], 0
    for sys, txts in texts.items():
        for txt in txts:
            scanned += 1
            low = txt.lower()
            for dead in tombs:
                if re.search(r"\b" + re.escape(dead.lower()) + r"\b", low):
                    i = low.find(dead.lower())
                    findings.append({"system": sys, "stale_ref": dead,
                                     "snippet": txt[max(0, i - 45):i + len(dead) + 45].strip()})
    return {"findings": findings, "scanned": scanned, "decommissioned": sorted(tombs)}


_CONFLICT_SYS = (
    "You check two on-call runbook excerpts for CONTRADICTIONS. They overlap on some system. Reply EXACTLY "
    "'CONSISTENT' if the two excerpts CAN BOTH BE TRUE at the same time. Only reply "
    "'CONFLICT: <one sentence naming it>' when they state facts that CANNOT both be true about the same thing "
    "(a direct contradiction, e.g. 'uses Redis' vs 'uses Memcached'). Complementary or layered facts are NOT "
    "conflicts — e.g. a cache sitting in FRONT OF a store (both true), A depending on B, or one excerpt simply "
    "adding detail the other omits. When unsure, answer CONSISTENT."
)
_CONFLICT_MAX_PAIRS = 8  # hard token guardrail: never run more than this many LLM checks per scan


def _conflict_pairs(texts):
    """Candidate pairs = docs that SHARE a mentioned known-system name (deterministic pre-filter, no LLM).
    This keeps the LLM off unrelated docs — the first guardrail before spending any tokens."""
    names = set(texts.keys())
    docs = []
    for sys, txts in texts.items():
        for t in txts:
            low = t.lower()
            mentioned = {n for n in names if re.search(r"\b" + re.escape(n.lower()) + r"\b", low)}
            mentioned.add(sys)
            docs.append((sys, t, mentioned))
    pairs = []
    for i in range(len(docs)):
        for j in range(i + 1, len(docs)):
            if docs[i][0] != docs[j][0] and (docs[i][2] & docs[j][2]):
                pairs.append((docs[i], docs[j]))
    return pairs


@app.get("/curation/conflicts")
async def curation_conflicts(workspace: str = "incidents"):
    """Curation step 2 — flag CONTRADICTORY runbooks. Bounded: deterministic pre-filter + a hard cap on
    LLM checks per scan. Each check is a tiny, time-bounded completion (temp 0, max 80 tokens)."""
    ws = _get_ws(workspace)
    if ws is None:
        return {"conflicts": [], "pairs_checked": 0, "pairs_total": 0, "capped": False}
    texts = S.get("texts", {}).get(ws["id"], {})
    pairs = _conflict_pairs(texts)
    capped = pairs[:_CONFLICT_MAX_PAIRS]
    model = os.environ.get("LLM_MODEL"); key = os.environ.get("LLM_API_KEY"); base = os.environ.get("LLM_ENDPOINT") or None
    conflicts = []
    for (sa, ta, _), (sb, tb, _) in capped:
        try:
            r = await asyncio.wait_for(litellm.acompletion(
                model=model, api_key=key, api_base=base, temperature=0, max_tokens=80,
                messages=[{"role": "system", "content": _CONFLICT_SYS},
                          {"role": "user", "content": f'Excerpt A: "{ta}"\n\nExcerpt B: "{tb}"'}]), timeout=12)
            verdict = (r.choices[0].message.content or "").strip()
        except Exception:
            verdict = "CONSISTENT"
        if verdict.upper().startswith("CONFLICT"):
            conflicts.append({"a": sa, "b": sb, "detail": verdict.split(":", 1)[1].strip() if ":" in verdict else verdict})
    return {"conflicts": conflicts, "pairs_checked": len(capped), "pairs_total": len(pairs), "capped": len(pairs) > _CONFLICT_MAX_PAIRS}


@app.get("/curation/aging")
async def curation_aging(workspace: str = "incidents", days: int = 180):
    """Flag runbooks not reviewed in over `days` days — likely stale by AGE (not because anything was
    forgotten). Pure date math: deterministic, 0 tokens. Completes the curation trilogy."""
    ws = _get_ws(workspace)
    if ws is None:
        return {"aging": [], "scanned": 0, "days": days}
    reviewed = S.get("reviewed", {}).get(ws["id"], {})
    today = datetime.date.today()
    aging = []
    for system, iso in reviewed.items():
        try:
            age = (today - datetime.date.fromisoformat(iso)).days
        except Exception:
            continue
        if age > days:
            aging.append({"system": system, "reviewed": iso, "age_days": age})
    aging.sort(key=lambda x: x["age_days"], reverse=True)
    return {"aging": aging, "scanned": len(reviewed), "days": days}


@app.get("/curation/proposals")
async def curation_proposals(workspace: str = "incidents", days: int = 365):
    """Forget-as-policy: surface DETERMINISTIC retirement CANDIDATES so curation becomes proactive instead
    of a manual per-system hunt. Primary, defensible signal = AGE — runbooks not reviewed in over `days`
    days (a HIGHER bar than the normal 180-day aging scan, so this is the 'long overdue, likely retired'
    tail). PROPOSALS ONLY: this route is read-only and NEVER mutates anything — the human still confirms
    each forget through the existing Proof-of-Forgetting flow. Tombstoned systems are already excluded
    (forget pops them from `reviewed`); structural items never carry a review date so they don't appear.
    Pure date math: deterministic, 0 tokens."""
    ws = _get_ws(workspace)
    if ws is None:
        return {"proposals": [], "scanned": 0, "days": days}
    reviewed = S.get("reviewed", {}).get(ws["id"], {})
    today = datetime.date.today()
    proposals = []
    for system, iso in reviewed.items():
        try:
            age = (today - datetime.date.fromisoformat(iso)).days
        except Exception:
            continue
        if age > days:
            yrs = age / 365
            proposals.append({
                "system": system, "age_days": age, "last_reviewed": iso,
                "reasons": [f"Not reviewed in {age} days (~{yrs:.1f} years) — well past the {days}-day retirement bar."],
            })
    proposals.sort(key=lambda x: x["age_days"], reverse=True)
    return {"proposals": proposals, "scanned": len(reviewed), "days": days}


@app.post("/curation/review")
async def curation_review(r: ReviewReq):
    """Close the Aging detection→action loop: mark a runbook reviewed-today. Refreshes its review date
    (so it leaves the overdue list) and lands a `reviewed` event on the Memory Timeline. Active curation,
    not just a report. Deterministic, 0 tokens (a date write + a timeline event)."""
    ws = _get_ws(r.workspace)
    if ws is None:
        return {"ok": False, "message": "That workspace no longer exists."}
    reviewed = S.setdefault("reviewed", {}).setdefault(ws["id"], {})
    if r.system not in reviewed:
        return {"ok": False, "message": f"'{r.system}' is not a tracked runbook."}
    today = datetime.date.today().isoformat()
    reviewed[r.system] = today
    # SELF-HEAL: re-reviewing a runbook means it's current again — if the curation cycle had demoted it,
    # automatically restore its ranking. The loop closes itself: stale sinks, re-reviewed resurfaces.
    healed = False
    dset = S.setdefault("demoted", {}).setdefault(ws["id"], set())
    if r.system in dset:
        ledger = ib.load_ledger() if ws["id"] == "incidents" else _ws_led(ws["id"])
        if ledger and r.system in ledger:
            n = await ib.restore_system(r.system, ledger, dataset=ws["dataset"], user=S["user"])
            dset.discard(r.system)
            _append_event(ws["id"], "restored", r.system, {"nodes": n, "via": "self-heal"})
            healed = True
    _save_ws_curation(ws["id"])                                                    # persist so it survives restart
    _append_event(ws["id"], "reviewed", r.system, {})
    return {"ok": True, "system": r.system, "reviewed": today, "age_days": 0, "self_healed": healed}


class CurationCycleReq(BaseModel):
    workspace: str = "incidents"
    dry_run: bool = True
    days: int = 180


@app.post("/curation/cycle")
async def curation_cycle(r: CurationCycleReq):
    """Run ONE bounded curation/decay pass: AUTO-DEMOTE aging systems (reversible) + QUEUE hard-deletes for
    human approval. dry_run=True (default) reports the plan WITHOUT mutating. On apply, each demote is
    recorded to the Timeline. Never auto-deletes — the irreversible action always waits for a human."""
    if not S["ready"]:
        return {"error": "not ready", "auto_demoted": [], "queued_for_approval": [], "health": {}}
    ws = _get_ws(r.workspace)
    if ws is None or (ws["id"] != "incidents" and not _ws_led(ws["id"])):
        return {"error": "unknown workspace", "auto_demoted": [], "queued_for_approval": [], "health": {}}
    ledger = ib.load_ledger() if ws["id"] == "incidents" else _ws_led(ws["id"])
    reviewed = S.get("reviewed", {}).get(ws["id"], {})
    receipt = await ib.run_curation_cycle(ledger or {}, reviewed, aging_days=r.days,
                                          dataset=ws["dataset"], user=S["user"], dry_run=r.dry_run)
    if not r.dry_run:
        dset = S.setdefault("demoted", {}).setdefault(ws["id"], set())
        for item in receipt.get("auto_demoted", []):
            dset.add(item.get("system"))
            _append_event(ws["id"], "demoted", item.get("system"),
                          {"weight": item.get("weight"), "nodes": item.get("nodes"), "reason": item.get("reason")})
        _save_ws_curation(ws["id"])   # persist the demoted set (no-op for golden 'incidents')
    return receipt


@app.post("/curation/restore")
async def curation_restore(r: RestoreReq):
    """Undo a curation demote: restore a system's nodes to the neutral weight so its advice can surface
    again. This is the reversibility that distinguishes soft `demote` from the permanent `forget` — the
    same data, brought back without a re-ingest. Lands a `restored` event on the Timeline."""
    if not S["ready"]:
        return {"ok": False, "message": "not ready"}
    ws = _get_ws(r.workspace)
    if ws is None or (ws["id"] != "incidents" and not _ws_led(ws["id"])):
        return {"ok": False, "message": "unknown workspace"}
    ledger = ib.load_ledger() if ws["id"] == "incidents" else _ws_led(ws["id"])
    if not ledger or r.system not in ledger:
        return {"ok": False, "message": f"'{r.system}' is not a tracked system."}
    n = await ib.restore_system(r.system, ledger, dataset=ws["dataset"], user=S["user"])
    S.setdefault("demoted", {}).setdefault(ws["id"], set()).discard(r.system)
    _save_ws_curation(ws["id"])   # persist (no-op for golden 'incidents')
    _append_event(ws["id"], "restored", r.system, {"nodes": n})
    return {"ok": True, "system": r.system, "nodes": n}


@app.get("/timeline")
async def timeline(workspace: str = "incidents"):
    """Memory timeline — a durable, chronological audit log of this workspace's memory events:
    review checkpoints + live add/forget (every forget carries its real removal receipt). On-thesis
    governance: prove WHAT was forgotten and WHEN. Deterministic, 0 tokens (just reads the event log)."""
    ws = _get_ws(workspace)
    if ws is None:
        return {"events": []}
    # newest-first; append-order (enumerate index) breaks same-second ties so the latest action always leads
    raw = S.get("events", {}).get(ws["id"], [])
    evs = [e for _, e in sorted(enumerate(raw), key=lambda p: (p[1].get("ts", ""), p[0]), reverse=True)]
    return {"events": evs}


def _fmt_human_ts(ts):
    """ISO timestamp -> a formal, human-readable string for the certificate (e.g. '15 May 2026, 14:32 UTC-ish
    local'). Best-effort: if parsing fails, return the raw string. We label it 'local time' honestly rather
    than fabricate a timezone the stored timestamp doesn't carry."""
    raw = str(ts or "")
    try:
        dt = datetime.datetime.fromisoformat(raw)
        return dt.strftime("%d %B %Y at %H:%M") + " (local time)"
    except Exception:
        return raw


def _build_certificate(ws, ev):
    """Assemble the Certificate of Erasure record from a stored 'forgotten' timeline event — purely from
    persisted data, no cognee/LLM calls. Returns (cert_dict, cert_id). The cert_id is a sha256 content hash
    over the canonical (sorted-key) JSON of the load-bearing fields, truncated to 16 hex chars: it lets
    anyone re-derive it from the printed fields to detect tampering. It is NOT a signature."""
    d = ev.get("detail") or {}
    ts = ev.get("ts") or ""
    # Only include fields we actually have — never fabricate. Missing numeric fields stay None and are
    # rendered honestly in the HTML ('—' / omitted) rather than invented.
    fields = {
        "system": ev.get("system") or "",
        "workspace_id": ws.get("id") or "",
        "workspace_name": d.get("workspace_name") or ws.get("name") or "",
        "dataset": d.get("dataset") or ws.get("dataset") or "",
        "timestamp_iso": ts,
        "docs_removed": d.get("docs"),
        "nodes_removed": d.get("nodes_removed"),
        "edges_removed": d.get("edges_removed"),
        "nodes_after": d.get("nodes_after"),
        "edges_after": d.get("edges_after"),
        "proof_query": d.get("proof_query") or "",
        "proof_answer": d.get("proof_answer") or "",
        "model": d.get("model") or "",
        "model_label": d.get("model_label") or "",
        "provider": d.get("provider") or "",
        "event_id": ev.get("id") or "",
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    cert_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    cert = dict(fields)
    cert["certificate_id"] = cert_id
    cert["timestamp_human"] = _fmt_human_ts(ts)
    return cert, cert_id


# Brand wave mark (reused from the app/landing header) — the Lethe wordmark sits beside it on the cert.
_WAVE_MARK = ('<svg width="34" height="30" viewBox="0 0 26 24" fill="none" stroke="#0891b2" stroke-width="2.2" '
              'stroke-linecap="round" aria-hidden="true"><path d="M2 12c2.2-4 4.4-4 6.6 0s4.4 4 6.6 0"/>'
              '<path d="M15.2 12c2.2-4 4.4-4 6.6 0" opacity=".4"/></svg>')


def _cert_html(cert):
    """Render a STANDALONE, print-optimized one-page Certificate of Erasure. Enterprise/formal: Instrument
    Serif display heading, Geist Mono for data/hash, hairline rules, the Lethe wave mark, clean @media print.
    All values come from `cert` (real stored data); missing fields are omitted honestly."""
    import html as _html
    esc = lambda v: _html.escape(str(v)) if v is not None else ""
    sys_name = cert.get("system") or "—"
    ws_label = cert.get("workspace_name") or cert.get("workspace_id") or "—"
    dataset = cert.get("dataset") or ""
    model = cert.get("model") or ""
    model_label = cert.get("model_label") or model
    ts_human = cert.get("timestamp_human") or ""
    ts_iso = cert.get("timestamp_iso") or ""
    cert_id = cert.get("certificate_id") or ""
    proof_q = cert.get("proof_query") or ""
    proof_a = cert.get("proof_answer") or ""

    # Measured-deletion metric tiles — only render a tile when its value is actually present.
    def _metric(val, label):
        if val is None:
            return ""
        return ('<div class="metric"><div class="metric-n">' + esc(val) + '</div>'
                '<div class="metric-l">' + esc(label) + '</div></div>')
    metrics = (_metric(cert.get("docs_removed"), "documents")
               + _metric(cert.get("nodes_removed"), "graph nodes")
               + _metric(cert.get("edges_removed"), "relationships"))
    if not metrics:
        metrics = '<div class="metric-empty">Deletion counts were not recorded for this event.</div>'

    # Optional rows in the "Record" table — skip rows with no real value.
    rows = []
    rows.append(("Erased subject", '<span class="mono">' + esc(sys_name) + '</span>'))
    rows.append(("Workspace / dataset", esc(ws_label) + (' &middot; <span class="mono">' + esc(dataset) + '</span>' if dataset else '')))
    if ts_human or ts_iso:
        rows.append(("Date of erasure", esc(ts_human) + (' <span class="mono dim">&middot; ' + esc(ts_iso) + '</span>' if ts_iso else '')))
    if model or model_label:
        rows.append(("Model in service", '<span class="mono">' + esc(model_label) + '</span>' + (' <span class="dim">(' + esc(model) + ')</span>' if model and model != model_label else '')))
    rows_html = "".join('<tr><th>' + k + '</th><td>' + v + '</td></tr>' for k, v in rows)

    # Verification block (post-deletion re-query) — only if a proof was recorded.
    if proof_q or proof_a:
        verify = ('<section class="block"><div class="kicker">Verification &middot; post-deletion re-query</div>'
                  '<div class="proof">'
                  + ('<div class="proof-q"><span class="proof-tag">QUERY</span><span>' + esc(proof_q) + '</span></div>' if proof_q else '')
                  + ('<div class="proof-a"><span class="proof-tag">ANSWER</span><span>' + esc(proof_a) + '</span></div>' if proof_a else '')
                  + '</div><p class="proof-note">A query for the erased subject after deletion returns no recollection &mdash; confirming it is no longer retrievable from the memory graph or vector store.</p>'
                  '</section>')
    else:
        verify = ""

    # Formal attestation sentence — dated from the stored erasure date when available.
    when = ts_human.split(" at ")[0] if ts_human else (ts_iso[:10] if ts_iso else "the recorded date")
    attest = ('This certifies that the knowledge above was permanently and verifiably removed from '
              'Lethe&rsquo;s memory graph and vector store on ' + esc(when) + '; a post-deletion query '
              'confirms it is no longer retrievable.')

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Certificate of Erasure &middot; """ + esc(sys_name) + """ &middot; Lethe</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 26 24' fill='none' stroke='%2322d3ee' stroke-width='2.4' stroke-linecap='round'><path d='M2 12c2.2-4 4.4-4 6.6 0s4.4 4 6.6 0'/><path d='M15.2 12c2.2-4 4.4-4 6.6 0' opacity='.4'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
  :root{--ink:#0f172a;--muted:#64748b;--faint:#94a3b8;--line:#e2e8f0;--line2:#cbd5e1;--brand:#0891b2;--paper:#ffffff;--wash:#f6f8fc}
  @media (prefers-color-scheme:dark){:root{--wash:#0a0e1a;--brand:#22d3ee}}  /* keep the sheet white "paper"; just darken the desk so it doesn't white-flash from a dark-mode demo */
  *{box-sizing:border-box}
  html,body{margin:0;padding:0;background:var(--wash);color:var(--ink);font-family:'Geist',system-ui,-apple-system,sans-serif;-webkit-font-smoothing:antialiased}
  .serif{font-family:'Instrument Serif','Spectral',Georgia,serif;font-weight:400}
  .mono{font-family:'Geist Mono',ui-monospace,SFMono-Regular,monospace}
  .dim{color:var(--faint)}
  .wrap{max-width:760px;margin:40px auto;padding:0 20px}
  .sheet{background:var(--paper);border:1px solid var(--line);border-radius:6px;padding:56px 60px 48px;box-shadow:0 1px 2px rgba(15,23,42,.04),0 12px 40px -12px rgba(15,23,42,.10);position:relative}
  .topbar{display:flex;align-items:center;gap:9px;padding-bottom:22px;border-bottom:1px solid var(--line)}
  .topbar .wordmark{font-size:21px;letter-spacing:-.01em}
  .topbar .tag{margin-left:auto;font-size:10.5px;letter-spacing:.22em;text-transform:uppercase;color:var(--faint)}
  .head{text-align:center;padding:38px 0 8px}
  .head .eyebrow{font-size:11px;letter-spacing:.34em;text-transform:uppercase;color:var(--brand);font-weight:500}
  .head h1{font-size:52px;line-height:1.04;margin:12px 0 0;letter-spacing:-.01em}
  .head .subject{margin-top:14px;font-size:15px;color:var(--muted)}
  .head .subject .mono{color:var(--ink);font-size:14px}
  .rule{height:1px;background:var(--line);margin:34px 0}
  .rule.soft{background:var(--line)}
  .block{margin:30px 0}
  .kicker{font-size:10.5px;letter-spacing:.2em;text-transform:uppercase;color:var(--faint);margin-bottom:13px;font-weight:500}
  table.record{width:100%;border-collapse:collapse}
  table.record th{text-align:left;font-weight:500;color:var(--muted);font-size:12.5px;padding:9px 0;width:38%;vertical-align:top;border-bottom:1px solid var(--line)}
  table.record td{text-align:left;font-size:13.5px;padding:9px 0;border-bottom:1px solid var(--line);vertical-align:top}
  .metrics{display:flex;gap:14px}
  .metric{flex:1;border:1px solid var(--line);border-radius:5px;padding:18px 10px;text-align:center;background:#f6f8fc}
  .metric-n{font-family:'Geist Mono',monospace;font-size:30px;font-weight:500;letter-spacing:-.02em;color:var(--ink)}
  .metric-l{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);margin-top:5px}
  .metric-empty{flex:1;border:1px dashed var(--line2);border-radius:5px;padding:16px;text-align:center;color:var(--faint);font-size:12.5px}
  .measured-note{margin-top:12px;font-size:12px;color:var(--muted);line-height:1.55}
  .proof{border:1px solid var(--line);border-radius:6px;overflow:hidden}
  .proof-q,.proof-a{display:flex;gap:12px;padding:13px 16px;font-size:13px;line-height:1.5}
  .proof-q{background:var(--wash);border-bottom:1px solid var(--line)}
  .proof-tag{font-family:'Geist Mono',monospace;font-size:9.5px;letter-spacing:.14em;color:var(--faint);padding-top:3px;min-width:50px}
  .proof-a span:last-child{color:var(--ink)}
  .proof-note{margin:12px 2px 0;font-size:12px;color:var(--muted);line-height:1.55}
  .attest{margin:30px 0 6px;font-size:14.5px;line-height:1.7;color:var(--ink)}
  .attest .serif{font-style:italic;font-size:16px}
  .certid{margin-top:34px;border:1px solid var(--line2);border-radius:6px;padding:18px 20px;display:flex;align-items:center;gap:18px;background:var(--wash)}
  .certid .label{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--faint);margin-bottom:6px}
  .certid .hash{font-family:'Geist Mono',monospace;font-size:19px;letter-spacing:.04em;color:var(--ink);word-break:break-all}
  .certid .frame{font-size:11px;color:var(--muted);margin-top:7px;line-height:1.5}
  .certid .seal{margin-left:auto;flex-shrink:0;width:60px;height:60px;border:1px solid var(--brand);border-radius:50%;display:flex;align-items:center;justify-content:center;color:var(--brand);opacity:.85}
  .foot{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);display:flex;align-items:center;gap:10px;font-size:11px;color:var(--faint)}
  .foot .mono{color:var(--muted)}
  .actions{max-width:760px;margin:18px auto 60px;padding:0 20px;text-align:center}
  .btn{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--line2);background:#fff;color:var(--ink);font-size:13px;font-weight:500;padding:10px 18px;border-radius:8px;cursor:pointer;transition:background .15s,transform .05s}
  .btn:hover{background:var(--wash)}
  .btn:active{transform:translateY(1px)}
  @media print{
    @page{margin:14mm}
    html,body{background:#fff}
    .wrap{margin:0 auto;max-width:none;padding:0}
    .sheet{border:none;border-radius:0;box-shadow:none;padding:0}
    .actions{display:none}
    .certid,.metric,.proof-q{-webkit-print-color-adjust:exact;print-color-adjust:exact}
    .head h1{font-size:46px}
  }
</style>
</head>
<body>
<div class="wrap"><div class="sheet">
  <div class="topbar">""" + _WAVE_MARK + """<span class="serif wordmark">Lethe</span><span class="tag">Verifiable Memory Erasure</span></div>

  <div class="head">
    <div class="eyebrow">Right to be forgotten &middot; Data erasure record</div>
    <h1 class="serif">Certificate of Erasure</h1>
    <div class="subject">Issued for the permanent removal of <span class="mono">""" + esc(sys_name) + """</span> from Lethe&rsquo;s memory.</div>
  </div>

  <div class="rule"></div>

  <section class="block">
    <div class="kicker">Record</div>
    <table class="record"><tbody>""" + rows_html + """</tbody></table>
  </section>

  <section class="block">
    <div class="kicker">Measured deletion &middot; permanently removed</div>
    <div class="metrics">""" + metrics + """</div>
    <p class="measured-note">The figures above are the real before/after difference measured against Lethe&rsquo;s knowledge graph at the time of erasure. The subject&rsquo;s documents, graph nodes and relationships were removed from both the graph and the vector store.</p>
  </section>

  """ + verify + """

  <div class="rule soft"></div>

  <p class="attest">""" + attest + """</p>

  <div class="certid">
    <div>
      <div class="label">Certificate ID &middot; content-hash &middot; tamper-evident</div>
      <div class="hash">""" + esc(cert_id) + """</div>
      <div class="frame">A SHA-256 hash over this certificate&rsquo;s fields. Anyone can re-derive it from the values above to detect tampering. This is a content hash, not a cryptographic signature.</div>
    </div>
    <div class="seal">""" + _WAVE_MARK + """</div>
  </div>

  <div class="foot">""" + _WAVE_MARK + """<span>Generated by <span class="mono">Lethe</span> &mdash; verifiable AI-memory forgetting. This document is built solely from Lethe&rsquo;s stored erasure record.</span></div>
</div></div>

<div class="actions">
  <button class="btn" onclick="window.print()">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect width="12" height="8" x="6" y="14"/></svg>
    Print / Save as PDF
  </button>
</div>
</body>
</html>"""


@app.get("/certificate/{workspace}/{event_id}")
async def certificate(workspace: str, event_id: str, format: str = "html"):
    """Standalone, print-optimized Certificate of Erasure for one stored 'forgotten' timeline event.
    Read-only and deterministic: built PURELY from the persisted event/receipt (no cognee or LLM calls).
    `?format=json` returns the structured cert (incl. the content-hash certificate id). HONEST: omits any
    field the stored event doesn't carry; the certificate id is a content hash, not a signature."""
    ws = _get_ws(workspace)
    if ws is None:
        return JSONResponse({"detail": "That workspace does not exist."}, status_code=404)
    ev = next((e for e in S.get("events", {}).get(ws["id"], [])
               if e.get("id") == event_id and e.get("op") == "forgotten"), None)
    if ev is None:
        return JSONResponse({"detail": "No erasure record found for that id in this workspace."}, status_code=404)
    cert, _cid = _build_certificate(ws, ev)
    if (format or "").lower() == "json":
        return JSONResponse(cert)
    return HTMLResponse(_cert_html(cert))


def _node_label(props):
    if not isinstance(props, dict):
        return "node"
    for k in ("name", "text", "description", "id"):
        v = props.get(k)
        if v:
            return str(v).replace("\\n", " ").strip()[:48]
    return "node"


@app.get("/graph")
async def graph(workspace: str = "incidents"):
    """Return the live knowledge graph (nodes + edges) for the in-app graph view."""
    if not S["ready"]:
        return {"nodes": [], "edges": [], "error": "not ready"}
    ws = _get_ws(workspace)
    if ws is None or (ws["id"] != "incidents" and not _ws_led(ws["id"])):
        return {"nodes": [], "edges": [], "count": {"nodes": 0, "edges": 0}}
    try:
        from cognee.context_global_variables import set_database_global_context_variables
        from cognee.infrastructure.databases.graph import get_graph_engine
        async with set_database_global_context_variables(ws["dataset"], S["user"].id):
            ge = await get_graph_engine()
            nodes, edges = await ge.get_graph_data()
        N, ids = [], set()
        for nd in nodes:
            nid = nd[0] if isinstance(nd, (list, tuple)) else nd
            props = nd[1] if isinstance(nd, (list, tuple)) and len(nd) > 1 and isinstance(nd[1], dict) else (nd if isinstance(nd, dict) else {})
            sid = str(nid)
            ids.add(sid)
            N.append({"id": sid, "label": _node_label(props), "type": str(props.get("type", "")) if isinstance(props, dict) else ""})
        E = []
        for ed in edges:
            if not (isinstance(ed, (list, tuple)) and len(ed) >= 2):
                continue
            s, t = str(ed[0]), str(ed[1])
            lbl = ""
            if len(ed) >= 3:
                lbl = ed[2] if isinstance(ed[2], str) else (ed[2].get("relationship_name", "") if isinstance(ed[2], dict) else "")
            if len(ed) >= 4 and isinstance(ed[3], dict):
                lbl = ed[3].get("relationship_name", lbl) or lbl
            if s in ids and t in ids:
                E.append({"source": s, "target": t, "label": str(lbl).replace("_", " ")[:48]})
        return {"nodes": N, "edges": E, "count": {"nodes": len(N), "edges": len(E)}}
    except Exception as e:
        return {"nodes": [], "edges": [], "error": str(e)[:200]}


@app.get("/ingest-status")
async def ingest_status():
    return S["ingest"]


@app.get("/shot/{name}")
async def shot(name: str):
    """Serve a product screenshot (docs/screenshots, whitelisted) for the landing's inline product cards.
    Public (in the auth allowlist) so the marketing landing renders for anyone; path is basename-sanitised."""
    from fastapi.responses import FileResponse
    base = os.path.basename(name).rsplit(".", 1)[0]
    allowed = {"landing", "triage", "graph", "curation", "timeline", "forget-receipt", "evidence", "lifecycle", "banner"}
    if base not in allowed:
        return JSONResponse({"detail": "not found"}, status_code=404)
    path = os.path.join(os.path.dirname(__file__), "docs", "screenshots", base + ".png")
    if not os.path.exists(path):
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


def _tok(s):
    return set(re.findall(r"[a-z]{4,}", s.lower()))


def _citations(ws_id, answer):
    """Cite a source system only when the ANSWER actually references it — primarily by NAME (the precise
    signal: triage answers name the systems they recommend), with a STRICT distinctive-word fallback for
    the rare unnamed case. Pure deterministic, no extra LLM/retrieval call. Ungrounded / 'not documented'
    answers name nothing and share no rare words → cite nothing. Tuned to UNDER-cite rather than over-cite
    (a wrong source erodes trust more than a missing one). Earlier logic over-cited on the 17-system corpus
    because 'distinctive' was 'in <= half the docs' — far too lax; now it's name-match first + a rare-word gate."""
    texts = S.get("texts", {}).get(ws_id, {})
    if not texts:
        return []
    al = " " + answer.lower() + " "
    cited = []
    for sys in texts:
        name = sys.lower()
        # NAME-match, boundary-safe (hyphen or space form). The lookbehind/lookahead exclude word-chars AND
        # hyphens, so short names never match inside a longer one ("cdn" won't match "cdn-transform").
        variants = {name, name.replace("-", " ")}
        if any(re.search(r"(?<![\w-])" + re.escape(v) + r"(?![\w-])", al) for v in variants):
            cited.append(sys)
    return cited


def _friendly_llm_error(e):
    """A dead/absent LLM key is the #1 fresh-clone failure. Turn the provider's auth exception into a
    plain, actionable message instead of a stack trace. Embeddings are local, so only the chat needs a key."""
    s = str(e).lower()
    if any(k in s for k in ("api key", "api_key", "authentication", "unauthorized", " 401", "invalid_api_key",
                            "no api key", "incorrect api key", "permission")):
        return "🔑 No working LLM key. Add one in Settings (the gear, bottom-left) — embeddings run locally, so only the chat needs a key."
    return None


@app.post("/ask")
async def ask(r: AskReq):
    if not S["ready"]:
        return {"answer": f"⏳ {S['status']} — give it a moment, then ask again."}
    q = (r.query or "").strip()
    if not q:
        return {"answer": "Ask a question about your incidents and I'll help — e.g. what to check if a service is slow."}
    ws = _get_ws(r.workspace)
    if ws is None:
        return {"answer": "That workspace no longer exists — pick one from the switcher."}
    if ws["id"] != "incidents" and not _ws_led(ws["id"]):
        return {"answer": "This workspace is empty — add runbooks or notes in Upload, then ask."}
    try:
        # time-bounded: a wedged provider must not hang the MCP path indefinitely (MCP client caps at 120s)
        answer = await asyncio.wait_for(ib.ask(q, S["user"], r.history, dataset=ws["dataset"],
                                               feedback_influence=LIVE_FEEDBACK_INFLUENCE), timeout=60)
        cites = [] if ib._smalltalk(q) else _citations(ws["id"], answer)
        return {"answer": answer, "citations": cites}
    except asyncio.TimeoutError:
        logger.warning("ask timed out after 60s")
        return {"answer": "⏳ That took too long to answer — the model didn't respond in time. Please try again."}
    except Exception as e:
        logger.exception("ask error: %r", e)
        return {"answer": _friendly_llm_error(e) or "⚠️ Something went wrong answering that — please try again."}


def _only_context_text(r):
    if isinstance(r, list) and r and isinstance(r[0], dict):
        sr = r[0].get("search_result")
        if isinstance(sr, list) and sr:
            return str(sr[0])
        if sr:
            return str(sr)
    return ""


@app.post("/ask-stream")
async def ask_stream(r: AskReq):
    """Streaming sibling of /ask for the web UI — same grounded answer, but token-by-token so the chat
    feels alive. NOTE the two paths are NOT equivalent: /ask runs cognee's GRAPH_COMPLETION (cognee both
    retrieves AND synthesizes the answer), whereas /ask-stream fetches ONLY the retrieved context
    (only_context=True) and then runs its OWN litellm completion with the SAME TRIAGE_PROMPT to stream
    tokens. Same retrieval + same prompt, different generator. /ask stays the non-stream path for MCP + as
    the proven fallback. Verified to preserve the hero answer + the forget flip."""
    import json as _json
    q = (r.query or "").strip()
    ws = _get_ws(r.workspace)
    guard = None
    if not S["ready"]:
        guard = f"⏳ {S['status']} — give it a moment, then ask again."
    elif not q:
        guard = "Ask a question about your incidents and I'll help — e.g. what to check if a service is slow."
    elif ws is None:
        guard = "That workspace no longer exists — pick one from the switcher."
    elif ws["id"] != "incidents" and not _ws_led(ws["id"]):
        guard = "This workspace is empty — add runbooks or notes in Upload, then ask."

    async def gen():
        if guard is not None:
            yield _json.dumps({"t": guard}) + "\n"; yield _json.dumps({"done": True, "citations": []}) + "\n"; return
        st = ib._smalltalk(q)
        if st is not None:
            yield _json.dumps({"t": st}) + "\n"; yield _json.dumps({"done": True, "citations": []}) + "\n"; return
        qq = q
        if r.history:
            # Mirror incident_brain.ask: fold ONLY prior USER turns into the query, never assistant answers
            # (a forgotten system could otherwise be quoted back from an earlier answer, poisoning retrieval).
            turns = [t for t in r.history
                     if isinstance(t, dict) and t.get("content") and t.get("role") == "user"][-6:]
            if turns:
                convo = "\n".join(("Engineer: " + str(t.get("content", ""))[:400]) for t in turns)
                qq = f"Earlier in this conversation the engineer asked:\n{convo}\n\nThe engineer now asks: {q}"
        try:
            cr = await cognee.search(query_text=qq, query_type=ib.SearchType.GRAPH_COMPLETION, only_context=True,
                                     system_prompt=ib.TRIAGE_PROMPT, datasets=[ws["dataset"]],
                                     feedback_influence=LIVE_FEEDBACK_INFLUENCE)
            context = _only_context_text(cr)
            model = os.environ.get("LLM_MODEL"); key = os.environ.get("LLM_API_KEY"); base = os.environ.get("LLM_ENDPOINT") or None
            msgs = [{"role": "system", "content": ib.TRIAGE_PROMPT},
                    {"role": "user", "content": f"Context (the on-call knowledge base):\n{context}\n\nQuestion: {q}\n\nAnswer concisely using only the context above; if it isn't covered, say so plainly."}]
            full = []
            resp = await litellm.acompletion(model=model, api_key=key, api_base=base, temperature=0, stream=True, timeout=60, messages=msgs)
            try:
                async for ch in resp:
                    try:
                        tok = ch.choices[0].delta.content or ""
                    except Exception:
                        tok = ""
                    if tok:
                        full.append(tok)
                        yield _json.dumps({"t": tok}) + "\n"
            finally:
                # a client disconnect (GeneratorExit) must not leak the upstream provider stream
                try:
                    await resp.aclose()
                except Exception:
                    pass
            answer = "".join(full).strip()
            cites = _citations(ws["id"], answer) if answer else []
            yield _json.dumps({"done": True, "citations": cites}) + "\n"
        except Exception as e:
            logger.exception("ask-stream error: %r", e)
            key_msg = _friendly_llm_error(e)
            if key_msg:
                yield _json.dumps({"t": key_msg}) + "\n"; yield _json.dumps({"done": True, "citations": []}) + "\n"
            else:
                yield _json.dumps({"error": "Something went wrong answering that."}) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


async def _graph_counts(ws):
    """(nodes, edges) for a workspace's dataset — best-effort, (None, None) on failure.
    Used to measure the REAL before/after diff a forget makes to the knowledge graph."""
    try:
        from cognee.context_global_variables import set_database_global_context_variables
        from cognee.infrastructure.databases.graph import get_graph_engine
        async with set_database_global_context_variables(ws["dataset"], S["user"].id):
            ge = await get_graph_engine()
            nodes, edges = await ge.get_graph_data()
        return (len(nodes or []), len(edges or []))
    except Exception:
        return (None, None)


async def _retrieval_purged(system, dataset, own_texts):
    """C03: retrieval-layer proof. After a forget, count how many of the system's OWN chunks survive a
    CHUNKS search (should be 0 — gone from the vector index, not just rephrased in the answer). Matched on
    the system's own doc text (distinctive prefix) so a surviving doc that merely MENTIONS the system does
    NOT false-positive. Deterministic, no LLM. Returns int, or None when there is no baseline / on failure."""
    markers = [t.strip()[:60].lower() for t in (own_texts or []) if t and t.strip()]
    if not markers:
        return None
    try:
        res = await cognee.search(query_text=str(system), query_type=ib.SearchType.CHUNKS,
                                  datasets=[dataset], user=S["user"], top_k=20)
    except Exception:
        return None
    texts = []
    for w in (res or []):
        if isinstance(w, dict) and "search_result" in w:
            texts += [(c.get("text") or "") for c in (w.get("search_result") or []) if isinstance(c, dict)]
    return sum(1 for t in texts if any(m in t.lower() for m in markers))


@app.post("/forget")
async def forget(r: ForgetReq):
    if not S["ready"]:
        return {"message": f"⏳ {S['status']} — try again in a moment."}
    ws = _get_ws(r.workspace)
    if ws is None:
        return {"message": "That workspace no longer exists."}
    led = _ws_led(ws["id"])
    try:
        nb, eb = await _graph_counts(ws)                       # graph BEFORE the forget
        async with _WRITE_LOCK:                                 # serialize cognee mutations (no concurrent forget/add)
            n = await ib.forget_system(r.system, led, dataset=ws["dataset"])
        if n == 0:
            return {"message": f"No documents tagged '{r.system}'."}
        led.pop(r.system, None)
        _save_ws_led(ws["id"])
        S.setdefault("tombstones", {}).setdefault(ws["id"], set()).add(r.system)   # remember it's gone (for curation)
        own_texts = list(S.get("texts", {}).get(ws["id"], {}).get(r.system, []) or [])  # capture BEFORE removal (C03 proof)
        S.get("texts", {}).get(ws["id"], {}).pop(r.system, None)                    # its own docs are gone too
        S.get("reviewed", {}).get(ws["id"], {}).pop(r.system, None)
        _save_ws_curation(ws["id"])                                                 # persist so it survives restart
        na, ea = await _graph_counts(ws)                       # graph AFTER the forget
        # C03: retrieval-layer proof — the forgotten docs' OWN chunks must be gone from the vector index
        # (deterministic, no LLM; bounded + fail-soft so it can NEVER break the forget itself).
        try:
            chunks_remaining = await asyncio.wait_for(_retrieval_purged(r.system, ws["dataset"], own_texts), timeout=15)
        except Exception:
            chunks_remaining = None
        # Proof: re-query the now-forgotten system. The triage prompt's "not documented" clause means a
        # truly-deleted entity comes back absent — that returned string IS the verifiable evidence.
        proof_q = f"What is {r.system}, and what should I check for it?"
        try:
            # bounded: the delete already happened (counts above are real). If the proof re-query stalls,
            # don't hang the request — say so plainly; the receipt's node/edge diff is the hard evidence.
            proof_a = await asyncio.wait_for(ib.ask(proof_q, S["user"], [], dataset=ws["dataset"]), timeout=25)
        except asyncio.TimeoutError:
            proof_a = "Verification timed out — the deletion still succeeded (see the node/edge counts above)."
        except Exception:
            proof_a = ""
        diff = lambda b, a: (b - a) if (isinstance(b, int) and isinstance(a, int)) else None
        receipt = {
            "system": r.system, "docs": n,
            "nodes_removed": diff(nb, na), "edges_removed": diff(eb, ea),
            "nodes_after": na, "edges_after": ea,
            "retrieval_chunks_remaining": chunks_remaining,
            "proof_query": proof_q, "proof_answer": proof_a,
        }
        # Persist the forget to the timeline — the real receipt becomes a durable, timestamped audit entry.
        # We store the FULL receipt fields (incl. model + after-counts + proof query) so the standalone
        # Certificate of Erasure can be rebuilt purely from the stored event (no re-query, no LLM call).
        _llm = _llm_info() or {}
        ev = _append_event(ws["id"], "forgotten", r.system, {
            "docs": n, "nodes_removed": receipt["nodes_removed"],
            "edges_removed": receipt["edges_removed"],
            "nodes_after": na, "edges_after": ea,
            "retrieval_chunks_remaining": chunks_remaining,
            "proof_query": proof_q, "proof_answer": proof_a,
            "model": _llm.get("model") or "", "model_label": _llm.get("label") or "",
            "provider": _llm.get("provider") or "",
            "workspace_name": ws.get("name") or "", "dataset": ws.get("dataset") or "",
        })
        receipt["event_id"] = ev.get("id")
        return {"message": f"Decommissioned '{r.system}' — forgot {n} document(s) from the graph + vectors.",
                "receipt": receipt, "event_id": ev.get("id"), "workspace": ws["id"]}
    except Exception as e:
        logger.exception("forget error: %r", e)
        return {"message": "⚠️ Could not decommission that system — please try again."}


async def _ingest_text(text, system, wid, dataset):
    """Add a single doc and cognify it into the given workspace's dataset (background task)."""
    try:
        async with _WRITE_LOCK:                                # serialize cognee mutations (no concurrent forget/add)
            r = await cognee.add(text, dataset_name=dataset)
            info = getattr(r, "data_ingestion_info", None) or []
            did = info[0].get("data_id") if info and isinstance(info[0], dict) else None
            await cognee.cognify(datasets=[dataset])
        _ws_led(wid).setdefault(system, []).append(str(did) if did else None)
        _save_ws_led(wid)
        S.setdefault("texts", {}).setdefault(wid, {}).setdefault(system, []).append(text)  # for curation scans
        S.get("tombstones", {}).get(wid, set()).discard(system)  # re-added → no longer decommissioned
        S.setdefault("reviewed", {}).setdefault(wid, {})[system] = datetime.date.today().isoformat()  # fresh on ingest
        _save_ws_curation(wid)                                                     # persist so it survives restart
        _append_event(wid, "added", system, {})                                    # timeline: remembered a new system
        S["ingest"] = {"state": "done", "system": system, "wid": wid}
    except Exception as e:
        S["ingest"] = {"state": "error", "error": str(e)[:160], "wid": wid}


@app.post("/upload")
async def upload(r: UploadReq):
    if not S["ready"]:
        return {"state": "error", "error": "not ready yet"}
    ws = _get_ws(r.workspace)
    if ws is None:
        return {"state": "error", "error": "unknown workspace"}
    if ws["id"] == "incidents" or ws["dataset"] == DEFAULT_WS["dataset"]:
        # protect the golden demo data: ingesting here would cognify into main_dataset and rewrite the
        # golden ledger.json. Force users into a fresh workspace instead. (No bypass flag for now.)
        return {"error": "Uploading into the default Incidents workspace is disabled to protect the demo data. Create a workspace first."}
    if S["ingest"].get("state") == "ingesting":
        return {"state": "busy", "error": "already ingesting a document — wait for it to finish"}
    text = (r.text or "").strip()
    if not text:
        return {"state": "error", "error": "empty document"}
    system = (r.system or "uploaded").strip() or "uploaded"
    # Defense-in-depth against stored XSS: a system name is rendered into many HTML sinks (Systems cards,
    # data-sys attributes, timeline). Reject names with angle brackets/quotes or absurd length server-side
    # so a malicious name can never reach the client at all (the esc() hardening is the second layer).
    if len(system) > 80 or re.search(r'[<>"\']', system):
        return {"state": "error", "error": "System name can't contain < > \" ' or be longer than 80 characters."}
    S["ingest"] = {"state": "ingesting", "system": system, "wid": ws["id"]}  # set lock synchronously before scheduling
    asyncio.create_task(_ingest_text(text, system, ws["id"], ws["dataset"]))
    return {"state": "ingesting", "system": system}


# --- Re-arm the demo -------------------------------------------------------
# After the destructive forget, the only way to re-run the demo used to be terminal + restart.
# /demo/rearm re-INGESTS the hero system's 2 runbooks into the golden dataset via the SAME proven
# background cognify path the upload flow uses. This is an honest re-ingest, NOT an "undo": the forget
# receipt and Timeline history stay (that is exactly the audit-log point). Gated to golden 'incidents'.
_HERO_SYSTEM = "legacy-cache"


async def _rearm_ingest(dataset):
    system, wid = _HERO_SYSTEM, "incidents"
    texts = [t for tag, t in ib.WIKI if tag == system]
    try:
        dids = []
        async with _WRITE_LOCK:                                 # serialize cognee mutations
            for text in texts:
                r = await cognee.add(text, dataset_name=dataset)
                info = getattr(r, "data_ingestion_info", None) or []
                dids.append(info[0].get("data_id") if info and isinstance(info[0], dict) else None)
            await cognee.cognify(datasets=[dataset])            # one cognify pass for both docs
        _ws_led(wid)[system] = [str(d) if d else None for d in dids]
        _save_ws_led(wid)
        S.setdefault("texts", {}).setdefault(wid, {})[system] = list(texts)   # restore curation source
        S.get("tombstones", {}).get(wid, set()).discard(system)               # re-added → no longer decommissioned
        S.setdefault("reviewed", {}).setdefault(wid, {})[system] = datetime.date.today().isoformat()
        _save_ws_curation(wid)
        _append_event(wid, "added", system, {"note": "re-ingested to re-arm the demo"})
        S["ingest"] = {"state": "done", "system": system, "wid": wid}
    except Exception as e:
        S["ingest"] = {"state": "error", "error": str(e)[:160], "wid": wid}


@app.post("/demo/rearm")
async def demo_rearm():
    """Re-arm the golden demo by re-ingesting the hero system's runbooks (golden 'incidents' only).
    Refuses if the system is still present, or while another ingest is running."""
    if not S["ready"]:
        return {"state": "error", "error": "not ready yet"}
    if _HERO_SYSTEM in _ws_led("incidents"):
        return {"state": "error", "error": "legacy-cache is already in the knowledge base — nothing to re-arm."}
    if S["ingest"].get("state") == "ingesting":
        return {"state": "busy", "error": "already ingesting — wait for it to finish"}
    S["ingest"] = {"state": "ingesting", "system": _HERO_SYSTEM, "wid": "incidents"}   # lock before scheduling
    asyncio.create_task(_rearm_ingest(DEFAULT_WS["dataset"]))
    return {"state": "ingesting", "system": _HERO_SYSTEM}


@app.get("/workspaces")
async def workspaces_list():
    return {"workspaces": [{"id": w["id"], "name": w["name"]} for w in (S["workspaces"] or [DEFAULT_WS])]}


@app.post("/workspaces")
async def workspaces_create(r: WsReq):
    import uuid as _uuid
    name = (r.name or "").strip()[:40] or "Workspace"
    wid = "ws_" + _uuid.uuid4().hex[:8]
    w = {"id": wid, "name": name, "dataset": wid}
    S["workspaces"].append(w)
    _save_workspaces()
    S["ws_ledgers"][wid] = {}
    _save_ws_led(wid)
    return {"workspace": {"id": wid, "name": name}}


@app.post("/workspaces/delete")
async def workspaces_delete(r: WsDelReq):
    """Delete a workspace and (on-thesis) HARD-DELETE its knowledge first. The default 'Incidents'
    workspace (the golden graph) can never be deleted — fail-closed."""
    wid = (r.id or "").strip()
    if not wid or wid == "incidents":
        return {"ok": False, "error": "The default workspace can't be deleted."}
    ws = next((w for w in (S["workspaces"] or []) if w["id"] == wid), None)
    if ws is None:
        return {"ok": False, "error": "That workspace no longer exists."}
    led = S.get("ws_ledgers", {}).get(wid, {})
    async with _WRITE_LOCK:                               # serialize cognee mutations (no concurrent forget/add)
        for system in list(led.keys()):                   # purge its docs — verifiable hard-delete
            try:
                await ib.forget_system(system, led, dataset=ws["dataset"])
            except Exception:
                pass
    S["workspaces"] = [w for w in S["workspaces"] if w["id"] != wid]
    _save_workspaces()
    S.get("ws_ledgers", {}).pop(wid, None)
    S.get("texts", {}).pop(wid, None)
    S.get("tombstones", {}).pop(wid, None)
    S.get("reviewed", {}).pop(wid, None)
    S.get("events", {}).pop(wid, None)
    try:
        os.remove(_ws_ledger_path(wid))
    except Exception:
        pass
    try:
        os.remove(_events_path(wid))
    except Exception:
        pass
    try:
        os.remove(_ws_curation_path(wid))
    except Exception:
        pass
    return {"ok": True}


# The two page shells live in templates/ as plain HTML (extracted 2026-07-04 byte-identically from the
# former in-file string literals — sha-verified). Loaded once at import; serving is unchanged.
_TPL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

def _read_tpl(name):
    with open(os.path.join(_TPL_DIR, name), encoding="utf-8") as f:
        return f.read()

PAGE = _read_tpl("app.html")



LANDING = _read_tpl("landing.html")



@app.get("/evidence")
async def evidence():
    """Headline metrics from the REAL correctness benchmark (research/forget_correctness_benchmark.py):
    an INDEPENDENT blind judge (a different model) scores answer correctness 0-2, before vs after forgetting,
    on a multi-system corpus. Public/read-only. Returns {available:false} UNTIL that benchmark has actually
    been run — so the landing never displays an unproven (or circular) number."""
    path = os.path.join(os.path.dirname(__file__), "research", "forget_correctness_results.json")
    try:
        with open(path) as f:
            r = json.load(f)
        return {
            "available": True,
            "generated_at": r.get("generated_at"),
            "judge_model": r.get("judge_model"),
            "corpus_docs": r.get("corpus_docs"),
            "decommissioned": len(r.get("decommissioned", []) or []),
            "stale_before": r.get("stale_correctness_before"), "stale_after": r.get("stale_correctness_after"),
            "control_before": r.get("control_correctness_before"), "control_after": r.get("control_correctness_after"),
            "abstention_before": r.get("abstention_correctness_before"), "abstention_after": r.get("abstention_correctness_after"),
            "retrieval_proof_pass": r.get("retrieval_proof_pass"),
            "docs_forgotten": r.get("docs_forgotten"),
        }
    except Exception:
        return {"available": False}


# --- /learn — the in-app docs/blog, rendering the learning/ chapters ---------------------------------
# The learning/ folder is the project explained end-to-end (architecture, cognee deep-dive, research
# story). /learn serves it as a readable blog: an index + raw markdown, rendered client-side. Public
# content (auth-open, like the landing); read-only; path-traversal guarded.
_LEARN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "learning")
# Internal working notes — real docs, wrong audience for the public blog (process scaffolding, not the story).
_LEARN_EXCLUDE = {"bug-hunt.md", "hackathon-winners-research.md", "security-review.md", "ui-audit.md"}


def _learn_files():
    out = []
    for root, dirs, files in os.walk(_LEARN_DIR):
        dirs.sort()
        for fn in sorted(files):
            if fn.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, fn), _LEARN_DIR)
                rel = rel.replace(os.sep, "/")
                if rel not in _LEARN_EXCLUDE:
                    out.append(rel)
    return out


@app.get("/learn/index.json")
async def learn_index():
    docs = []
    for rel in _learn_files():
        title = os.path.splitext(os.path.basename(rel))[0].replace("-", " ")
        try:
            with open(os.path.join(_LEARN_DIR, rel), encoding="utf-8") as f:
                for line in f:
                    if line.startswith("#"):
                        title = line.lstrip("#").strip()
                        break
        except Exception:
            pass
        docs.append({"path": rel, "title": title})
    return {"docs": docs}


@app.get("/learn/raw/{doc_path:path}")
async def learn_raw(doc_path: str):
    base = os.path.realpath(_LEARN_DIR)
    full = os.path.realpath(os.path.join(base, doc_path))
    if not (full.startswith(base + os.sep) and full.endswith(".md") and os.path.isfile(full)):
        return JSONResponse({"error": "not found"}, status_code=404)
    if os.path.relpath(full, base).replace(os.sep, "/") in _LEARN_EXCLUDE:
        return JSONResponse({"error": "not found"}, status_code=404)
    with open(full, encoding="utf-8") as f:
        return PlainTextResponse(f.read(), media_type="text/markdown; charset=utf-8")


@app.get("/learn", response_class=HTMLResponse)
async def learn_view():
    return _read_tpl("learn.html")


@app.get("/", response_class=HTMLResponse)
async def index():
    return LANDING


@app.get("/app", response_class=HTMLResponse)
async def app_view():
    return PAGE
