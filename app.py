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
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
    if request.url.path not in _AUTH_OPEN and not request.url.path.startswith("/shot/"):
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
    out = {"ready": S["ready"], "status": S["status"], "auth": bool(_AUTH_TOKEN)}
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


PAGE = """<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lethe</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 26 24' fill='none' stroke='%2322d3ee' stroke-width='2.4' stroke-linecap='round'><path d='M2 12c2.2-4 4.4-4 6.6 0s4.4 4 6.6 0'/><path d='M15.2 12c2.2-4 4.4-4 6.6 0' opacity='.4'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
<style type="text/tailwindcss">
  @custom-variant dark (&:where(.dark, .dark *));
  /* Cool blue-tinted neutral palette: re-tints the zinc + slate ramps with a bolder blue hint (blue > red)
     so the whole app reads as a deep, cool blue-gray — not brown, not flat gray. Reversible. */
  @theme {
    --color-zinc-50:#f5f7fa; --color-zinc-100:#eef1f6; --color-zinc-200:#e0e5ee; --color-zinc-300:#cbd3e1;
    --color-zinc-400:#93a0b5; --color-zinc-500:#64718a; --color-zinc-600:#475066; --color-zinc-700:#353d50;
    --color-zinc-800:#222838; --color-zinc-900:#161b28; --color-zinc-950:#0d1119;
    --color-slate-100:#eef1f6; --color-slate-200:#e0e5ee; --color-slate-300:#cbd3e1; --color-slate-400:#93a0b5;
    --color-slate-500:#64718a; --color-slate-600:#475066; --color-slate-700:#353d50; --color-slate-800:#222838;
    --color-slate-900:#141a27;
  }
  body{font-family:'Geist',system-ui,sans-serif}
  @keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}
  .dot{animation:blink 1.4s infinite both}.dot:nth-child(2){animation-delay:.2s}.dot:nth-child(3){animation-delay:.4s}
  @keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .fadein{animation:fadein .28s ease both}
  @keyframes dissolve{0%{opacity:1;filter:blur(0)}18%{box-shadow:0 0 24px 2px rgba(34,211,238,.30)}100%{opacity:0;transform:translateY(16px) scale(.95);filter:blur(7px)}}
  .dissolving{animation:dissolve .75s ease forwards;pointer-events:none}
  ::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:rgba(120,120,135,.3);border-radius:8px}
  .serif{font-family:'Instrument Serif','Spectral',Georgia,serif;font-weight:400}
  .bg-grid{background-image:radial-gradient(rgba(100,116,139,.12) 1px,transparent 1px);background-size:26px 26px}
  .dark .bg-grid{background-image:radial-gradient(rgba(148,163,184,.06) 1px,transparent 1px)}
  .wv{position:absolute;left:0;top:0;height:100%;width:200%}
  .wv-f{animation:wflow 26s linear infinite}.wv-b{animation:wflow 40s linear infinite}
  @keyframes wflow{to{transform:translateX(-50%)}}
  /* --- dashboard motion primitives (ported from the landing so /app feels as alive as /) --- */
  @keyframes cardIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  @keyframes modalIn{from{opacity:0;transform:scale(.96) translateY(6px)}to{opacity:1;transform:none}}
  @keyframes shimmer{to{background-position:-200% 0}}
  .glow{background-image:radial-gradient(220px circle at var(--mx,-200px) var(--my,-200px),rgba(34,211,238,.13),transparent 60%)}
  .lift{transition:transform .18s ease,box-shadow .25s ease,border-color .25s ease}
  .lift:hover{transform:translateY(-2px);box-shadow:0 18px 44px -24px rgba(2,8,20,.55);border-color:rgba(34,211,238,.35)}
  .card-enter>*{animation:cardIn .45s cubic-bezier(.22,1,.36,1) both;animation-delay:calc(var(--i,0)*45ms)}
  .mscale{animation:modalIn .2s cubic-bezier(.22,1,.36,1)}
  .view-enter{animation:cardIn .3s cubic-bezier(.22,1,.36,1) both}
  .skel{background:linear-gradient(90deg,rgba(148,163,184,.10) 25%,rgba(148,163,184,.20) 37%,rgba(148,163,184,.10) 63%);background-size:200% 100%;animation:shimmer 1.3s linear infinite;border-radius:.6rem}
  .dark .skel{background:linear-gradient(90deg,rgba(148,163,184,.06) 25%,rgba(148,163,184,.13) 37%,rgba(148,163,184,.06) 63%);background-size:200% 100%}
  @keyframes thsh{to{background-position:-200% 0}}
  .thsh{background:linear-gradient(90deg,rgba(113,113,122,.65) 20%,rgba(34,211,238,.95) 50%,rgba(113,113,122,.65) 80%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;animation:thsh 1.5s linear infinite}
  .dark .thsh{background:linear-gradient(90deg,rgba(161,161,170,.5) 20%,rgba(34,211,238,.95) 50%,rgba(161,161,170,.5) 80%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text}
  button:not(:disabled):active{transform:scale(.97)}
  input:focus-visible,textarea:focus-visible{box-shadow:0 0 0 3px rgba(34,211,238,.15)}
  .drop-hot{border-color:#22d3ee!important;background-color:rgba(34,211,238,.07)!important}
  @media (prefers-reduced-motion:reduce){.wv-f,.wv-b{animation:none}.card-enter>*,.mscale,.view-enter{animation:none}.lift:hover{transform:none}.glow::before{display:none}.skel{animation:none}.thsh{animation:none;-webkit-text-fill-color:#71717a;color:#71717a}}
</style>
</head>
<body class="min-h-screen text-slate-900 antialiased dark:text-slate-100">
<div aria-hidden="true" class="bg-grid fixed inset-0 -z-10 bg-[#f6f8fc] dark:bg-[#0a0e1a]"></div>
<div aria-hidden="true" class="pointer-events-none fixed inset-x-0 bottom-0 -z-10 h-44 overflow-hidden" style="-webkit-mask-image:linear-gradient(0deg,#000,#000 28%,transparent);mask-image:linear-gradient(0deg,#000,#000 28%,transparent)">
  <svg class="wv wv-b" viewBox="0 0 1440 90" preserveAspectRatio="none" fill="none"><path d="M0 55 Q120 30 240 55 T480 55 T720 55 T960 55 T1200 55 T1440 55" stroke="#6366f1" stroke-width="1.5" stroke-opacity=".13" stroke-linecap="round"/></svg>
  <svg class="wv wv-f" viewBox="0 0 1440 90" preserveAspectRatio="none" fill="none"><path d="M0 44 Q120 20 240 44 T480 44 T720 44 T960 44 T1200 44 T1440 44" stroke="#22d3ee" stroke-width="1.5" stroke-opacity=".15" stroke-linecap="round"/></svg>
</div>
<div class="flex h-screen overflow-hidden">

  <aside class="flex w-16 md:w-60 shrink-0 flex-col border-r border-zinc-300 px-2 md:px-3 py-4 dark:border-zinc-900">
    <div class="mb-6 flex items-center justify-center md:justify-start gap-2 px-1 md:px-2">
      <svg class="lethe-x" width="32" height="16" viewBox="0 0 40 20" fill="none" aria-label="Lethe"><style>@keyframes lxL{to{transform:translateX(-20px)}}@keyframes lxR{to{transform:translateX(20px)}}.lethe-x:hover .lxa{animation:lxL 2.2s linear infinite}.lethe-x:hover .lxb{animation:lxR 2.2s linear infinite}@media(prefers-reduced-motion:reduce){.lethe-x:hover .lxa,.lethe-x:hover .lxb{animation:none}}</style><defs><clipPath id="lxClipA"><rect width="40" height="20"/></clipPath></defs><g clip-path="url(#lxClipA)"><path class="lxb" d="M-20 10 Q-15 18 -10 10 T0 10 T10 10 T20 10 T30 10 T40 10 T50 10 T60 10" stroke="#6366f1" stroke-width="2.6" stroke-linecap="round"/><path class="lxa" d="M-20 10 Q-15 2 -10 10 T0 10 T10 10 T20 10 T30 10 T40 10 T50 10 T60 10" stroke="#22d3ee" stroke-width="2.6" stroke-linecap="round"/></g></svg>
      <a href="/" class="serif hidden text-lg leading-none tracking-tight transition hover:opacity-70 md:inline">Lethe</a>
    </div>
    <div class="relative mb-4 hidden md:block">
      <button id="wsBtn" class="flex w-full items-center justify-between gap-2 rounded-lg border border-zinc-300 px-2.5 py-2 text-sm transition hover:bg-zinc-100 dark:border-zinc-800 dark:hover:bg-zinc-900">
        <span class="flex min-w-0 items-center gap-2"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></svg><span id="wsName" class="truncate font-medium">Incidents</span></span>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
      </button>
      <div id="wsMenu" class="absolute left-0 right-0 top-11 z-30 hidden rounded-xl border border-zinc-300 bg-white p-1.5 shadow-xl dark:border-zinc-800 dark:bg-[#1a2233]"></div>
    </div>
    <nav class="flex flex-col gap-1" id="nav">
      <button data-view="triage" title="Triage" class="nav flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22z"/></svg><span class="hidden md:inline">Triage</span></button>
      <button data-view="systems" title="Systems" class="nav flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg><span class="hidden md:inline">Systems</span></button>
      <button data-view="upload" title="Upload" class="nav flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg><span class="hidden md:inline">Upload</span></button>
      <button data-view="graph" title="Graph" class="nav flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="m7 7.5 3.5 8.5M17 7.5 13.5 16M7.2 6.4h9.6"/></svg><span class="hidden md:inline">Graph</span></button>
      <button data-view="curation" title="Curation" class="nav flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 3.5 11 7l3.5 1.5L11 10l-1.5 3.5L8 10 4.5 8.5 8 7z"/><path d="m18 13 .7 1.9 1.9.7-1.9.7L18 19l-.7-1.9-1.9-.7 1.9-.7z"/></svg><span class="hidden md:inline">Curation</span></button>
      <button data-view="timeline" title="Timeline" class="nav flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span class="hidden md:inline">Timeline</span></button>
    </nav>
    <div id="chatsPane" class="mt-5 hidden min-h-0 flex-1 flex-col md:flex">
      <div class="mb-1.5 flex items-center justify-between px-2.5">
        <span class="text-[11px] font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">Chats</span>
        <button id="newChatSide" aria-label="New chat" title="New chat" class="rounded p-0.5 text-zinc-400 transition hover:text-zinc-900 dark:hover:text-white"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg></button>
      </div>
      <div id="chatList" class="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto"></div>
    </div>
    <div class="mt-auto flex flex-col gap-2 px-1">
      <div id="llmrow" class="hidden items-center gap-1.5 text-[11px] text-zinc-500 md:flex dark:text-zinc-400" title="Answers are generated by this model. Switch the provider in .env — Ollama = fully offline.">
        <span id="llmdot" class="h-1.5 w-1.5 shrink-0 rounded-full bg-zinc-400"></span><span id="llmbadge" class="truncate">…</span><span id="authLock" title="Token-protected instance" class="hidden shrink-0 text-cyan-500 dark:text-cyan-400"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></span>
      </div>
      <div class="flex items-center gap-2 px-1 text-xs text-zinc-500 dark:text-zinc-400"><span id="dot" class="h-2 w-2 shrink-0 rounded-full bg-amber-500"></span><span id="dotxt" class="hidden md:inline">starting…</span></div>
      <div class="flex items-center justify-center gap-1.5 md:flex-col md:items-stretch md:gap-1">
      <button id="setBtn" aria-label="Settings" title="Model & API key" class="flex h-8 w-8 items-center justify-center gap-2.5 rounded-md border border-zinc-300 px-0 text-zinc-600 transition hover:bg-zinc-100 md:h-auto md:w-full md:justify-start md:border-0 md:px-2.5 md:py-2 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-900"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg><span class="hidden text-sm md:inline">Settings</span></button>
      <button id="theme" aria-label="Toggle theme" title="Toggle theme" class="flex h-8 w-8 items-center justify-center gap-2.5 rounded-md border border-zinc-300 px-0 text-zinc-600 transition hover:bg-zinc-100 md:h-auto md:w-full md:justify-start md:border-0 md:px-2.5 md:py-2 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-900">
        <svg class="block shrink-0 dark:hidden" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        <svg class="hidden shrink-0 dark:block" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/></svg>
        <span class="hidden text-sm md:inline">Theme</span>
      </button>
      </div>
    </div>
  </aside>

  <main class="flex min-h-0 flex-1 flex-col">
    <div class="mx-auto flex h-full w-full max-w-3xl flex-col px-4 md:px-6">

      <section data-view="triage" class="flex min-h-0 flex-1 flex-col py-6">
        <div class="relative mb-3 flex shrink-0 items-center justify-between gap-2">
          <button id="threadBtn" class="flex min-w-0 items-center gap-1.5 rounded-lg px-2 py-1 text-sm font-medium text-zinc-700 transition hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-900">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22z"/></svg>
            <span id="threadTitle" class="truncate">New chat</span>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
          </button>
          <div id="ctxBadge" class="mr-auto hidden min-w-0 items-center gap-1.5 pl-1 text-[11px] text-zinc-400 sm:flex dark:text-zinc-500" title="Active workspace and how many systems it knows about">
            <span class="h-1 w-1 shrink-0 rounded-full bg-zinc-300 dark:bg-zinc-700"></span>
            <span id="ctxWs" class="truncate font-medium text-zinc-500 dark:text-zinc-400">Incidents</span>
            <span id="ctxSep" class="hidden text-zinc-300 dark:text-zinc-700">·</span>
            <span id="ctxSys" class="hidden shrink-0 whitespace-nowrap" style="font-family:'Geist Mono',ui-monospace,monospace"></span>
          </div>
          <button id="newChat" class="flex shrink-0 items-center gap-1 rounded-lg border border-zinc-300 px-2.5 py-1.5 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:border-zinc-800 dark:text-zinc-400 dark:hover:text-white"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>New chat</button>
          <div id="threadMenu" class="absolute left-0 top-10 z-30 hidden max-h-80 w-80 overflow-y-auto rounded-xl border border-zinc-300 bg-white p-1.5 shadow-xl dark:border-zinc-800 dark:bg-[#1a2233]"></div>
        </div>
        <div id="demoCard" class="hidden shrink-0 mb-3 rounded-xl border border-cyan-300/60 bg-cyan-50/40 p-4 dark:border-cyan-800/40 dark:bg-cyan-950/15">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="mono text-[10px] uppercase tracking-[0.18em] text-cyan-600 dark:text-cyan-400">start here</div>
              <h3 class="serif mt-0.5 text-xl tracking-tight">The 60-second demo</h3>
            </div>
            <button id="demoCardX" aria-label="Dismiss" class="-mr-1 -mt-1 shrink-0 rounded p-1 text-zinc-400 transition hover:text-zinc-600 dark:hover:text-zinc-200"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button>
          </div>
          <ol class="mt-3 space-y-2 text-sm text-zinc-600 dark:text-zinc-300">
            <li class="flex items-start gap-2.5"><span class="mono mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-cyan-500/15 text-[11px] font-medium text-cyan-700 dark:text-cyan-300">1</span><span><button id="demoStep1" class="text-left font-medium text-cyan-700 underline decoration-dotted underline-offset-2 transition hover:text-cyan-800 dark:text-cyan-400 dark:hover:text-cyan-300">Ask: &ldquo;If auth-service latency is high, what should I check?&rdquo;</button></span></li>
            <li class="flex items-start gap-2.5"><span class="mono mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-cyan-500/15 text-[11px] font-medium text-cyan-700 dark:text-cyan-300">2</span><span>Then <button id="demoStep2" class="font-medium text-cyan-700 underline decoration-dotted underline-offset-2 transition hover:text-cyan-800 dark:text-cyan-400 dark:hover:text-cyan-300">decommission <span class="mono">legacy-cache</span> in Systems</button> &mdash; watch the removal receipt.</span></li>
            <li class="flex items-start gap-2.5"><span class="mono mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-cyan-500/15 text-[11px] font-medium text-cyan-700 dark:text-cyan-300">3</span><span>Ask the <b class="font-semibold text-zinc-700 dark:text-zinc-200">exact same question</b> again &mdash; watch the answer flip to the live system.</span></li>
            <li class="flex items-start gap-2.5"><span class="mono mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-cyan-500/15 text-[11px] font-medium text-cyan-700 dark:text-cyan-300">4</span><span><button id="demoStep4" class="text-left font-medium text-cyan-700 underline decoration-dotted underline-offset-2 transition hover:text-cyan-800 dark:text-cyan-400 dark:hover:text-cyan-300">Run a curation cycle in Curation</button> &mdash; watch aging knowledge sink (reversibly), and the very stale ones queue for your approval.</span></li>
          </ol>
        </div>
        <div id="log" role="log" aria-live="polite" aria-relevant="additions text" aria-label="Conversation" class="flex min-h-0 flex-1 flex-col space-y-3 overflow-y-auto rounded-xl border border-zinc-300 bg-white p-4 dark:border-zinc-800 dark:bg-[#141b2b]"></div>
        <div class="mt-3 flex shrink-0 items-end gap-2 rounded-2xl border border-zinc-300 bg-white p-1.5 pl-3.5 shadow-sm transition focus-within:border-cyan-500 focus-within:shadow-md focus-within:shadow-cyan-500/10 dark:border-zinc-800 dark:bg-[#141b2b]">
          <textarea id="q" rows="1" placeholder="Ask anything about your incidents…" class="max-h-40 flex-1 resize-none bg-transparent py-2 text-sm outline-none placeholder:text-zinc-400 dark:placeholder:text-zinc-500"></textarea>
          <button id="bAsk" disabled aria-label="Ask" class="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-zinc-950 text-white transition hover:bg-zinc-800 disabled:opacity-40 dark:bg-white dark:text-black dark:hover:bg-zinc-200"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
        </div>
      </section>

      <section data-view="systems" class="hidden min-h-0 flex-1 overflow-y-auto py-8">
        <div class="flex flex-col items-start gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div><h2 class="serif text-2xl tracking-tight">Systems</h2>
          <p class="mt-1 max-w-2xl text-sm text-zinc-500 dark:text-zinc-400">Everything in the knowledge base. Decommission one and it's hard-deleted from the graph + vectors.</p></div>
          <button id="addSysBtn" class="shrink-0 rounded-lg border border-zinc-300 px-3 py-1.5 text-sm font-medium text-zinc-700 transition hover:border-zinc-400 hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"><span class="mr-1 text-cyan-500">+</span>Add system</button>
        </div>
        <div id="sysgrid" class="mt-5 grid grid-cols-1 gap-2.5 lg:grid-cols-2"></div>
      </section>

      <section data-view="upload" class="hidden min-h-0 flex-1 overflow-y-auto py-8">
        <div><h2 class="serif text-2xl tracking-tight">Upload</h2>
        <p class="mt-1 max-w-2xl text-sm text-zinc-500 dark:text-zinc-400">Feed your own runbooks, post-mortems or notes. They're parsed into the knowledge graph.</p>
        <p class="mt-2 max-w-2xl text-xs text-zinc-400 dark:text-zinc-500">Ingestion runs a real <span class="mono">cognify</span> pass — about a minute, and it uses your LLM key. The default <span class="font-medium">Incidents</span> workspace is read-only to protect the demo data; create a workspace to ingest into.</p></div>
        <label id="drop" class="mt-5 block cursor-pointer rounded-xl border border-dashed border-zinc-300 bg-white p-10 text-center transition hover:border-zinc-400 dark:border-zinc-700 dark:bg-[#141b2b] dark:hover:border-zinc-600">
          <input id="file" type="file" accept=".txt,.md,.markdown,.json,.csv,.log" multiple class="hidden">
          <svg class="mx-auto mb-2 text-zinc-400" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m17 8-5-5-5 5"/><path d="M12 3v12"/></svg>
          <div class="text-sm font-medium">Drop a file or click to browse</div>
          <div class="mt-1 text-xs text-zinc-400">.txt, .md, .json, .csv, .log</div>
        </label>
        <div class="my-3 text-center text-xs text-zinc-400">or paste text</div>
        <textarea id="paste" rows="4" placeholder="Paste a runbook or note…" class="inp w-full resize-y"></textarea>
        <div class="mt-2 flex gap-2">
          <input id="upsys" placeholder="System name (e.g. redis-cache)" class="inp flex-1">
          <button id="bUp" disabled class="btn-primary">Ingest</button>
        </div>
        <div id="upmsg" class="mt-3 text-sm"></div>
      </section>

      <section data-view="graph" class="hidden flex min-h-0 flex-1 flex-col overflow-y-auto py-8">
        <div class="flex items-end justify-between">
          <div><h2 class="serif text-2xl tracking-tight">Knowledge graph</h2>
          <p class="mt-1 max-w-2xl text-sm text-zinc-500 dark:text-zinc-400">The live graph Cognee built from your docs. Click a node to see what it connects to; decommission a system, then refresh — watch its node vanish.</p></div>
          <div class="flex items-center gap-2">
            <input id="gsearch" placeholder="find a node…" aria-label="Search the graph" class="mono w-36 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm outline-none placeholder:text-zinc-400 focus:border-cyan-500 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500 sm:w-52">
            <button id="greload" class="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm transition hover:bg-zinc-100 dark:border-zinc-800 dark:hover:bg-zinc-900">Refresh</button>
          </div>
        </div>
        <div class="relative mt-5 min-h-[300px] flex-1">
          <div id="graphbox" class="absolute inset-0 rounded-xl border border-zinc-300 bg-white dark:border-zinc-800 dark:bg-[#141b2b]"></div>
          <div id="gskel" class="hidden absolute inset-0 rounded-xl skel"></div>
          <div id="gpanel" data-glow class="glow hidden absolute right-3 top-3 z-10 w-72 max-h-[calc(100%-1.5rem)] overflow-y-auto rounded-xl border border-zinc-300 bg-white/95 p-4 shadow-xl backdrop-blur dark:border-zinc-700 dark:bg-[#0e1422]/95"></div>
        </div>
        <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-zinc-400">
          <span class="flex items-center gap-1.5"><span class="h-2.5 w-2.5 rounded-full bg-cyan-400"></span>system / concept</span>
          <span class="flex items-center gap-1.5"><span class="h-2.5 w-2.5 rounded-full bg-indigo-400"></span>type</span>
          <span id="glegDem" class="hidden flex items-center gap-1.5"><span class="h-2.5 w-2.5 rounded-full bg-amber-400"></span>demoted</span>
          <span id="gmeta"></span>
          <button id="gask" class="hidden rounded-md border border-cyan-500/40 px-2 py-0.5 font-medium text-cyan-600 transition hover:bg-cyan-500/10 dark:text-cyan-400"></button>
        </div>
      </section>

      <section data-view="curation" class="hidden min-h-0 flex-1 overflow-y-auto py-8">
        <div><h2 class="serif text-2xl tracking-tight">Curation</h2>
        <p class="mt-1 max-w-2xl text-sm text-zinc-500 dark:text-zinc-400">Proactive memory hygiene — catch knowledge that has gone stale or contradicts itself, before it misleads you.</p></div>
        <div id="memHealth" class="mt-5"></div>
        <div data-glow class="glow mt-5 rounded-xl border border-cyan-300/70 bg-cyan-50/40 p-4 dark:border-cyan-800/50 dark:bg-cyan-950/15">
          <div class="flex items-start justify-between gap-3">
            <div><div class="flex items-center gap-2"><span class="mono text-[10px] uppercase tracking-wide text-cyan-600 dark:text-cyan-400">The decay loop</span></div>
              <h3 class="mt-0.5 text-sm font-semibold">Run a curation cycle</h3>
              <p class="mt-0.5 max-w-2xl text-xs text-zinc-500 dark:text-zinc-400">One bounded pass over every runbook by review-age. Aging knowledge is <span class="font-medium text-amber-600 dark:text-amber-400">auto-demoted</span> (reversible — it sinks in answers but stays restorable); the very stale are also <span class="font-medium text-red-600 dark:text-red-400">queued for your approval</span> before any permanent delete. <span class="font-medium text-emerald-600 dark:text-emerald-400">Preview is free · 0 tokens</span> · nothing changes until you apply.</p></div>
            <button id="curCycRun" class="shrink-0 rounded-full bg-cyan-600 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-cyan-500">Run cycle</button>
          </div>
          <div id="curCycBody" class="mt-3"></div>
        </div>
        <div data-glow class="glow mt-3 rounded-xl border border-zinc-300 bg-white/60 p-4 dark:border-zinc-800 dark:bg-[#141b2b]/50">
          <div class="flex items-start justify-between gap-3">
            <div><h3 class="text-sm font-semibold">Stale references</h3><p class="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">Remaining docs that still mention a decommissioned system. <span class="font-medium text-emerald-600 dark:text-emerald-400">Free · 0 tokens</span></p></div>
            <button id="curRun" class="shrink-0 rounded-full bg-zinc-950 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Scan</button>
          </div>
          <div id="curBody" class="mt-3"></div>
        </div>
        <div data-glow class="glow mt-3 rounded-xl border border-zinc-300 bg-white/60 p-4 dark:border-zinc-800 dark:bg-[#141b2b]/50">
          <div class="flex items-start justify-between gap-3">
            <div><h3 class="text-sm font-semibold">Conflicting runbooks</h3><p class="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">Docs that contradict each other on a fact. <span class="font-medium text-amber-600 dark:text-amber-400">Uses your model · capped at 8 checks</span></p></div>
            <button id="curConfRun" class="shrink-0 rounded-full bg-zinc-950 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Scan</button>
          </div>
          <div id="curConfBody" class="mt-3"></div>
        </div>
        <div data-glow class="glow mt-3 rounded-xl border border-zinc-300 bg-white/60 p-4 dark:border-zinc-800 dark:bg-[#141b2b]/50">
          <div class="flex items-start justify-between gap-3">
            <div><h3 class="text-sm font-semibold">Aging knowledge</h3><p class="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">Runbooks not reviewed in over 180 days — likely stale by age. <span class="font-medium text-emerald-600 dark:text-emerald-400">Free · 0 tokens</span></p></div>
            <button id="curAgeRun" class="shrink-0 rounded-full bg-zinc-950 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Scan</button>
          </div>
          <div id="curAgeBody" class="mt-3"></div>
        </div>
        <div data-glow class="glow mt-3 rounded-xl border border-zinc-300 bg-white/60 p-4 dark:border-zinc-800 dark:bg-[#141b2b]/50">
          <div class="flex items-start justify-between gap-3">
            <div><h3 class="text-sm font-semibold">Retirement proposals</h3><p class="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">Long-overdue runbooks that look retired — candidates to forget. <span class="font-medium text-emerald-600 dark:text-emerald-400">Free · 0 tokens</span> · <span class="text-zinc-500 dark:text-zinc-400">Proposals only — nothing is deleted until you confirm.</span></p></div>
            <button id="curPropRun" class="shrink-0 rounded-full bg-zinc-950 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Scan</button>
          </div>
          <div id="curPropBody" class="mt-3"></div>
        </div>
      </section>

      <section data-view="timeline" class="hidden min-h-0 flex-1 overflow-y-auto py-8">
        <div><h2 class="serif text-2xl tracking-tight">Timeline</h2>
        <p class="mt-1 max-w-2xl text-sm text-zinc-500 dark:text-zinc-400">A durable audit log of what this memory has learned and forgotten — and when. Every decommission lands here with its verifiable removal receipt. <span class="font-medium text-emerald-600 dark:text-emerald-400">Free · 0 tokens</span></p></div>
        <div class="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-zinc-500 dark:text-zinc-400">
          <span class="inline-flex items-center gap-1.5"><span class="h-2 w-2 rounded-full bg-zinc-400"></span>reviewed</span>
          <span class="inline-flex items-center gap-1.5"><span class="h-2 w-2 rounded-full bg-amber-400"></span>demoted <span class="text-zinc-400">· reversible</span></span>
          <span class="inline-flex items-center gap-1.5"><span class="h-2 w-2 rounded-full bg-cyan-400"></span>restored / self-healed</span>
          <span class="inline-flex items-center gap-1.5"><span class="h-2 w-2 rounded-full bg-red-500"></span>forgotten <span class="text-zinc-400">· with removal receipt</span></span>
        </div>
        <p class="mt-2 text-xs text-zinc-400 dark:text-zinc-500">Run a curation cycle or decommission a system and the coloured receipts land here.</p>
        <div id="tlBody" class="mt-5"></div>
      </section>

    </div>
  </main>
</div>

<div id="wsModal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
  <div class="mscale w-full max-w-sm rounded-2xl border border-zinc-300 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-[#0e1422]">
    <div class="flex items-center gap-2"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-cyan-500"><path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></svg><h3 class="text-base font-semibold tracking-tight">New workspace</h3></div>
    <p class="mt-1 text-sm text-zinc-500 dark:text-zinc-400">A separate knowledge base with its own graph — isolated from your other workspaces.</p>
    <input id="wsModalInput" maxlength="40" placeholder="e.g. API docs, Onboarding, Vendor configs" class="mt-4 w-full rounded-lg border border-zinc-300 bg-white px-3.5 py-2.5 text-sm outline-none placeholder:text-zinc-400 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/25 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500">
    <div id="wsModalMsg" class="mt-2 min-h-[1rem] text-xs"></div>
    <div class="mt-3 flex justify-end gap-2">
      <button id="wsModalCancel" class="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900">Cancel</button>
      <button id="wsModalOk" class="rounded-full bg-zinc-950 px-4 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Create workspace</button>
    </div>
  </div>
</div>

<div id="wsDelModal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
  <div class="mscale w-full max-w-sm rounded-2xl border border-zinc-300 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-[#0e1422]">
    <div class="flex items-center gap-2.5"><span class="grid h-9 w-9 place-items-center rounded-full border border-red-300 bg-red-50 text-red-600 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-400"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg></span><h3 class="text-base font-semibold tracking-tight">Delete workspace?</h3></div>
    <p class="mt-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">This permanently <b class="font-medium text-zinc-700 dark:text-zinc-200">hard-deletes</b> <span id="wsDelName" class="mono text-zinc-700 dark:text-zinc-200"></span> and all of its knowledge — graph and vectors. This can't be undone.</p>
    <div id="wsDelMsg" class="mt-2 min-h-[1rem] text-xs"></div>
    <div class="mt-4 flex justify-end gap-2">
      <button id="wsDelCancel" class="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900">Cancel</button>
      <button id="wsDelOk" class="rounded-full bg-red-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-red-700">Delete</button>
    </div>
  </div>
</div>

<div id="fgModal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
  <div class="mscale w-full max-w-md rounded-2xl border border-zinc-300 bg-white shadow-2xl dark:border-zinc-800 dark:bg-[#0e1422]">
    <div id="fgBody" class="p-6"></div>
  </div>
</div>

<div id="setModal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
  <div class="mscale w-full max-w-md rounded-2xl border border-zinc-300 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-[#0e1422]">
    <div class="flex items-center gap-2"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-cyan-500"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg><h3 class="text-base font-semibold tracking-tight">Model &amp; API key</h3></div>
    <p class="mt-1 text-sm text-zinc-500 dark:text-zinc-400">Bring your own provider. The key is stored locally on this machine and applied instantly — it only ever talks to the provider you pick.</p>
    <div class="mt-4 space-y-3">
      <div><label class="mb-1 block text-xs font-medium text-zinc-500 dark:text-zinc-400">Provider</label>
        <select id="setProvider" class="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-cyan-500 dark:border-zinc-800 dark:bg-[#141b2b]">
          <option value="openai">OpenAI</option><option value="anthropic">Anthropic (Claude)</option><option value="gemini">Google (Gemini)</option><option value="openrouter">OpenRouter</option><option value="groq">Groq</option><option value="lmstudio">LM Studio (local · no key)</option><option value="ollama">Ollama (local · no key)</option><option value="custom">Custom (OpenAI-compatible)</option>
        </select></div>
      <div><label class="mb-1 block text-xs font-medium text-zinc-500 dark:text-zinc-400">API key</label>
        <input id="setKey" type="password" autocomplete="off" placeholder="sk-…" class="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm outline-none placeholder:text-zinc-400 focus:border-cyan-500 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500"></div>
      <div><label class="mb-1 block text-xs font-medium text-zinc-500 dark:text-zinc-400">Model</label>
        <input id="setModelInput" class="mono w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-cyan-500 dark:border-zinc-800 dark:bg-[#141b2b]"></div>
      <div><label class="mb-1 block text-xs font-medium text-zinc-500 dark:text-zinc-400">Endpoint <span class="text-zinc-400">(optional)</span></label>
        <input id="setEndpoint" class="mono w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm outline-none focus:border-cyan-500 dark:border-zinc-800 dark:bg-[#141b2b]"></div>
    </div>
    <div class="mt-4 border-t border-zinc-200 pt-4 dark:border-zinc-800">
      <div class="flex items-center gap-1.5"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-zinc-400"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg><span class="text-[11px] font-semibold uppercase tracking-wide text-zinc-500 dark:text-zinc-400">Security</span></div>
      <div id="secState" class="mt-2 text-sm text-zinc-600 dark:text-zinc-300">checking…</div>
      <p class="mt-1.5 text-xs text-zinc-400 dark:text-zinc-500">Single-tenant guard — one shared token gates every API route. Set <span class="mono">LETHE_AUTH_TOKEN</span> before any public deploy.</p>
      <div id="secTokenRow" class="mt-2.5 hidden gap-2">
        <input id="secToken" type="password" autocomplete="off" placeholder="paste access token…" class="mono flex-1 rounded-lg border border-zinc-300 bg-white px-3 py-2 text-xs outline-none placeholder:text-zinc-400 focus:border-cyan-500 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500">
        <button id="secSet" class="shrink-0 rounded-lg border border-zinc-300 px-3 py-2 text-xs font-medium text-zinc-700 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900">Save</button>
        <button id="secClear" class="shrink-0 rounded-lg border border-zinc-300 px-3 py-2 text-xs font-medium text-zinc-500 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-400 dark:hover:bg-zinc-900">Clear</button>
      </div>
    </div>
    <div id="setMsg" class="mt-3 min-h-[16px] text-xs"></div>
    <div class="mt-4 flex items-center justify-between">
      <button id="setReset" class="text-xs font-medium text-zinc-500 transition hover:text-zinc-900 dark:hover:text-white">Reset to default</button>
      <div class="flex gap-2"><button id="setCancel" class="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900">Cancel</button>
      <button id="setSave" class="rounded-full bg-zinc-950 px-4 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Save</button></div>
    </div>
  </div>
</div>

<div id="addModal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
  <div class="mscale w-full max-w-md rounded-2xl border border-zinc-300 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-[#0e1422]">
    <div class="flex items-center gap-2"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-cyan-500"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 8v8M8 12h8"/></svg><h3 class="text-base font-semibold tracking-tight">Add a system</h3></div>
    <p class="mt-1 text-sm text-zinc-500 dark:text-zinc-400">Name it and paste its runbook, notes or post-mortem — Lethe builds it into the knowledge graph.</p>
    <input id="addName" maxlength="60" placeholder="System name (e.g. redis-cache)" class="mt-4 w-full rounded-lg border border-zinc-300 bg-white px-3.5 py-2.5 text-sm outline-none placeholder:text-zinc-400 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/25 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500">
    <textarea id="addText" rows="5" placeholder="Paste the runbook or notes…" class="mt-2 w-full resize-y rounded-lg border border-zinc-300 bg-white px-3.5 py-2.5 text-sm outline-none placeholder:text-zinc-400 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/25 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500"></textarea>
    <div id="addMsg" class="mt-2 min-h-[16px] text-xs"></div>
    <div class="mt-4 flex justify-end gap-2">
      <button id="addCancel" class="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900">Cancel</button>
      <button id="addOk" class="rounded-full bg-zinc-950 px-4 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Add system</button>
    </div>
  </div>
</div>

<div id="rearmModal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
  <div class="mscale w-full max-w-md rounded-2xl border border-zinc-300 bg-white p-5 shadow-2xl dark:border-zinc-800 dark:bg-[#0e1422]">
    <div class="flex items-center gap-2"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-cyan-500"><path d="M3 12a9 9 0 1 0 9-9 9 9 0 0 0-6.4 2.6L3 8"/><path d="M3 3v5h5"/></svg><h3 class="text-base font-semibold tracking-tight">Re-arm the demo</h3></div>
    <p class="mt-2 text-sm text-zinc-500 dark:text-zinc-400">This re-ingests the 2 <span class="mono">legacy-cache</span> runbooks into the knowledge base so you can run the forget demo again. It runs a real cognify pass &mdash; about 30 seconds &mdash; and uses your LLM key. The original forget receipt on the Timeline stays &mdash; this is a re-ingest, not an undo.</p>
    <div id="rearmMsg" class="mt-2 min-h-[16px] text-xs"></div>
    <div class="mt-4 flex justify-end gap-2">
      <button id="rearmCancel" class="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900">Cancel</button>
      <button id="rearmGo" class="rounded-full bg-cyan-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-cyan-500">Re-ingest 2 runbooks</button>
    </div>
  </div>
</div>

<div id="srcPeek" class="fixed inset-0 z-50 hidden">
  <div id="srcPeekBg" class="absolute inset-0 bg-black/40 backdrop-blur-sm"></div>
  <div id="srcPeekPanel" class="mscale absolute right-0 top-0 flex h-full w-full max-w-md flex-col overflow-y-auto border-l border-zinc-300 bg-white p-6 shadow-2xl dark:border-zinc-700 dark:bg-[#0e1422]"></div>
</div>

<script>
 const $=id=>document.getElementById(id);
 // Spotlight glow: any element marked [data-glow] gets a cursor-follow radial highlight (matches the landing .card feel).
 let _lastGlow=null;
 document.addEventListener('pointermove',function(e){const c=e.target.closest&&e.target.closest('[data-glow]');
   if(c!==_lastGlow&&_lastGlow){_lastGlow.style.setProperty('--mx','-200px');_lastGlow.style.setProperty('--my','-200px');}
   _lastGlow=c;if(!c)return;
   const r=c.getBoundingClientRect();c.style.setProperty('--mx',(e.clientX-r.left)+'px');c.style.setProperty('--my',(e.clientY-r.top)+'px');},{passive:true});
 // Optional auth: if this instance is token-protected, attach the saved token to API calls and prompt
 // on a 401. No-op locally (no token set server-side → no 401 → identical behavior).
 (function(){const _f=window.fetch.bind(window);
   window.fetch=function(u,o){o=o||{};const rel=(typeof u==='string')&&u.charAt(0)==='/';const t=localStorage.getItem('lethe.token');
     if(rel&&t)o.headers=Object.assign({},o.headers,{Authorization:'Bearer '+t});
     return _f(u,o).then(function(r){
       if(r.status===401&&rel){const nt=prompt('This Lethe instance is protected. Enter the access token:');
         if(nt){localStorage.setItem('lethe.token',nt);o.headers=Object.assign({},o.headers,{Authorization:'Bearer '+nt});return _f(u,o);}}
       return r;});};
 })();
 const root=document.documentElement;
 if(localStorage.theme==='light')root.classList.remove('dark');
 $('theme').onclick=()=>{root.classList.toggle('dark');localStorage.theme=root.classList.contains('dark')?'dark':'light';};

 const inpCls='rounded-lg border border-zinc-300 bg-white px-4 py-2.5 text-sm outline-none placeholder:text-zinc-400 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/25 dark:border-zinc-800 dark:bg-[#141b2b] dark:placeholder:text-zinc-500 dark:focus:border-cyan-400';
 const btnCls='rounded-full bg-zinc-950 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-zinc-800 disabled:opacity-40 dark:bg-white dark:text-black dark:hover:bg-zinc-200';
 const chipCls='cursor-pointer rounded-full border border-zinc-300 bg-zinc-100/50 px-3.5 py-1.5 text-xs text-zinc-600 transition hover:-translate-y-px hover:border-zinc-400 hover:text-zinc-900 dark:border-zinc-800 dark:bg-zinc-900/40 dark:text-zinc-400 dark:hover:text-white';
 document.querySelectorAll('.inp').forEach(e=>e.className=inpCls+(e.classList.contains('flex-1')?' flex-1':'')+(e.classList.contains('w-full')?' w-full resize-y':''));
 document.querySelectorAll('.btn-primary').forEach(e=>e.className=btnCls);
 document.getElementById('log').addEventListener('click',e=>{const c=e.target.closest('[data-q]');if(c)ask(c.dataset.q);});

 let ready=false;
 let threads=[],active=null;
 let activeWs=localStorage.getItem('lethe.ws')||'incidents';
 let wsList=[];
 let curView='triage';
 const navActive='bg-zinc-100 font-medium text-zinc-900 shadow-[inset_2px_0_0_#0891b2] dark:bg-[#1a2233] dark:text-white dark:shadow-[inset_2px_0_0_#22d3ee]';
 const navIdle='text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-900 dark:hover:text-white';
 function show(v){
   curView=v;
   document.querySelectorAll('section[data-view]').forEach(s=>s.classList.toggle('hidden',s.dataset.view!==v));
   const _sec=document.querySelector('section[data-view="'+v+'"]');  // gentle view-enter fade+rise (skip graph — never transform the vis-network canvas)
   if(_sec&&v!=='graph'){_sec.classList.remove('view-enter');void _sec.offsetWidth;_sec.classList.add('view-enter');}
   document.querySelectorAll('.nav').forEach(b=>{b.className='nav flex items-center justify-center md:justify-start gap-2.5 rounded-md px-2 md:px-2.5 py-2 text-sm '+(b.dataset.view===v?navActive:navIdle);});
   if(v==='systems')loadSystems();
   if(v==='graph')loadGraph();
   if(v==='curation')loadCuration();
   if(v==='timeline')loadTimeline();
 }
 document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>show(b.dataset.view));
 show('triage');

 // First-run guided-demo card (Triage): shows once on the default workspace until dismissed.
 function demoCardUpd(){const dc=$('demoCard');if(!dc)return;const shouldShow=localStorage.getItem('lethe.demoCard')!=='done'&&activeWs==='incidents';dc.classList.toggle('hidden',!shouldShow);}
 (function(){const dc=$('demoCard');if(!dc)return;
   $('demoCardX').onclick=()=>{localStorage.setItem('lethe.demoCard','done');dc.classList.add('hidden');};
   $('demoStep1').onclick=()=>{show('triage');ask('If auth-service latency is high, what should I check?');};
   $('demoStep2').onclick=()=>show('systems');
   {const s4=$('demoStep4');if(s4)s4.onclick=()=>show('curation');}
   demoCardUpd();
 })();

 function setDot(text,kind){$('dotxt').textContent=text;$('dot').className='h-2 w-2 rounded-full '+(kind==='ok'?'bg-emerald-500':kind==='err'?'bg-red-500':'bg-amber-500');}
 async function poll(){try{const h=await(await fetch('/health')).json();
   if(h.llm){$('llmbadge').textContent=h.llm.label+' · '+(h.llm.local?'local':'cloud');$('llmdot').className='h-1.5 w-1.5 shrink-0 rounded-full '+(h.llm.local?'bg-emerald-500':'bg-cyan-500');}
   {const al=$('authLock');if(al)al.classList.toggle('hidden',!h.auth);}
   if(h.ready){ready=true;setDot('ready · '+((h.systems&&h.systems.length)||0)+' systems','ok');$('bAsk').disabled=false;$('bUp').disabled=false;if(ctxSysCount==null)loadCtxCount();if(!poll._mem){poll._mem=1;loadMemHealth();}}
   else{setDot(h.status||'starting…','wait');setTimeout(poll,2000);}
 }catch(e){setTimeout(poll,2000);}}
 poll();

 const esc=s=>String(s).replace(/[&<>"'`]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','`':'&#96;'}[c]));
 function clearEmpty(){const e=$('log').querySelector('.js-empty');if(e)e.remove();}
 // markdown-lite, XSS-safe: escape FIRST, then toggle **bold** and `code` via split — input can never inject HTML
 function fmt(s){let h=esc(String(s));
   // esc() leaves ** intact but now escapes backticks to &#96; — split on the entity so `code` still toggles.
   if(h.indexOf('**')>=0){const p=h.split('**');h=p.map((x,i)=>i%2?'<strong>'+x+'</strong>':x).join('');}
   if(h.indexOf('&#96;')>=0){const p=h.split('&#96;');h=p.map((x,i)=>i%2?'<code class="rounded bg-zinc-100 px-1 text-[.85em] dark:bg-zinc-800">'+x+'</code>':x).join('');}
   return h;}
 function bubble(role,html){
   clearEmpty();const w=document.createElement('div');
   if(role==='user'){w.className='flex justify-end fadein';w.innerHTML='<div class="max-w-[82%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-zinc-950 px-4 py-2.5 text-sm text-white dark:bg-white dark:text-black">'+html+'</div>';$('log').appendChild(w);$('log').scrollTop=$('log').scrollHeight;return w.firstChild;}
   w.className='flex justify-start gap-3 fadein';
   const av=document.createElement('div');av.className='mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full bg-cyan-500/10 text-cyan-500';av.innerHTML='<svg width="17" height="9" viewBox="2 2 30 16" fill="none"><path d="M-20 10 Q-15 18 -10 10 T0 10 T10 10 T20 10 T30 10 T40 10" stroke="#6366f1" stroke-width="2.6" stroke-linecap="round"/><path d="M-20 10 Q-15 2 -10 10 T0 10 T10 10 T20 10 T30 10 T40 10" stroke="#22d3ee" stroke-width="2.6" stroke-linecap="round"/></svg>';
   const box=document.createElement('div');box.className='group min-w-0 max-w-[88%]';
   const c=document.createElement('div');c.className='whitespace-pre-wrap text-sm leading-relaxed text-zinc-800 dark:text-zinc-100';c.innerHTML=html;
   box.appendChild(c);w.appendChild(av);w.appendChild(box);$('log').appendChild(w);$('log').scrollTop=$('log').scrollHeight;return c;
 }
 function addCopy(c,text){const box=c.parentElement;const bar=document.createElement('div');
   bar.className='mt-1 flex opacity-60 transition group-hover:opacity-100';
   const b=document.createElement('button');b.className='rounded-md px-1.5 py-0.5 text-xs text-zinc-400 transition hover:text-zinc-700 dark:hover:text-zinc-200';b.textContent='Copy';
   b.onclick=()=>{try{navigator.clipboard.writeText(text);}catch(e){}b.textContent='Copied';setTimeout(()=>{b.textContent='Copy';},1200);};
   bar.appendChild(b);box.appendChild(bar);}
 function addSources(c,sources){const box=c.parentElement;const bar=document.createElement('div');
   bar.className='mt-1.5 flex flex-wrap items-center gap-1.5';
   bar.innerHTML='<span class="mono text-[10px] uppercase tracking-wide text-zinc-400">sources</span>';
   sources.forEach(s=>{const b=document.createElement('button');b.className='mono rounded-full border border-zinc-300 bg-zinc-100/60 px-3 py-1 text-[11px] text-zinc-500 transition hover:-translate-y-px hover:border-cyan-500/50 hover:text-cyan-600 dark:border-zinc-700 dark:bg-zinc-800/40 dark:text-zinc-400 dark:hover:text-cyan-400';b.textContent=s;b.title='View the source for '+s;b.onclick=()=>openSource(s);bar.appendChild(b);});
   box.appendChild(bar);maybeAddConnMap(box,bar,sources);}
 // X1: "Show connections" — an on-demand, STATIC mini-map of an answer's cited entities + their 1-hop
 // neighbours, built from the REAL /graph (Entity/EntityType-filtered, same keep-filter as the Graph view).
 // No hand-curated adjacency: if fewer than 2 nodes resolve, the button never renders. One open at a time.
 var _gcache={},_openConnMap=null;
 function invalidateGraphCache(ws){if(ws)delete _gcache[ws];else _gcache={};}
 async function getGraphCached(ws){if(_gcache[ws])return _gcache[ws];try{var d=await(await fetch('/graph?workspace='+encodeURIComponent(ws))).json();if(d&&d.nodes){_gcache[ws]=d;return d;}}catch(e){}return null;}
 function connNorm(v){return String(v||'').toLowerCase().replace(/[\\s_]+/g,'-');}
 function resolveConn(graph,sources){var keep={Entity:1,EntityType:1};
   var nodes=graph.nodes.filter(function(n){return keep[n.type];}),byId={};nodes.forEach(function(n){byId[n.id]=n;});
   var cites=(sources||[]).map(connNorm).filter(function(x){return x;});
   var hits=[];nodes.forEach(function(n){var nl=connNorm(n.label);for(var i=0;i<cites.length;i++){var cq=cites[i];if(nl===cq||nl.indexOf(cq)>=0||cq.indexOf(nl)>=0){hits.push(n.id);break;}}});
   if(!hits.length)return null;
   var edges=graph.edges.filter(function(e){return byId[e.source]&&byId[e.target];});
   var hitSet={};hits.forEach(function(id){hitSet[id]=1;});var ids=hits.slice();
   edges.forEach(function(e){if(hitSet[e.source]&&byId[e.target].type==='Entity'&&ids.indexOf(e.target)<0)ids.push(e.target);if(hitSet[e.target]&&byId[e.source].type==='Entity'&&ids.indexOf(e.source)<0)ids.push(e.source);});
   ids=ids.slice(0,12);var idset={};ids.forEach(function(id){idset[id]=1;});
   return {nodes:ids.map(function(id){return byId[id];}),edges:edges.filter(function(e){return idset[e.source]&&idset[e.target];})};}
 function buildConnMap(container,resolved){loadVis(function(){
   var p=gpal();
   var nds=new vis.DataSet(resolved.nodes.map(function(n){return {id:n.id,label:n.label,shape:'dot',size:9,color:gbase(n,p),font:{color:p.font,size:11,strokeWidth:4,strokeColor:p.halo}};}));
   var eds=new vis.DataSet(resolved.edges.map(function(e,i){return {id:i,from:e.source,to:e.target,color:{color:p.edge,opacity:1},width:1};}));
   var net=new vis.Network(container,{nodes:nds,edges:eds},{nodes:{borderWidth:2},edges:{smooth:{type:'continuous',roundness:.4},arrows:{to:{enabled:true,scaleFactor:.3}}},physics:{solver:'forceAtlas2Based',stabilization:{iterations:140,fit:true}},interaction:{dragNodes:false,dragView:false,zoomView:false,hover:false,selectable:false,keyboard:false}});
   net.once('stabilizationIterationsDone',function(){net.setOptions({physics:false});net.fit();var cv=container.querySelector('canvas');if(cv){cv.setAttribute('aria-hidden','true');cv.setAttribute('tabindex','-1');}});
 });}
 async function maybeAddConnMap(box,bar,sources){
   if(!sources||!sources.length)return;
   var graph=await getGraphCached(activeWs);if(!graph||!graph.nodes)return;
   var resolved=resolveConn(graph,sources);
   if(!resolved||resolved.nodes.length<2)return;                                  // silent skip — never an empty map
   var btn=document.createElement('button');btn.className='mono text-[11px] text-zinc-400 transition hover:text-cyan-600 dark:hover:text-cyan-400';btn.textContent='Show connections ↳';btn.setAttribute('aria-label','Show a map of the connections in this answer');
   var map=document.createElement('div');map.setAttribute('aria-hidden','true');map.className='hidden mt-2 h-56 w-full overflow-hidden rounded-xl border border-zinc-200 bg-white/60 dark:border-zinc-800 dark:bg-[#0e1422]/60';
   var built=false;
   btn.onclick=function(){
     if(map.classList.contains('hidden')){
       if(_openConnMap&&_openConnMap.el!==map){_openConnMap.el.classList.add('hidden');_openConnMap.btn.textContent='Show connections ↳';}
       map.classList.remove('hidden');btn.textContent='Hide connections ↑';_openConnMap={el:map,btn:btn};
       if(!built){built=true;buildConnMap(map,resolved);}
     }else{map.classList.add('hidden');btn.textContent='Show connections ↳';if(_openConnMap&&_openConnMap.el===map)_openConnMap=null;}
   };
   bar.appendChild(btn);box.appendChild(map);
 }
 // Citation source peek — a right slide-over showing the ORIGINAL wiki doc(s) behind a chip.
 // Server-side ledger-gated: after a forget the /source route 404s, so the peek vanishes with the
 // evidence (it must never contradict Proof-of-Forgetting). askAbout stays, as a secondary action.
 function srcPeekHead(n){return '<div class="flex items-start justify-between gap-2"><div><div class="mono text-[10px] uppercase tracking-[0.18em] text-cyan-600 dark:text-cyan-400">source</div><h3 class="serif mt-0.5 text-xl tracking-tight">'+esc(n)+'</h3></div><button id="srcPeekX" aria-label="Close source" class="-mr-1 -mt-1 shrink-0 rounded p-1 text-zinc-400 transition hover:text-zinc-600 dark:hover:text-zinc-200"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>';}
 async function openSource(name){const p=$('srcPeek'),panel=$('srcPeekPanel');if(!p||!panel)return;
   panel.innerHTML=srcPeekHead(name)+'<div class="mt-4 space-y-3"><div class="skel h-24 w-full"></div></div>';
   p.classList.remove('hidden');
   let body='';
   try{const r=await fetch('/source/'+encodeURIComponent(name)+'?workspace='+encodeURIComponent(activeWs));
     if(r.ok){const j=await r.json();const docs=j.docs||[];
       body='<p class="mono mt-2 text-[10px] uppercase tracking-wide text-zinc-400">'+docs.length+' document'+(docs.length===1?'':'s')+' &middot; from the knowledge base</p>'+
         '<div class="mt-3 space-y-3">'+docs.map(d=>'<div class="rounded-xl border border-zinc-200 bg-zinc-50/60 p-4 dark:border-zinc-800 dark:bg-[#141b2b]"><div class="text-xs font-semibold text-zinc-700 dark:text-zinc-200">'+esc(d.title)+'</div><p class="mt-1.5 text-[13px] leading-relaxed text-zinc-600 dark:text-zinc-300">'+esc(d.text)+'</p></div>').join('')+'</div>';
     }else{body='<p class="mt-4 text-sm text-zinc-500 dark:text-zinc-400">No original source document is available for this citation here.</p>';}
   }catch(e){body='<p class="mt-4 text-sm text-red-500">Could not load the source.</p>';}
   body+='<button id="srcPeekAsk" class="mt-5 w-full shrink-0 rounded-lg border border-cyan-500/40 px-3 py-2 text-sm font-medium text-cyan-600 transition hover:bg-cyan-500/10 dark:text-cyan-400">Ask about this &rarr;</button>';
   panel.innerHTML=srcPeekHead(name)+body;
   $('srcPeekX').onclick=closeSource;
   $('srcPeekAsk').onclick=()=>{closeSource();askAbout(name);};}
 function closeSource(){const p=$('srcPeek');if(p)p.classList.add('hidden');}
 {const _bg=$('srcPeekBg');if(_bg)_bg.onclick=closeSource;}
 // M4: re-arm the demo — an honest re-INGEST of the hero runbooks (never framed as an undo).
 function openRearm(){const m=$('rearmModal');if(!m)return;const msg=$('rearmMsg');if(msg)msg.innerHTML='';const go=$('rearmGo');if(go)go.disabled=false;m.classList.remove('hidden');m.classList.add('flex');}
 function closeRearm(){const m=$('rearmModal');if(!m)return;m.classList.add('hidden');m.classList.remove('flex');}
 async function doRearm(){const go=$('rearmGo'),msg=$('rearmMsg');if(!go||!msg)return;go.disabled=true;
   msg.innerHTML='<div class="text-zinc-500">re-ingesting <span class="mono">legacy-cache</span><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span> (a real cognify pass, ~30s)</div><div class="skel mt-2 h-1.5 w-full"></div>';
   try{const up=await(await fetch('/demo/rearm',{method:'POST'})).json();
     if(up.state==='error'||up.state==='busy'||up.error){go.disabled=false;msg.innerHTML='<span class="text-red-500">'+esc(up.error||'could not re-arm')+'</span>';return;}
     const deadline=Date.now()+120000;
     const t=setInterval(async()=>{
       if(Date.now()>deadline){clearInterval(t);go.disabled=false;msg.innerHTML='<span class="text-red-500">taking longer than expected — check Systems in a moment.</span>';return;}
       const st=await(await fetch('/ingest-status')).json();
       if(st.state==='done'){clearInterval(t);invalidateGraphCache('incidents');closeRearm();if(curView==='systems')loadSystems();if(curView==='timeline')loadTimeline();}
       else if(st.state==='error'){clearInterval(t);go.disabled=false;msg.innerHTML='<span class="text-red-500">re-ingest failed: '+esc(st.error||'')+'</span>';}
     },2000);
   }catch(e){go.disabled=false;msg.innerHTML='<span class="text-red-500">re-arm failed</span>';}}
 (function(){const ca=$('rearmCancel'),go=$('rearmGo'),m=$('rearmModal');if(ca)ca.onclick=closeRearm;if(go)go.onclick=doRearm;if(m)m.addEventListener('click',e=>{if(e.target===m)closeRearm();});})();
 const THINKING='<span class="thsh">thinking...</span>';
 async function ask(q){q=(q||$('q').value).trim();if(!q||!ready)return;
   const t=ensureThread(q);const hist=t.msgs.slice(-6);
   t.msgs.push({role:'user',content:q});saveThreads();renderBar();
   bubble('user',esc(q));$('q').value='';autogrow();
   const c=bubble('a',THINKING);
   await answerInto(q,hist,c,t);
 }
 // Resolve one answer into bubble c, with a client-side timeout + abort so a stalled model can never
 // freeze the UI on "thinking…". On failure, offer a Retry that re-attempts into the same bubble (no
 // duplicated turns). Reused by ask() and by the Retry button.
 async function answerInto(q,hist,c,t){
   c.innerHTML=THINKING;$('bAsk').disabled=true;
   // Stream the answer token-by-token via /ask-stream (NDJSON). The 60s timeout guards only the FIRST
   // token (connection/model-busy); once tokens flow it's cleared so a long answer is never cut off.
   const ctrl=new AbortController();const to=setTimeout(()=>ctrl.abort(),60000);
   try{
     const r=await fetch('/ask-stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q,history:hist,workspace:activeWs}),signal:ctrl.signal});
     if(!r.ok||!r.body)throw new Error('http '+r.status);
     const reader=r.body.getReader();const dec=new TextDecoder();
     let buf='',answer='',cites=[],first=true,errored=null;
     while(true){
       const {value,done}=await reader.read();if(done)break;
       buf+=dec.decode(value,{stream:true});let nl;
       while((nl=buf.indexOf('\\n'))>=0){
         const line=buf.slice(0,nl).trim();buf=buf.slice(nl+1);if(!line)continue;
         let d;try{d=JSON.parse(line);}catch(_){continue;}
         if(d.t!=null){if(first){c.innerHTML='';first=false;clearTimeout(to);}answer+=d.t;c.innerHTML=fmt(answer);$('log').scrollTop=$('log').scrollHeight;}
         if(d.error)errored=d.error;
         if(d.done)cites=d.citations||[];
       }
     }
     clearTimeout(to);
     if(errored)throw new Error(errored);
     answer=answer.trim();c.innerHTML=fmt(answer);addCopy(c,answer);if(cites&&cites.length)addSources(c,cites);
     t.msgs.push({role:'assistant',content:answer,sources:cites});saveThreads();
   }catch(e){
     const slow=e&&e.name==='AbortError';
     c.innerHTML='<span class="text-red-500">'+(slow?'That took too long — the model may be busy or rate-limited.':'Could not reach the model right now.')+'</span> <button class="ml-1.5 rounded-md border border-zinc-300 px-2 py-0.5 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900">Retry</button>';
     const rb=c.querySelector('button');if(rb)rb.onclick=()=>answerInto(q,hist,c,t);
   }finally{clearTimeout(to);$('bAsk').disabled=false;$('log').scrollTop=$('log').scrollHeight;}
 }
 function askAbout(name){show('triage');ask('What should I check for '+name+', and what depends on it?');}
 const qEl=$('q');qEl.classList.add('resize-none');qEl.style.maxHeight='160px';
 function autogrow(){qEl.style.height='auto';qEl.style.height=Math.max(42,Math.min(qEl.scrollHeight,160))+'px';}
 qEl.addEventListener('input',autogrow);
 $('bAsk').onclick=()=>ask();
 qEl.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();ask();}});

 // chat threads (history) — client-side, persisted in localStorage
 const EMPTY_HTML='<div class="js-empty my-auto py-10 text-center"><h2 class="serif mx-auto max-w-lg text-3xl leading-tight tracking-tight text-zinc-900 dark:text-zinc-100">Ask anything about your incidents.</h2><p class="mx-auto mt-3 mb-6 max-w-sm text-sm text-zinc-500 dark:text-zinc-400">Plain English — what to check, who owns it, what breaks if it fails.</p><div class="mx-auto flex max-w-md flex-wrap justify-center gap-2">'+
   ['If auth-service latency is high, what should I check?|auth-service latency','Who owns the payments-service?|who owns payments?','What is affected if the search-index fails?|blast radius of search-index','What does the api-gateway do?|what is the api-gateway?','How does the payments-service depend on the auth-service?|payments ↔ auth']
   .map(s=>{const i=s.indexOf('|');return '<button data-q="'+s.slice(0,i)+'" class="'+chipCls+'">'+s.slice(i+1)+'</button>';}).join('')+'</div></div>';
 function loadThreads(){try{threads=JSON.parse(localStorage.getItem('lethe.threads.'+activeWs)||'[]');}catch(e){threads=[];}active=localStorage.getItem('lethe.active.'+activeWs)||null;if(!threads.find(t=>t.id===active))active=null;}
 function saveThreads(){try{localStorage.setItem('lethe.threads.'+activeWs,JSON.stringify(threads.slice(0,50)));localStorage.setItem('lethe.active.'+activeWs,active||'');}catch(e){}}
 function curThread(){return threads.find(t=>t.id===active)||null;}
 function ensureThread(firstQ){let t=curThread();if(!t){t={id:'c'+Date.now().toString(36)+Math.floor(Math.random()*1e4).toString(36),title:firstQ.slice(0,64),msgs:[]};threads.unshift(t);active=t.id;}return t;}
 function renderLog(){const log=$('log');log.innerHTML='';const t=curThread();if(!t||!t.msgs.length){log.insertAdjacentHTML('beforeend',EMPTY_HTML);return;}t.msgs.forEach(m=>{if(m.role==='user')bubble('user',esc(m.content));else{const c=bubble('a',fmt(m.content));addCopy(c,m.content);if(m.sources&&m.sources.length)addSources(c,m.sources);}});log.scrollTop=log.scrollHeight;}
 function renderChatList(){const el=$('chatList');if(!el)return;
   if(!threads.length){el.innerHTML='<div class="px-2.5 py-3 text-xs text-zinc-400 dark:text-zinc-600">No chats yet.</div>';return;}
   el.innerHTML='';
   threads.forEach(t=>{const row=document.createElement('div');row.className='group flex items-center rounded-lg '+(t.id===active?'bg-zinc-100 dark:bg-[#1a2233]':'hover:bg-zinc-100 dark:hover:bg-zinc-900');
     const b=document.createElement('button');b.className='flex-1 truncate px-2.5 py-1.5 text-left text-[13px] '+(t.id===active?'font-medium text-zinc-900 dark:text-white':'text-zinc-600 dark:text-zinc-400');b.textContent=t.title;b.title=t.title;b.onclick=()=>{if(curView!=='triage')show('triage');switchThread(t.id);};
     const d=document.createElement('button');d.className='mr-1 shrink-0 rounded p-1 text-zinc-400 opacity-0 transition hover:text-red-500 group-hover:opacity-100';d.setAttribute('aria-label','delete chat');d.innerHTML='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>';d.onclick=e=>{e.stopPropagation();delThread(t.id);};
     row.appendChild(b);row.appendChild(d);el.appendChild(row);});
 }
 function renderBar(){const t=curThread();$('threadTitle').textContent=t?t.title:'New chat';renderChatList();}
 function newChat(){active=null;saveThreads();$('threadMenu').classList.add('hidden');renderLog();renderBar();}
 function switchThread(id){active=id;saveThreads();$('threadMenu').classList.add('hidden');renderLog();renderBar();}
 function delThread(id){threads=threads.filter(t=>t.id!==id);if(active===id)active=null;saveThreads();renderMenu();renderLog();renderBar();}
 function renderMenu(){const m=$('threadMenu');if(!threads.length){m.innerHTML='<div class="px-3 py-6 text-center text-xs text-zinc-400">No saved chats yet.</div>';return;}
   m.innerHTML='';threads.forEach(t=>{const row=document.createElement('div');row.className='group flex items-center gap-1 rounded-lg '+(t.id===active?'bg-zinc-100 dark:bg-[#1b2230]':'');
     const b=document.createElement('button');b.className='flex-1 truncate px-2.5 py-2 text-left text-sm text-zinc-700 transition hover:text-zinc-950 dark:text-zinc-300 dark:hover:text-white';b.textContent=t.title;b.onclick=()=>switchThread(t.id);
     const d=document.createElement('button');d.className='mr-1 shrink-0 rounded p-1 text-zinc-400 opacity-0 transition hover:text-red-500 group-hover:opacity-100';d.setAttribute('aria-label','delete chat');d.innerHTML='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>';d.onclick=e=>{e.stopPropagation();delThread(t.id);};
     row.appendChild(b);row.appendChild(d);m.appendChild(row);});}
 $('threadBtn').onclick=e=>{e.stopPropagation();const m=$('threadMenu');if(m.classList.contains('hidden')){renderMenu();m.classList.remove('hidden');}else{m.classList.add('hidden');}};
 $('newChat').onclick=newChat;
 (function(){const n=$('newChatSide');if(n)n.onclick=()=>{if(curView!=='triage')show('triage');newChat();};})();
 document.addEventListener('click',e=>{const m=$('threadMenu');if(!m.classList.contains('hidden')&&!m.contains(e.target)&&!e.target.closest('#threadBtn'))m.classList.add('hidden');});
 loadThreads();renderBar();renderLog();

 // workspaces — separate Cognee datasets you switch between (Incidents = the default golden graph)
 async function loadWs(){try{wsList=((await(await fetch('/workspaces')).json()).workspaces)||[];}catch(e){wsList=[{id:'incidents',name:'Incidents'}];}
   if(!wsList.find(w=>w.id===activeWs)){activeWs='incidents';localStorage.setItem('lethe.ws',activeWs);loadThreads();renderBar();renderLog();}renderWsName();}
 function renderWsName(){const w=wsList.find(x=>x.id===activeWs);if($('wsName'))$('wsName').textContent=w?w.name:'Incidents';renderCtx();}
 // Header context pill: active workspace + system count. ctxSysCount is null until first /systems read.
 let ctxSysCount=null;
 function renderCtx(){const w=wsList.find(x=>x.id===activeWs);if($('ctxWs'))$('ctxWs').textContent=w?w.name:'Incidents';
   const known=ctxSysCount!=null;
   if($('ctxSys')){$('ctxSys').textContent=known?(ctxSysCount+' system'+(ctxSysCount===1?'':'s')):'';$('ctxSys').classList.toggle('hidden',!known);}
   if($('ctxSep'))$('ctxSep').classList.toggle('hidden',!known);}
 async function loadCtxCount(){const ws=activeWs;try{const j=await(await fetch('/systems?workspace='+encodeURIComponent(ws))).json();if(ws!==activeWs)return;ctxSysCount=(j.systems&&j.systems.length)||0;renderCtx();}catch(e){}}
 function renderWsMenu(){const m=$('wsMenu');m.innerHTML='';
   wsList.forEach(w=>{const row=document.createElement('div');row.className='group flex items-center gap-1 rounded-lg '+(w.id===activeWs?'bg-zinc-100 dark:bg-[#1b2230]':'hover:bg-zinc-100 dark:hover:bg-zinc-900');
     const b=document.createElement('button');b.className='flex min-w-0 flex-1 items-center gap-2 truncate px-2.5 py-2 text-left text-sm '+(w.id===activeWs?'font-medium text-zinc-900 dark:text-white':'text-zinc-600 dark:text-zinc-300');b.textContent=w.name;b.onclick=()=>switchWs(w.id);row.appendChild(b);
     if(w.id!=='incidents'){const d=document.createElement('button');d.className='mr-1 shrink-0 rounded p-1 text-zinc-400 opacity-0 transition hover:text-red-500 group-hover:opacity-100';d.setAttribute('aria-label','delete workspace');d.innerHTML='<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg>';d.onclick=e=>{e.stopPropagation();delWs(w.id,w.name);};row.appendChild(d);}
     m.appendChild(row);});
   const sep=document.createElement('div');sep.className='my-1 border-t border-zinc-300 dark:border-zinc-800';m.appendChild(sep);
   const nw=document.createElement('button');nw.className='flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-sm font-medium text-cyan-600 hover:bg-cyan-500/10 dark:text-cyan-400';nw.innerHTML='<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>New workspace';nw.onclick=createWs;m.appendChild(nw);}
 let wsDelPending=null;
 function delWs(id,name){wsDelPending={id:id,name:name};$('wsDelName').textContent=name;const dm=$('wsDelMsg');if(dm)dm.textContent='';$('wsDelOk').disabled=false;$('wsMenu').classList.add('hidden');const m=$('wsDelModal');m.classList.remove('hidden');m.classList.add('flex');}
 function closeWsDel(){const m=$('wsDelModal');m.classList.add('hidden');m.classList.remove('flex');wsDelPending=null;}
 async function doDelWs(){if(!wsDelPending)return;const id=wsDelPending.id;const msg=$('wsDelMsg');if(msg)msg.textContent='';$('wsDelOk').disabled=true;
   try{const r=await fetch('/workspaces/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});const j=await r.json();
     if(!r.ok||!j.ok){if(msg)msg.innerHTML='<span class="text-red-500">'+esc(j.error||'Could not delete the workspace.')+'</span>';$('wsDelOk').disabled=false;return;}
     closeWsDel();
     wsList=wsList.filter(w=>w.id!==id);
     if(activeWs===id){activeWs='incidents';localStorage.setItem('lethe.ws','incidents');ctxSysCount=null;loadThreads();renderBar();renderLog();loadCtxCount();}
     renderWsName();renderWsMenu();if(curView==='systems')loadSystems();if(curView==='graph')loadGraph();
   }catch(e){if(msg)msg.innerHTML='<span class="text-red-500">Request failed — is the server running?</span>';$('wsDelOk').disabled=false;}
 }
 (function(){const ok=$('wsDelOk'),ca=$('wsDelCancel'),m=$('wsDelModal');if(ok)ok.onclick=doDelWs;if(ca)ca.onclick=closeWsDel;if(m)m.addEventListener('click',e=>{if(e.target===m)closeWsDel();});})();
 function switchWs(id){activeWs=id;localStorage.setItem('lethe.ws',id);invalidateGraphCache();$('wsMenu').classList.add('hidden');ctxSysCount=null;renderWsName();loadCtxCount();loadThreads();renderBar();renderLog();demoCardUpd();if(curView==='systems')loadSystems();if(curView==='graph')loadGraph();if(curView==='timeline')loadTimeline();}
 function createWs(){$('wsMenu').classList.add('hidden');$('wsModalInput').value='';const cm=$('wsModalMsg');if(cm)cm.textContent='';$('wsModalOk').disabled=false;const m=$('wsModal');m.classList.remove('hidden');m.classList.add('flex');setTimeout(()=>$('wsModalInput').focus(),40);}
 function closeWsModal(){const m=$('wsModal');m.classList.add('hidden');m.classList.remove('flex');}
 async function doCreateWs(){const name=($('wsModalInput').value||'').trim();if(!name)return;const msg=$('wsModalMsg');if(msg)msg.textContent='';$('wsModalOk').disabled=true;
   try{const r=await fetch('/workspaces',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name})});const j=await r.json();
     if(r.ok&&j.workspace){wsList.push(j.workspace);closeWsModal();switchWs(j.workspace.id);}
     else{if(msg)msg.innerHTML='<span class="text-red-500">'+esc(j.error||'Could not create the workspace.')+'</span>';}
   }catch(e){if(msg)msg.innerHTML='<span class="text-red-500">Request failed — is the server running?</span>';}
   $('wsModalOk').disabled=false;}
 $('wsModalOk').onclick=doCreateWs;$('wsModalCancel').onclick=closeWsModal;
 $('wsModalInput').addEventListener('keydown',e=>{if(e.key==='Enter')doCreateWs();else if(e.key==='Escape')closeWsModal();});
 $('wsModal').addEventListener('click',e=>{if(e.target===$('wsModal'))closeWsModal();});
 // Esc closes whichever modal / graph panel is open (backdrop-click already works everywhere).
 document.addEventListener('keydown',function(e){
   if(e.key!=='Escape')return;
   const ids=['srcPeekX','rearmCancel','fgDone','fgCancel','fgX','setCancel','addCancel','wsDelCancel','wsModalCancel','gpclose'];
   for(let i=0;i<ids.length;i++){const el=$(ids[i]);if(el&&el.offsetParent!==null){el.click();return;}}
 });
 $('wsBtn').onclick=e=>{e.stopPropagation();const m=$('wsMenu');if(m.classList.contains('hidden')){renderWsMenu();m.classList.remove('hidden');}else{m.classList.add('hidden');}};
 document.addEventListener('click',e=>{const m=$('wsMenu');if(m&&!m.classList.contains('hidden')&&!m.contains(e.target)&&!e.target.closest('#wsBtn'))m.classList.add('hidden');});
 loadWs();

 async function loadSystems(){
   const g=$('sysgrid');g.classList.remove('card-enter');g.innerHTML=Array(4).fill('<div class="rounded-xl border border-zinc-200 bg-white/50 px-4 py-3 dark:border-zinc-800 dark:bg-[#141b2b]/50"><div class="skel h-4 w-24"></div><div class="skel mt-2.5 h-3 w-40"></div><div class="mt-3.5 flex justify-end"><div class="skel h-7 w-28"></div></div></div>').join('');
   try{const j=await(await fetch('/systems?workspace='+encodeURIComponent(activeWs))).json();
     ctxSysCount=(j.systems&&j.systems.length)||0;renderCtx();
     if(!j.systems.length){g.innerHTML='<div class="col-span-full rounded-xl border border-dashed border-zinc-300 bg-white/50 p-8 text-center dark:border-zinc-700 dark:bg-[#141b2b]/50"><p class="text-sm text-zinc-500 dark:text-zinc-400">No systems in this workspace yet.</p><button id="emptyAdd" class="mt-3 rounded-full bg-cyan-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-cyan-500">Add a system</button></div>';const ea=$('emptyAdd');if(ea)ea.onclick=()=>{const b=$('addSysBtn');if(b)b.click();};return;}
     g.innerHTML='';g.classList.add('card-enter');
     // M4: after the hero forget, the golden Systems view offers an in-app re-arm (re-ingest, not undo).
     if(activeWs==='incidents'&&!j.systems.some(s=>s.name==='legacy-cache')){
       const rc=document.createElement('div');
       rc.className='col-span-full rounded-xl border border-cyan-300/60 bg-cyan-50/40 p-4 dark:border-cyan-800/40 dark:bg-cyan-950/15';
       rc.innerHTML='<div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between"><div><div class="text-sm font-medium text-zinc-800 dark:text-zinc-100">Demo was run &mdash; <span class="mono">legacy-cache</span> was verifiably forgotten.</div><p class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">Re-ingest its 2 runbooks to run the forget demo again. ~30s &middot; uses your LLM key &middot; the Timeline receipt stays.</p></div><button id="rearmBtn" class="shrink-0 rounded-full border border-cyan-500/50 bg-white/70 px-4 py-2 text-sm font-medium text-cyan-700 transition hover:bg-cyan-50 dark:bg-transparent dark:text-cyan-300 dark:hover:bg-cyan-950/30">Re-arm the demo</button></div>';
       g.appendChild(rc);const rb=rc.querySelector('#rearmBtn');if(rb)rb.onclick=openRearm;
     }
     const staleDays=j.stale_days||180;
     j.systems.forEach((s,idx)=>{
       const card=document.createElement('div');
       card.className='glow lift flex flex-col gap-3 rounded-xl border border-zinc-300 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-[#141b2b]';
       card.setAttribute('data-glow','');card.style.setProperty('--i',idx);
       let meta=s.docs+' document'+(s.docs===1?'':'s');let badge='';const overdue=(s.age_days!=null&&s.age_days>staleDays);
       if(s.age_days!=null){const ago=s.age_days>=365?(s.age_days/365).toFixed(1)+'y':s.age_days+'d';meta+=' · reviewed '+ago+' ago';if(overdue)badge=' <span class="ml-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">overdue</span>';}
       if(s.demoted)badge+=' <span class="ml-1 inline-flex items-center gap-1 rounded-full border border-amber-300/70 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:border-amber-800/50 dark:bg-amber-950/30 dark:text-amber-300"><svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>demoted · reversible</span>';
       const markBtn=overdue?'<button data-sys="'+esc(s.name)+'" class="shrink-0 rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 transition hover:bg-emerald-100 dark:border-emerald-800/60 dark:bg-emerald-950/30 dark:text-emerald-300 dark:hover:bg-emerald-900/40">Mark reviewed</button>':'';
       card.innerHTML='<div class="min-w-0"><div class="mono text-sm font-medium">'+esc(s.name)+'</div><div class="mt-0.5 text-xs text-zinc-400">'+meta+badge+'</div></div>'+
         '<div class="flex flex-wrap items-center justify-end gap-2">'+markBtn+'<button class="decom shrink-0 rounded-full border border-red-300 bg-red-50/50 px-3 py-1.5 text-xs font-medium text-red-600 transition hover:-translate-y-px hover:border-red-400 hover:bg-red-50 dark:border-red-800/70 dark:bg-red-950/20 dark:text-red-400 dark:hover:bg-red-950/40">Decommission</button></div>';
       card.querySelector('.decom').onclick=()=>forgetFlow(s.name,card);
       const mb=card.querySelector('[data-sys]');if(mb)mb.onclick=()=>markReviewed(mb);
       g.appendChild(card);
     });
   }catch(e){g.innerHTML='<div class="col-span-full rounded-xl border border-red-300/60 bg-red-50/40 p-4 text-sm text-red-600 dark:border-red-900/40 dark:bg-red-950/20 dark:text-red-400">Could not load systems. Check the server is running, then reopen this view.</div>';}
 }

 // Curation (memory hygiene) — proactively flag remaining docs that still reference a decommissioned
 // system. Step 1 is DETERMINISTIC: pure text matching, zero LLM calls (the strongest token guardrail).
 // Curation opens as a live dashboard: auto-run the two FREE (0-token, deterministic) scans on first open;
 // the conflict scan stays MANUAL because it spends model tokens (cost stays opt-in + transparent).
 // S1: Memory health — a deterministic, 0-token score (no gauge, no sidebar number). Computed client-side
 // from the same aging + stale-ref scans the curation view already runs. Formula: 100 - 3*overdue(cap30)
 // - 5*veryStale(cap20) - 8*staleRefs(cap24). Golden = 7 / 3 / 0 -> 64.
 function memScore(o,v,s){return Math.max(0,100-Math.min(3*o,30)-Math.min(5*v,20)-Math.min(8*s,24));}
 function tweenCount(el,to){if(!el)return;if(gReduced){el.textContent=to;return;}var t0=performance.now();
   (function step(now){var k=Math.min(1,(now-t0)/700);el.textContent=Math.round(to*(1-Math.pow(1-k,3)));if(k<1)requestAnimationFrame(step);})(t0);
   setTimeout(function(){el.textContent=to;},820);}  // guarantee the final value even if rAF is throttled (background tab)
 function curationDot(score){var nav=document.querySelector('[data-view="curation"]');if(!nav)return;nav.classList.add('relative');var dot=nav.querySelector('.curdot');
   if(score<80&&!dot){dot=document.createElement('span');dot.className='curdot absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-amber-500';dot.title='Memory health below 80';nav.appendChild(dot);}
   else if(score>=80&&dot){dot.remove();}}
 async function loadMemHealth(){var box=$('memHealth');var o=0,v=0,s=0;
   try{var aj=await(await fetch('/curation/aging?workspace='+encodeURIComponent(activeWs)+'&days=180')).json();var ag=aj.aging||[];o=ag.length;v=ag.filter(function(x){return x.age_days>360;}).length;}catch(e){}
   try{var cj=await(await fetch('/curation?workspace='+encodeURIComponent(activeWs))).json();s=(cj.findings||[]).length;}catch(e){}
   var score=memScore(o,v,s);curationDot(score);if(!box)return;
   var ageDed=Math.min(3*o,30)+Math.min(5*v,20),refDed=Math.min(8*s,24);
   var why=o+' overdue · '+v+' very stale · '+s+' stale reference'+(s===1?'':'s');
   box.innerHTML='<div class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5"><span class="serif text-2xl tracking-tight text-zinc-900 dark:text-zinc-100">Memory health <span id="memScore">0</span></span><span class="text-sm text-zinc-500 dark:text-zinc-400">'+why+'</span></div>'+
     '<div class="mt-2 flex h-1 w-full max-w-md overflow-hidden rounded-full bg-zinc-200/60 dark:bg-zinc-800/60"><div style="width:'+score+'%" class="h-full bg-emerald-500/80"></div><div style="width:'+ageDed+'%" class="h-full bg-amber-500/80"></div><div style="width:'+refDed+'%" class="h-full bg-red-500/80"></div></div>'+
     '<div class="mono mt-1 text-[10px] uppercase tracking-wide text-zinc-400">deterministic · free · 0 tokens</div>';
   tweenCount($('memScore'),score);}
 function loadCuration(){loadMemHealth();const cyc=$('curCycBody');if(cyc&&cyc.dataset.ran!=='1')cyc.innerHTML='<div class="text-sm text-zinc-400">Click <span class="font-medium text-zinc-500 dark:text-zinc-300">Run cycle</span> for a free preview of what would be demoted or queued — nothing changes until you apply.</div>';
   const b=$('curBody');if(b&&b.dataset.ran!=='1')runCuration();
   const cb=$('curConfBody');if(cb&&cb.dataset.ran!=='1')cb.innerHTML='<div class="text-sm text-zinc-400">Scan for contradictions between runbooks. <span class="text-amber-600 dark:text-amber-400">Uses your model</span> — click Scan when you want it.</div>';
   const ab=$('curAgeBody');if(ab&&ab.dataset.ran!=='1')runAging();
   const pb=$('curPropBody');if(pb&&pb.dataset.ran!=='1')runProposals();}
 // 2-row shimmer skeleton for curation scans (replaces the old "scanning..." text line)
 function curSkel(){return '<div class="space-y-2.5"><div class="skel h-16 w-full"></div><div class="skel h-16 w-full"></div></div>';}
 async function runAging(){const b=$('curAgeBody');if(!b)return;b.dataset.ran='1';b.innerHTML=curSkel();
   try{const j=await(await fetch('/curation/aging?workspace='+encodeURIComponent(activeWs)+'&days=180')).json();
     const a=j.aging||[];const meta=(j.scanned||0)+' runbooks · 0 tokens';
     if(!a.length){b.innerHTML='<div class="rounded-xl border border-emerald-200 bg-emerald-50/50 p-4 text-sm text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300"><span class="font-semibold">All fresh.</span> Every runbook was reviewed within '+(j.days||180)+' days. <span class="opacity-70">('+meta+')</span></div>';return;}
     let h='<div class="mb-3 text-sm text-zinc-500 dark:text-zinc-400"><span class="font-medium text-amber-600 dark:text-amber-400">'+a.length+' overdue for review</span> · '+meta+'</div><div class="card-enter space-y-2.5">';
     a.forEach((x,idx)=>{const ago=x.age_days>=365?(x.age_days/365).toFixed(1)+' years':Math.round(x.age_days/30)+' months';h+='<div style="--i:'+idx+'" class="rounded-xl border border-amber-200 bg-amber-50/40 p-4 dark:border-amber-900/40 dark:bg-amber-950/15"><div class="flex items-start justify-between gap-3"><div><div class="flex flex-wrap items-center gap-1.5 text-sm"><span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.system)+'</span><span class="text-zinc-400">last reviewed</span><span class="mono font-medium text-amber-600 dark:text-amber-400">'+esc(x.reviewed)+'</span></div><p class="mt-1.5 text-xs text-zinc-500 dark:text-zinc-400">'+x.age_days+' days ago (~'+ago+') — verify it is still accurate.</p></div><button data-sys="'+esc(x.system)+'" class="shrink-0 rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 transition hover:bg-emerald-100 dark:border-emerald-800/60 dark:bg-emerald-950/30 dark:text-emerald-300 dark:hover:bg-emerald-900/40">Mark reviewed</button></div></div>';});
     h+='</div>';b.innerHTML=h;
     b.querySelectorAll('button[data-sys]').forEach(btn=>btn.onclick=()=>markReviewed(btn));
   }catch(e){b.innerHTML='<div class="text-sm text-red-500">scan failed</div>';}
 }
 // Aging detection→action: mark a runbook reviewed-today → it leaves the overdue list + lands on the Timeline.
 async function markReviewed(btn){const sys=btn.dataset.sys;btn.disabled=true;btn.classList.add('opacity-50','pointer-events-none');
   try{const r=await(await fetch('/curation/review',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({system:sys,workspace:activeWs})})).json();
     if(r&&r.ok){loadMemHealth();if(curView==='systems')loadSystems();if($('curAgeBody')&&$('curAgeBody').dataset.ran==='1')runAging();if($('curPropBody')&&$('curPropBody').dataset.ran==='1')runProposals();if(curView==='timeline')loadTimeline();}else{btn.disabled=false;btn.classList.remove('opacity-50','pointer-events-none');}
   }catch(e){btn.disabled=false;btn.classList.remove('opacity-50','pointer-events-none');}
 }
 (function(){const r=$('curAgeRun');if(r)r.onclick=runAging;})();
 // Retirement proposals (forget-as-policy) — DETERMINISTIC, read-only retirement CANDIDATES (long-overdue
 // runbooks). PROPOSALS ONLY: each row offers two HUMAN-GATED actions and nothing is deleted automatically.
 //  • "Forget…" reuses the EXISTING Proof-of-Forgetting modal (forgetFlow → /forget → receipt + certificate).
 //  • "Keep / mark reviewed" reuses the EXISTING mark-reviewed action so the human can dismiss a proposal.
 async function runProposals(){const b=$('curPropBody');if(!b)return;b.dataset.ran='1';b.innerHTML=curSkel();
   try{const j=await(await fetch('/curation/proposals?workspace='+encodeURIComponent(activeWs)+'&days=365')).json();
     const p=j.proposals||[];const meta=(j.scanned||0)+' runbooks · 0 tokens';
     if(!p.length){b.innerHTML='<div class="rounded-xl border border-emerald-200 bg-emerald-50/50 p-4 text-sm text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300"><span class="font-semibold">Nothing to retire.</span> No runbook is overdue past '+(j.days||365)+' days. <span class="opacity-70">('+meta+')</span></div>';return;}
     let h='<div class="mb-2 text-sm text-zinc-500 dark:text-zinc-400"><span class="font-medium text-amber-600 dark:text-amber-400">'+p.length+' retirement candidate'+(p.length===1?'':'s')+'</span> · '+meta+'</div>'+
       '<p class="mb-3 text-xs text-zinc-400">Proposals only — nothing is deleted until you confirm.</p><div class="card-enter space-y-2.5">';
     p.forEach((x,idx)=>{const ago=x.age_days>=365?(x.age_days/365).toFixed(1)+' years':Math.round(x.age_days/30)+' months';
       h+='<div style="--i:'+idx+'" class="rounded-xl border border-amber-200 bg-amber-50/40 p-4 dark:border-amber-900/40 dark:bg-amber-950/15"><div class="flex items-start justify-between gap-3"><div class="min-w-0">'+
         '<div class="flex flex-wrap items-center gap-1.5 text-sm"><span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.system)+'</span><span class="text-zinc-400">last reviewed</span><span class="mono font-medium text-amber-600 dark:text-amber-400">'+esc(x.last_reviewed)+'</span></div>'+
         '<p class="mt-1.5 text-xs text-zinc-500 dark:text-zinc-400">'+x.age_days+' days ago (~'+ago+') — looks retired. '+esc((x.reasons||[])[0]||'')+'</p></div>'+
         '<div class="flex shrink-0 items-center gap-2">'+
         '<button data-prop-forget="'+esc(x.system)+'" class="rounded-full border border-red-300 bg-red-50/50 px-3 py-1.5 text-xs font-medium text-red-600 transition hover:border-red-400 hover:bg-red-50 dark:border-red-800/70 dark:bg-red-950/20 dark:text-red-400 dark:hover:bg-red-950/40">Forget…</button>'+
         '<button data-prop-keep="'+esc(x.system)+'" class="rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 transition hover:bg-emerald-100 dark:border-emerald-800/60 dark:bg-emerald-950/30 dark:text-emerald-300 dark:hover:bg-emerald-900/40">Keep / mark reviewed</button>'+
         '</div></div></div>';});
     h+='</div>';b.innerHTML=h;
     b.querySelectorAll('button[data-prop-forget]').forEach(btn=>btn.onclick=()=>forgetFlow(btn.getAttribute('data-prop-forget'),null));
     b.querySelectorAll('button[data-prop-keep]').forEach(btn=>{btn.dataset.sys=btn.getAttribute('data-prop-keep');btn.onclick=()=>markReviewed(btn);});
   }catch(e){b.innerHTML='<div class="text-sm text-red-500">scan failed</div>';}
 }
 (function(){const r=$('curPropRun');if(r)r.onclick=runProposals;})();
 // The curation/decay CYCLE — the headline loop. "Run cycle" = a dry-run PREVIEW (free · 0 tokens · no
 // mutation): what WOULD be auto-demoted (reversible) + what is queued for human approval (hard-delete).
 // "Apply" performs ONLY the reversible demotes; permanent deletes always wait for a human via forgetFlow.
 function cycTierBadge(w){return (w!=null&&w<=0.05)
   ?'<span class="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-700 dark:bg-red-950/40 dark:text-red-300">Deep</span>'
   :'<span class="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:bg-amber-950/40 dark:text-amber-300">Mild</span>';}
 async function runCurationCycle(apply){
   const b=$('curCycBody');if(!b)return;b.dataset.ran='1';
   b.innerHTML='<div class="text-sm text-zinc-500">'+(apply?'applying demotes':'previewing the cycle')+'<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></div>';
   try{
     const j=await(await fetch('/curation/cycle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace:activeWs,dry_run:!apply})})).json();
     if(j.error){b.innerHTML='<div class="text-sm text-red-500">'+esc(j.error)+'</div>';return;}
     renderCycle(b,j,apply);
     if(apply)invalidateGraphCache(activeWs);
     if(apply&&curView==='timeline')loadTimeline();
   }catch(e){b.innerHTML='<div class="text-sm text-red-500">cycle failed</div>';}
 }
 function renderCycle(b,j,applied){
   const auto=j.auto_demoted||[],q=j.queued_for_approval||[],h=j.health||{};
   if(!auto.length&&!q.length){b.innerHTML='<div class="rounded-xl border border-emerald-200 bg-emerald-50/50 p-4 text-sm text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300"><span class="font-semibold">All current.</span> No runbook is overdue for review — nothing to demote.</div>';return;}
   let html='<div class="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-500 dark:text-zinc-400">'+
     '<span><span class="mono font-medium text-zinc-700 dark:text-zinc-200">'+(h.systems||0)+'</span> systems</span>'+
     '<span class="text-zinc-300 dark:text-zinc-700">·</span><span><span class="mono font-medium text-amber-600 dark:text-amber-400">'+(h.overdue||0)+'</span> overdue</span>'+
     '<span class="text-zinc-300 dark:text-zinc-700">·</span><span><span class="mono font-medium text-amber-600 dark:text-amber-400">'+auto.length+'</span> '+(applied?'demoted':'to demote')+'</span>'+
     '<span class="text-zinc-300 dark:text-zinc-700">·</span><span><span class="mono font-medium text-red-600 dark:text-red-400">'+q.length+'</span> queued</span></div>';
   if(auto.length){
     html+='<div class="mb-1.5 text-xs font-medium text-zinc-600 dark:text-zinc-300">'+(applied?'Auto-demoted (reversible)':'Will auto-demote (reversible)')+'</div><div class="space-y-2">';
     auto.forEach(x=>{html+='<div class="flex items-start justify-between gap-3 rounded-lg border border-zinc-200 bg-white/70 px-3 py-2 dark:border-zinc-800 dark:bg-[#0e1422]/50">'+
       '<div class="min-w-0"><div class="flex flex-wrap items-center gap-1.5 text-sm">'+cycTierBadge(x.weight)+'<span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.system)+'</span>'+
       (applied?'<span class="rounded-full bg-cyan-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-cyan-700 dark:bg-cyan-950/40 dark:text-cyan-300">Demoted ✓</span>':'')+'</div>'+
       '<p class="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">'+esc(x.reason||('not reviewed in '+x.age_days+'d'))+(x.nodes!=null?' · <span class="mono">'+x.nodes+' nodes</span>':'')+'</p></div>'+
       (applied?'<button data-restore="'+esc(x.system)+'" class="shrink-0 rounded-lg border border-zinc-300 px-2.5 py-1 text-xs font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900">Restore</button>':'')+
       '</div>';});
     html+='</div>';
   }
   if(q.length){
     html+='<div class="mb-1.5 mt-4 text-xs font-medium text-zinc-600 dark:text-zinc-300">Queued for your approval — permanent delete</div>'+
       '<p class="mb-2 text-xs text-zinc-400">Already demoted, so they are no longer recommended. Nothing is deleted until you confirm.</p><div class="space-y-2">';
     q.forEach(x=>{html+='<div class="flex items-start justify-between gap-3 rounded-lg border border-red-200 bg-red-50/40 px-3 py-2 dark:border-red-900/40 dark:bg-red-950/15">'+
       '<div class="min-w-0"><div class="flex flex-wrap items-center gap-1.5 text-sm"><span class="rounded-full bg-red-100 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-red-700 dark:bg-red-950/40 dark:text-red-300">Queued</span><span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.system)+'</span></div>'+
       '<p class="mt-0.5 text-xs text-zinc-500 dark:text-zinc-400">'+esc(x.reason||('very stale ('+x.age_days+'d)'))+'</p></div>'+
       '<button data-cyc-forget="'+esc(x.system)+'" class="shrink-0 rounded-full border border-red-300 bg-red-50/50 px-2.5 py-1 text-xs font-medium text-red-600 transition hover:border-red-400 hover:bg-red-50 dark:border-red-800/70 dark:bg-red-950/20 dark:text-red-400 dark:hover:bg-red-950/40">Forget…</button>'+
       '</div>';});
     html+='</div>';
   }
   if(!applied&&auto.length){
     html+='<div class="mt-4 flex items-center justify-between gap-3"><span class="text-xs text-zinc-400">Preview — nothing has changed yet.</span>'+
       '<button id="curCycApply" class="rounded-full bg-cyan-600 px-3.5 py-2 text-sm font-medium text-white transition hover:bg-cyan-500">Apply '+auto.length+' demote'+(auto.length===1?'':'s')+'</button></div>';
   } else if(applied){
     html+='<div class="mt-4 flex items-center gap-2 text-xs text-cyan-700 dark:text-cyan-300"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Demotes applied and recorded to the Timeline. Reversible — Restore any system above, or reset the demo to clear all.</div>';
   }
   b.innerHTML=html;
   const ap=$('curCycApply');if(ap)ap.onclick=()=>runCurationCycle(true);
   b.querySelectorAll('button[data-restore]').forEach(btn=>btn.onclick=()=>restoreSystem(btn));
   b.querySelectorAll('button[data-cyc-forget]').forEach(btn=>btn.onclick=()=>forgetFlow(btn.getAttribute('data-cyc-forget'),null));
 }
 async function restoreSystem(btn){const sys=btn.getAttribute('data-restore');btn.disabled=true;btn.textContent='…';
   try{const r=await(await fetch('/curation/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({system:sys,workspace:activeWs})})).json();
     if(r&&r.ok){btn.outerHTML='<span class="shrink-0 text-xs font-medium text-emerald-600 dark:text-emerald-400">Restored ✓</span>';if(curView==='timeline')loadTimeline();}
     else{btn.disabled=false;btn.textContent='Restore';}
   }catch(e){btn.disabled=false;btn.textContent='Restore';}
 }
 (function(){const r=$('curCycRun');if(r)r.onclick=()=>runCurationCycle(false);})();
 async function runConflicts(){const b=$('curConfBody');if(!b)return;b.dataset.ran='1';b.innerHTML=curSkel();
   try{const j=await(await fetch('/curation/conflicts?workspace='+encodeURIComponent(activeWs))).json();
     const c=j.conflicts||[];const meta=(j.pairs_checked||0)+' pair'+(j.pairs_checked===1?'':'s')+' checked'+(j.capped?' (capped at 8)':'');
     if(!c.length){b.innerHTML='<div class="rounded-xl border border-emerald-200 bg-emerald-50/50 p-4 text-sm text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300"><span class="font-semibold">No contradictions.</span> Your runbooks agree with each other. <span class="opacity-70">('+meta+')</span></div>';return;}
     let h='<div class="mb-3 text-sm text-zinc-500 dark:text-zinc-400"><span class="font-medium text-amber-600 dark:text-amber-400">'+c.length+' conflict'+(c.length===1?'':'s')+'</span> · '+meta+'</div><div class="card-enter space-y-2.5">';
     c.forEach((x,idx)=>{h+='<div style="--i:'+idx+'" class="rounded-xl border border-amber-200 bg-amber-50/40 p-4 dark:border-amber-900/40 dark:bg-amber-950/15"><div class="flex flex-wrap items-center gap-1.5 text-sm"><span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.a)+'</span><span class="text-zinc-400">contradicts</span><span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.b)+'</span></div><p class="mt-2 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">'+esc(x.detail)+'</p></div>';});
     h+='</div>';b.innerHTML=h;
   }catch(e){b.innerHTML='<div class="text-sm text-red-500">scan failed</div>';}
 }
 (function(){const r=$('curConfRun');if(r)r.onclick=runConflicts;})();
 async function runCuration(){const b=$('curBody');b.dataset.ran='1';b.innerHTML=curSkel();
   try{const j=await(await fetch('/curation?workspace='+encodeURIComponent(activeWs))).json();
     const f=j.findings||[];const meta=(j.scanned||0)+' docs scanned · 0 tokens';
     if(!f.length){b.innerHTML='<div class="rounded-xl border border-emerald-200 bg-emerald-50/50 p-4 text-sm text-emerald-700 dark:border-emerald-900/40 dark:bg-emerald-950/20 dark:text-emerald-300"><span class="font-semibold">Clean.</span> No remaining runbook references a decommissioned system. <span class="opacity-70">('+meta+')</span></div>';return;}
     const hl=(snip,dead)=>{const e=esc(snip);try{return e.replace(new RegExp('('+dead.replace(/[^A-Za-z0-9]/g,'\\\\$&')+')','ig'),'<span class="rounded bg-red-100 px-1 font-medium text-red-700 line-through decoration-red-400 dark:bg-red-950/50 dark:text-red-300">$1</span>');}catch(_){return e;}};
     let h='<div class="mb-3 text-sm text-zinc-500 dark:text-zinc-400"><span class="font-medium text-amber-600 dark:text-amber-400">'+f.length+' stale reference'+(f.length===1?'':'s')+'</span> · '+meta+'</div><div class="card-enter space-y-2.5">';
     f.forEach((x,idx)=>{h+='<div style="--i:'+idx+'" class="rounded-xl border border-amber-200 bg-amber-50/40 p-4 dark:border-amber-900/40 dark:bg-amber-950/15"><div class="flex flex-wrap items-center gap-1.5 text-sm"><span class="mono font-medium text-zinc-900 dark:text-zinc-100">'+esc(x.system)+'</span><span class="text-zinc-400">still mentions</span><span class="mono font-medium text-red-600 dark:text-red-400">'+esc(x.stale_ref)+'</span></div><p class="mt-2 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">…'+hl(x.snippet,x.stale_ref)+'…</p></div>';});
     h+='</div>';b.innerHTML=h;
   }catch(e){b.innerHTML='<div class="text-sm text-red-500">scan failed</div>';}
 }
 (function(){const r=$('curRun');if(r)r.onclick=runCuration;})();

 // Memory timeline — durable audit log of what this memory learned / forgot, and when (forgets carry the real receipt).
 const TL_META={
   forgotten:{label:'Forgotten',dot:'bg-red-500',badge:'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300'},
   added:{label:'Added',dot:'bg-emerald-500',badge:'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300'},
   reviewed:{label:'Reviewed',dot:'bg-zinc-400 dark:bg-zinc-500',badge:'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300'},
   demoted:{label:'Demoted',dot:'bg-amber-500',badge:'bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-300'},
   restored:{label:'Restored',dot:'bg-cyan-500',badge:'bg-cyan-100 text-cyan-700 dark:bg-cyan-950/40 dark:text-cyan-300'},
   _default:{label:'Event',dot:'bg-zinc-400',badge:'bg-zinc-100 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300'}
 };
 function fmtTs(ts){const p=String(ts||'').slice(0,10).split('-');if(p.length===3){const d=new Date(+p[0],+p[1]-1,+p[2]);if(!isNaN(d))return d.toLocaleDateString(undefined,{year:'numeric',month:'short',day:'numeric'});}return esc(String(ts||''));}
 async function loadTimeline(){
   const b=$('tlBody');if(!b)return;
   b.innerHTML='<div class="ml-2 space-y-5 border-l border-zinc-200 dark:border-zinc-800">'+Array(3).fill('<div class="relative mb-5 ml-5"><span class="skel absolute -left-[1.65rem] top-1 h-3 w-3 rounded-full"></span><div class="skel h-3 w-40"></div><div class="skel mt-2 h-3 w-56"></div></div>').join('')+'</div>';
   try{
     const j=await(await fetch('/timeline?workspace='+encodeURIComponent(activeWs))).json();
     const ev=j.events||[];
     if(!ev.length){b.innerHTML='<div class="rounded-xl border border-zinc-300 bg-white/50 p-5 text-sm text-zinc-500 dark:border-zinc-800 dark:bg-[#141b2b]/40 dark:text-zinc-400">No memory events yet for this workspace.</div>';return;}
     let h='<ol class="card-enter relative ml-2 border-l border-zinc-200 dark:border-zinc-800">';
     ev.forEach((e,idx)=>{
       const m=TL_META[e.op]||TL_META._default, d=e.detail||{};
       let body='';
       if(e.op==='forgotten'){
         const bits=[];
         if(d.docs!=null)bits.push(d.docs+' doc'+(d.docs===1?'':'s'));
         if(d.nodes_removed!=null)bits.push(d.nodes_removed+' nodes');
         if(d.edges_removed!=null)bits.push(d.edges_removed+' edges');
         body='<p class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">Removed from the graph + vectors'+(bits.length?': <span class="mono text-red-600 dark:text-red-400">'+bits.join(' · ')+'</span>':'')+'.</p>';
         if(d.proof_answer)body+='<p class="mt-1.5 rounded-md bg-zinc-100 px-2.5 py-1.5 text-xs italic text-zinc-600 dark:bg-zinc-900/60 dark:text-zinc-400">Re-query proof: “'+esc(String(d.proof_answer).slice(0,200))+'”</p>';
         // Certificate of Erasure — verifiable, exportable record of this deletion (only for events that carry a stable id).
         if(e.id)body+='<a href="/certificate/'+encodeURIComponent(activeWs)+'/'+encodeURIComponent(e.id)+'" target="_blank" rel="noopener" class="mt-1.5 inline-flex items-center gap-1 text-xs font-medium text-cyan-600 transition hover:text-cyan-500 dark:text-cyan-400"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>Certificate</a>';
       } else if(e.op==='added'){
         body='<p class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">Ingested into memory and made searchable.</p>';
       } else if(e.op==='reviewed'){
         body='<p class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">Runbook last verified accurate.</p>';
       } else if(e.op==='demoted'){
         const tier=(d.weight!=null&&d.weight<=0.05)?'deep':'mild';
         body='<p class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">Down-ranked ('+tier+') by the curation cycle — its advice stops being recommended'+(d.nodes!=null?': <span class="mono text-amber-600 dark:text-amber-400">'+d.nodes+' nodes</span>':'')+'. <span class="text-zinc-400">Reversible.</span></p>';
       } else if(e.op==='restored'){
         const heal=d.via==='self-heal';
         body='<p class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">'+(heal?'Re-reviewed, so it self-healed — brought back to normal ranking':'Brought back to normal ranking')+(d.nodes!=null?': <span class="mono text-cyan-600 dark:text-cyan-400">'+d.nodes+' nodes</span>':'')+'.</p>';
       }
       h+='<li class="relative mb-5 ml-5" style="--i:'+idx+'"><span class="absolute -left-[1.65rem] top-1 h-3 w-3 rounded-full ring-4 ring-[#f6f8fc] dark:ring-[#0a0e1a] '+m.dot+'"></span>'
         +'<div class="flex flex-wrap items-center gap-2"><span class="'+m.badge+' rounded-full px-2 py-0.5 text-[11px] font-medium">'+m.label+'</span><span class="mono text-sm font-medium text-zinc-900 dark:text-zinc-100">'+esc(e.system)+'</span><span class="text-xs text-zinc-400">'+fmtTs(e.ts)+'</span></div>'
         +body+'</li>';
     });
     h+='</ol>';
     b.innerHTML=h;
   }catch(err){b.innerHTML='<div class="text-sm text-red-500">Could not load timeline.</div>';}
 }

 // Proof of Forgetting — premium decommission flow: confirm -> dissolve + verify -> receipt (the differentiator made visible)
 let fgCard=null,fgSys=null,fgDidForget=false;
 function fgOpen(){const m=$('fgModal');m.classList.remove('hidden');m.classList.add('flex');}
 function fgClose(){const m=$('fgModal');m.classList.add('hidden');m.classList.remove('flex');const refresh=fgDidForget;fgDidForget=false;fgCard=null;fgSys=null;if(refresh){if(curView==='systems')loadSystems();if(curView==='curation'&&$('curPropBody')&&$('curPropBody').dataset.ran==='1')runProposals();if(curView==='curation'&&$('curAgeBody')&&$('curAgeBody').dataset.ran==='1')runAging();}}
 function forgetFlow(sys,card){fgSys=sys;fgCard=card;fgDidForget=false;
   $('fgBody').innerHTML=
     '<div class="flex items-center gap-2.5"><span class="grid h-9 w-9 place-items-center rounded-full border border-red-300 bg-red-50 text-red-600 dark:border-red-800/70 dark:bg-red-950/30 dark:text-red-400"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/></svg></span><h3 class="text-base font-semibold tracking-tight">Decommission '+esc(sys)+'?</h3></div>'+
     '<p class="mt-3 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">This is a <b class="font-medium text-zinc-700 dark:text-zinc-200">verifiable hard-delete</b> — every document, graph node and vector for <span class="mono">'+esc(sys)+'</span> is removed for good. Lethe proves it afterwards.</p>'+
     '<div class="mt-5 flex justify-end gap-2"><button id="fgCancel" class="rounded-lg px-3.5 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900">Cancel</button>'+
     '<button id="fgGo" class="rounded-full bg-red-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-red-700">Decommission</button></div>';
   $('fgCancel').onclick=fgClose;$('fgGo').onclick=fgRun;fgOpen();
 }
 async function fgRun(){
   $('fgBody').innerHTML=
     '<div class="py-7 text-center"><div class="mx-auto mb-4 h-7 w-7"><svg class="animate-spin text-cyan-500" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 1 1-6.2-8.6" stroke-linecap="round"/></svg></div>'+
     '<p class="text-sm font-medium">Forgetting <span class="mono">'+esc(fgSys)+'</span>…</p>'+
     '<p class="mt-1 text-xs text-zinc-400">removing from the graph + vectors, then verifying</p></div>';
   const ac=new AbortController();const to=setTimeout(()=>ac.abort(),75000);  // don't spin forever if the server stalls
   try{const res=await fetch('/forget',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({system:fgSys,workspace:activeWs}),signal:ac.signal});
     clearTimeout(to);if(!res.ok)throw new Error('bad status');const j=await res.json();fgDidForget=true;invalidateGraphCache(activeWs);
     if(fgCard){fgCard.classList.add('dissolving');const c=fgCard;setTimeout(()=>{c&&c.remove();},760);}  // only dissolve AFTER the delete actually succeeds
     const rec=j.receipt||{system:fgSys,docs:0};if(!rec.event_id&&j.event_id)rec.event_id=j.event_id;rec._ws=j.workspace||activeWs;fgReceipt(rec);
   }catch(e){clearTimeout(to);const msg=e&&e.name==='AbortError'?'This is taking too long — the decommission may still be finishing. Refresh Systems in a moment to check.':'Could not complete the decommission.';$('fgBody').innerHTML='<div class="py-7 text-center"><p class="text-sm text-red-500">'+esc(msg)+'</p><button id="fgX" class="mt-4 rounded-lg border border-zinc-300 px-4 py-2 text-sm dark:border-zinc-800">Close</button></div>';$('fgX').onclick=fgClose;}
 }
 function fgStat(n,label){return '<div class="flex-1 rounded-xl border border-zinc-300 bg-zinc-50/60 px-2 py-3 text-center dark:border-zinc-800 dark:bg-zinc-900/40"><div class="mono text-2xl font-medium tracking-tight">'+(n==null?'—':n)+'</div><div class="mt-0.5 text-[10px] uppercase tracking-wide text-zinc-400">'+label+'</div></div>';}
 function fgReceipt(r){
   let stats=fgStat(r.docs,'documents');
   if(r.nodes_removed!=null)stats+=fgStat(r.nodes_removed,'graph nodes');
   if(r.edges_removed!=null)stats+=fgStat(r.edges_removed,'relationships');
   const proof=r.proof_answer?
     '<div class="mt-5"><div class="mb-2 mono text-[10px] uppercase tracking-wide text-zinc-400">Proof · re-queried just now</div>'+
     '<div class="space-y-2 rounded-xl border border-zinc-300 bg-zinc-50/50 p-3 dark:border-zinc-800 dark:bg-zinc-900/30">'+
     '<div class="flex justify-end"><div class="max-w-[85%] rounded-2xl rounded-br-md bg-zinc-950 px-3.5 py-2 text-sm text-white dark:bg-white dark:text-black">'+esc(r.proof_query)+'</div></div>'+
     '<div class="flex justify-start"><div class="max-w-[90%] rounded-2xl rounded-bl-md border border-zinc-300 bg-white px-3.5 py-2 text-sm leading-relaxed dark:border-zinc-800 dark:bg-[#1a2233]">'+esc(r.proof_answer)+'</div></div></div></div>':'';
   // C03 retrieval-layer proof: forgotten docs' own chunks are gone from the vector index, not just rephrased.
   const retr=(r.retrieval_chunks_remaining!=null)?
     '<div class="mt-4 flex items-center gap-2 rounded-xl border border-emerald-500/40 bg-emerald-500/[0.04] px-3 py-2"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" class="shrink-0 text-emerald-500"><path d="M20 6 9 17l-5-5"/></svg><span class="text-xs text-zinc-600 dark:text-zinc-300">Retrieval-layer proof: <span class="mono font-medium">'+r.retrieval_chunks_remaining+'</span> of its chunks remain in the vector index'+(r.retrieval_chunks_remaining===0?' &mdash; gone from the index, not just rephrased':'')+'</span></div>':'';
   // Certificate of Erasure — a verifiable, exportable record of this deletion (opens a standalone print-ready page).
   const cert=r.event_id?'<button id="fgCert" class="inline-flex items-center gap-1.5 rounded-lg border border-zinc-300 px-3 py-2 text-sm font-medium text-zinc-700 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-200 dark:hover:bg-zinc-900"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>Download Certificate</button>':'';
   // Re-ask the incident question in a fresh chat — the money shot: same question, now a different answer.
   const reask='<button id="fgReask" class="mt-5 flex w-full items-center justify-center gap-2 rounded-full bg-cyan-600 px-4 py-2.5 text-sm font-medium text-white transition hover:bg-cyan-500"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9 9 0 0 0-6.4 2.6L3 8"/><path d="M3 3v5h5"/></svg>Ask the same question again</button>';
   $('fgBody').innerHTML=
     '<div class="text-center"><div class="mx-auto mb-3 grid h-12 w-12 place-items-center rounded-full border border-cyan-500/30 bg-cyan-500/5 text-cyan-500"><svg width="22" height="20" viewBox="0 0 26 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M2 12c2.2-4 4.4-4 6.6 0s4.4 4 6.6 0"/><path d="M15.2 12c2.2-4 4.4-4 6.6 0" opacity=".4"/></svg></div>'+
     '<h3 class="serif text-3xl tracking-tight">Forgotten.</h3>'+
     '<p class="mt-1 text-sm text-zinc-500 dark:text-zinc-400"><span class="mono text-zinc-700 dark:text-zinc-200">'+esc(r.system)+'</span> is gone — across the graph and the vectors.</p></div>'+
     '<div class="mt-5 flex gap-2">'+stats+'</div>'+proof+retr+reask+
     '<div class="mt-4 flex items-center justify-between gap-2"><span class="inline-flex items-center gap-1.5 text-[11px] font-medium text-cyan-600 dark:text-cyan-400"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Verifiable hard-delete</span>'+
     '<div class="flex items-center gap-2">'+cert+
     '<button id="fgDone" class="rounded-lg border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-700 dark:text-zinc-300 dark:hover:bg-zinc-900">Done</button></div></div>';
   $('fgDone').onclick=fgClose;
   const rb=$('fgReask');if(rb)rb.onclick=()=>{fgClose();newChat();show('triage');ask('If auth-service latency is high, what should I check?');};
   const cb=$('fgCert');if(cb)cb.onclick=()=>window.open('/certificate/'+encodeURIComponent(r._ws||activeWs)+'/'+encodeURIComponent(r.event_id),'_blank');
 }
 $('fgModal').addEventListener('click',e=>{if(e.target===$('fgModal'))fgClose();});

 // graph — Obsidian-style: degree-sized nodes, hover a node to highlight its neighbours and dim the rest,
 // organic force-directed layout, relationship labels that reveal only on focus (clean by default).
 let gnet=null,gdata=null,gpinned=null,gpdepth=1,gRaf=null,gGen=0;
 const gReduced=!!(window.matchMedia&&window.matchMedia('(prefers-reduced-motion:reduce)').matches);
 function loadVis(cb){if(window.vis)return cb();const s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js';s.onload=cb;s.onerror=()=>{$('gmeta').textContent='could not load the graph library';};document.head.appendChild(s);}
 function gpal(){const dark=document.documentElement.classList.contains('dark');return dark?{
     ent:{background:'#22d3ee',border:'#0891b2'},typ:{background:'#818cf8',border:'#6366f1'},dem:{background:'#f59e0b',border:'#d97706'},
     dimNode:{background:'rgba(100,116,139,.16)',border:'rgba(100,116,139,.22)'},
     font:'#e2e8f0',fontDim:'rgba(148,163,184,.30)',halo:'#0a0e1a',
     edge:'rgba(100,116,139,.42)',edgeDim:'rgba(100,116,139,.09)',edgeHi:'#22d3ee',efont:'#cbd5e1',glow:'rgba(34,211,238,.85)'
   }:{
     ent:{background:'#0891b2',border:'#0e7490'},typ:{background:'#6366f1',border:'#4f46e5'},dem:{background:'#d97706',border:'#b45309'},
     dimNode:{background:'rgba(148,163,184,.22)',border:'rgba(148,163,184,.28)'},
     font:'#0f172a',fontDim:'rgba(100,116,139,.40)',halo:'#f4f7fc',
     edge:'rgba(148,163,184,.70)',edgeDim:'rgba(148,163,184,.22)',edgeHi:'#0891b2',efont:'#334155',glow:'rgba(8,145,178,.55)'
   };}
 function gbase(n,p){return n._demoted?{background:p.dem.background,border:p.dem.border}:(n.type==='Entity'?{background:p.ent.background,border:p.ent.border}:{background:p.typ.background,border:p.typ.border});}
 // animate node opacities toward targets (~170ms easeOutCubic) — the smooth Obsidian focus-pull; snaps on reduced-motion.
 function gAnimate(nop){
   if(gRaf){cancelAnimationFrame(gRaf);gRaf=null;}
   const gen=++gGen,cl=v=>v<0?0:(v>1?1:v);
   const ncur={};gdata.kn.forEach(n=>{const it=gdata.nodesDS.get(n.id);ncur[n.id]=cl((it&&typeof it.opacity==='number')?it.opacity:1);});
   const apply=f=>gdata.nodesDS.update(gdata.kn.map(n=>{const t=nop[n.id]==null?1:nop[n.id];return {id:n.id,opacity:cl(ncur[n.id]+(t-ncur[n.id])*f)};}));
   if(gReduced){apply(1);return;}
   const dur=170,t0=performance.now();
   function step(now){if(gen!==gGen)return;const k=Math.min(1,Math.max(0,(now-t0)/dur));apply(1-Math.pow(1-k,3));if(k<1)gRaf=requestAnimationFrame(step);else gRaf=null;}
   gRaf=requestAnimationFrame(step);
 }
 function gReset(){if(!gnet||!gdata)return;const p=gpal();
   gdata.nodesDS.update(gdata.kn.map(n=>({id:n.id,color:gbase(n,p),font:{color:p.font},shadow:{enabled:false}})));
   gdata.edgesDS.update(gdata.ke.map(e=>({id:e._id,color:{color:p.edge,opacity:1},width:1.1,label:'',font:{color:p.efont}})));
   const nop={};gdata.kn.forEach(n=>nop[n.id]=1);gAnimate(nop);
 }
 function gFocus(id){if(!gnet||!gdata)return;const p=gpal();
   const nb=new Set([id]);gdata.ke.forEach(e=>{if(e.source===id)nb.add(e.target);if(e.target===id)nb.add(e.source);});
   gdata.nodesDS.update(gdata.kn.map(n=>{
     if(n.id===id)return{id:n.id,color:gbase(n,p),font:{color:p.font},shadow:{enabled:true,color:p.glow,size:30,x:0,y:0}};
     if(nb.has(n.id))return{id:n.id,color:gbase(n,p),font:{color:p.font},shadow:{enabled:false}};
     return{id:n.id,color:gbase(n,p),font:{color:p.fontDim},shadow:{enabled:false}};
   }));
   gdata.edgesDS.update(gdata.ke.map(e=>{const on=e.source===id||e.target===id;return on
     ?{id:e._id,color:{color:p.edgeHi,opacity:1},width:2.4,label:e.label,font:{color:p.efont}}
     :{id:e._id,color:{color:p.edgeDim,opacity:1},width:.7,label:'',font:{color:p.efont}};}));
   const nop={};gdata.kn.forEach(n=>nop[n.id]=(n.id===id||nb.has(n.id))?1:.13);gAnimate(nop);
 }
 // blast radius — BFS hop distances, then highlight outward N hops (incident "what depends on this").
 function gHops(id,depth){const dist={};dist[id]=0;let fr=[id];
   for(let d=1;d<=depth;d++){const nx=[];fr.forEach(u=>{gdata.ke.forEach(e=>{const v=e.source===u?e.target:(e.target===u?e.source:null);if(v!=null&&dist[v]===undefined){dist[v]=d;nx.push(v);}});});fr=nx;}
   return dist;}
 function gFocusHops(id,depth){if(!gnet||!gdata)return;const p=gpal();gpdepth=depth;
   const dist=gHops(id,depth),opAt=h=>h<=1?1:(h===2?.5:.32);
   gdata.nodesDS.update(gdata.kn.map(n=>{const h=dist[n.id];
     if(h===undefined)return{id:n.id,color:gbase(n,p),font:{color:p.fontDim},shadow:{enabled:false}};
     return{id:n.id,color:gbase(n,p),font:{color:h<=1?p.font:p.fontDim},shadow:n.id===id?{enabled:true,color:p.glow,size:30,x:0,y:0}:{enabled:false}};}));
   gdata.edgesDS.update(gdata.ke.map(e=>{const a=dist[e.source],b=dist[e.target],inset=a!==undefined&&b!==undefined,far=inset?Math.max(a,b):99;
     return inset?{id:e._id,color:{color:p.edgeHi,opacity:1},width:Math.max(.8,2.4-far*0.55),label:far<=1?e.label:'',font:{color:p.efont}}
       :{id:e._id,color:{color:p.edgeDim,opacity:1},width:.7,label:'',font:{color:p.efont}};}));
   const nop={};gdata.kn.forEach(n=>{const h=dist[n.id];nop[n.id]=h===undefined?.10:opAt(h);});gAnimate(nop);}
 function gMeta(id){const el=$('gmeta');if(!gdata)return;
   if(id==null){el.textContent=gdata.kn.length+' systems & concepts · '+gdata.ke.length+' relationships  ·  hover a node to trace its connections';return;}
   const n=gdata.byId[id],deg=gdata.deg[id]||0;
   el.innerHTML='<b class="text-zinc-600 dark:text-zinc-300">'+esc(n?n.label:'node')+'</b> · '+deg+' connection'+(deg===1?'':'s')+'  ·  click empty space to reset';
 }
 // Node detail panel — read a node's domain connections in plain text (the canvas edge labels are tiny).
 function gPanelHide(){const el=$('gpanel');if(el){el.classList.add('hidden');el.innerHTML='';}}
 function gPanelShow(id){const el=$('gpanel');if(!el||!gdata)return;const n=gdata.byId[id];if(!n){gPanelHide();return;}
   const types=[],rels=[];
   gdata.ke.forEach(e=>{
     if(e.source===id){const o=gdata.byId[e.target];if(!o)return;
       if(n.type==='Entity'&&o.type==='EntityType')types.push(o.label);else rels.push({dir:'→',rel:e.label,node:o.label});}
     else if(e.target===id){const o=gdata.byId[e.source];if(!o)return;rels.push({dir:'←',rel:e.label,node:o.label});}
   });
   const isEnt=n.type==='Entity';
   let h='<div class="flex items-start justify-between gap-2"><div class="text-sm font-semibold text-zinc-900 dark:text-zinc-100">'+esc(n.label)+'</div><button id="gpclose" aria-label="Close" class="-mr-1 -mt-1 shrink-0 text-zinc-400 transition hover:text-zinc-600 dark:hover:text-zinc-200"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>';
   if(isEnt&&types.length)h+='<div class="mt-1.5 flex flex-wrap gap-1">'+types.map(t=>'<span class="rounded-full bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700 dark:bg-indigo-950/40 dark:text-indigo-300">'+esc(t)+'</span>').join('')+'</div>';
   else h+='<div class="mt-1 text-[11px] uppercase tracking-wide text-zinc-400">'+(isEnt?'entity':'type')+'</div>';
   if(rels.length)h+='<div class="mt-3 text-[11px] uppercase tracking-wide text-zinc-400">connections</div><div class="mt-1.5 space-y-1.5">'+rels.map(r=>'<div class="flex items-baseline gap-1.5 text-xs"><span class="text-cyan-500">'+r.dir+'</span><span class="text-zinc-500 dark:text-zinc-400">'+esc(r.rel)+'</span><span class="font-medium text-zinc-800 dark:text-zinc-200">'+esc(r.node)+'</span></div>').join('')+'</div>';
   else h+='<div class="mt-3 text-xs text-zinc-400">No connections in the graph.</div>';
   if(rels.length||types.length){h+='<div class="mt-3 text-[11px] uppercase tracking-wide text-zinc-400">blast radius</div>';
     h+='<div class="mt-1.5 flex gap-1">'+[1,2,3].map(d=>'<button data-d="'+d+'" class="gpradb flex-1 rounded-md border px-2 py-1 text-[11px] font-medium transition '+(d===gpdepth?'border-cyan-500/60 bg-cyan-500/10 text-cyan-600 dark:text-cyan-300':'border-zinc-200 text-zinc-500 hover:border-cyan-500/50 dark:border-zinc-700 dark:text-zinc-400')+'">'+d+(d>1?' hops':' hop')+'</button>').join('')+'</div>';}
   h+='<button id="gpask" class="mt-3 w-full rounded-lg border border-cyan-500/40 px-3 py-1.5 text-xs font-medium text-cyan-600 transition hover:bg-cyan-500/10 dark:text-cyan-400">Ask about this →</button>';
   el.innerHTML=h;el.classList.remove('hidden');el.classList.remove('mscale');void el.offsetWidth;el.classList.add('mscale');
   $('gpclose').onclick=()=>{gPanelHide();gpinned=null;gpdepth=1;gReset();gMeta(null);};
   $('gpask').onclick=()=>askAbout(n.label);
   [...el.querySelectorAll('.gpradb')].forEach(b=>b.onclick=()=>{gFocusHops(id,+b.dataset.d);gPanelShow(id);});
 }
 function loadGraph(){loadVis(async()=>{
   $('gmeta').textContent='loading…';gpinned=null;
   {const gs=$('gskel');if(gs)gs.classList.add('hidden');}  // physics settles IN VIEW — the live wobble IS the show (CEO: never hide/freeze it)
   try{const d=await(await fetch('/graph?workspace='+encodeURIComponent(activeWs))).json();
     if(d.error){$('gmeta').textContent='error: '+d.error;return;}
     const p=gpal();
     const keep={Entity:1,EntityType:1};
     const kn=d.nodes.filter(n=>keep[n.type]);
     // M2d: tint demoted systems amber (client-side; fail-safe — reuses the demote state from /systems)
     let demCount=0;
     try{const sj=await(await fetch('/systems?workspace='+encodeURIComponent(activeWs))).json();
       const gnorm=v=>String(v||'').toLowerCase().replace(/[\\s_]+/g,'-');
       const demSet=new Set((sj.systems||[]).filter(s=>s.demoted).map(s=>gnorm(s.name)));
       if(demSet.size)kn.forEach(n=>{if(demSet.has(gnorm(n.label))){n._demoted=true;demCount++;}});
     }catch(_){}
     {const gl=$('glegDem');if(gl)gl.classList.toggle('hidden',demCount===0);}
     const ids=new Set(kn.map(n=>n.id));
     const ke=d.edges.filter(e=>ids.has(e.source)&&ids.has(e.target)).map((e,i)=>({source:e.source,target:e.target,label:e.label,_id:i}));
     const deg={};ke.forEach(e=>{deg[e.source]=(deg[e.source]||0)+1;deg[e.target]=(deg[e.target]||0)+1;});
     const byId={};kn.forEach(n=>byId[n.id]=n);
     const nodesDS=new vis.DataSet(kn.map(n=>({id:n.id,label:n.label,shape:'dot',value:Math.sqrt(deg[n.id]||0)+1.5,color:gbase(n,p),
       font:{color:p.font,strokeWidth:4,strokeColor:p.halo}})));
     const edgesDS=new vis.DataSet(ke.map(e=>({id:e._id,from:e.source,to:e.target,label:'',color:{color:p.edge,opacity:1},width:1.1,font:{color:p.efont,strokeWidth:3,strokeColor:p.halo}})));
     gdata={kn,ke,deg,byId,nodesDS,edgesDS};
     const opts={
       nodes:{borderWidth:2,scaling:{min:7,max:26,label:{enabled:true,min:12,max:22}}},
       edges:{smooth:{type:'continuous',roundness:.4},arrows:{to:{enabled:true,scaleFactor:.4}},font:{size:11,align:'middle'},selectionWidth:0,hoverWidth:0},
       physics:{solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-70,centralGravity:.012,springLength:130,springConstant:.07,damping:.45,avoidOverlap:.55},stabilization:{iterations:300,fit:true},minVelocity:.75},
       interaction:{hover:true,hoverConnectedEdges:false,selectConnectedEdges:false,zoomView:true,dragView:true}};
     gnet=new vis.Network($('graphbox'),{nodes:nodesDS,edges:edgesDS},opts);window.gnet=gnet;
     const box=$('graphbox');
     gnet.on('hoverNode',pr=>{if(gpinned==null)gFocus(pr.node);box.style.cursor='pointer';});
     gnet.on('blurNode',()=>{if(gpinned==null)gReset();box.style.cursor='default';});
     const gk=$('gask');
     gnet.on('click',pr=>{if(pr.nodes&&pr.nodes.length){gpinned=pr.nodes[0];gpdepth=1;gFocusHops(gpinned,1);gMeta(gpinned);gPanelShow(gpinned);if(gk)gk.classList.add('hidden');gnet.focus(gpinned,{scale:gnet.getScale(),locked:false,animation:{duration:500,easingFunction:'easeInOutCubic'}});}else{gpinned=null;gpdepth=1;gReset();gMeta(null);gPanelHide();gnet.fit({animation:{duration:600,easingFunction:'easeInOutQuad'}});}});
     gnet.on('doubleClick',pr=>{if(pr.nodes&&pr.nodes.length){const n=gdata.byId[pr.nodes[0]];if(n)askAbout(n.label);}});
     gnet.once('stabilizationIterationsDone',()=>gnet.fit({animation:{duration:600,easingFunction:'easeInOutQuad'}}));
     window.gFocus=gFocus;window.gReset=gReset;window.gFocusHops=gFocusHops;
     gMeta(null);
   }catch(e){$('gmeta').textContent='failed to load graph';}
 });}
 $('greload').onclick=loadGraph;
 // S2: search a node by label (hyphen/space-normalized), Enter → focus + halo; Esc/clear → reset.
 function gSearch(){if(!gnet||!gdata)return;var raw=($('gsearch').value||'').trim();
   if(!raw){gpinned=null;gReset();gMeta(null);gPanelHide();return;}
   var q=raw.toLowerCase().replace(/[\\s_]+/g,'-'),norm=function(n){return String(n.label||'').toLowerCase().replace(/[\\s_]+/g,'-');};
   var hit=gdata.kn.find(function(n){return norm(n)===q;})||gdata.kn.find(function(n){return norm(n).indexOf(q)>=0;});
   if(!hit){gMeta(null);return;}
   gpinned=hit.id;gFocus(hit.id);gMeta(hit.id);gnet.focus(hit.id,{scale:1.15,animation:{duration:600,easingFunction:'easeInOutCubic'}});}
 (function(){var gi=$('gsearch');if(gi)gi.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();gSearch();}else if(e.key==='Escape'){gi.value='';gpinned=null;if(gnet&&gdata){gReset();gMeta(null);}}});})();

 // upload
 const drop=$('drop');let pending='';
 $('file').addEventListener('change',e=>readFiles(e.target.files));
 drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('drop-hot');});
 drop.addEventListener('dragleave',()=>drop.classList.remove('drop-hot'));
 drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drop-hot');readFiles(e.dataTransfer.files);});
 function readFiles(files){if(!files||!files.length)return;const f=files[0];const rd=new FileReader();
   rd.onload=()=>{pending=rd.result;if(!$('upsys').value)$('upsys').value=f.name.replace(/\\.[^.]+$/,'');$('upmsg').innerHTML='<span class="text-zinc-500">loaded <b>'+esc(f.name)+'</b> ('+f.size+' bytes) — name it and click Ingest.</span>';};
   rd.readAsText(f);}
 $('bUp').onclick=async()=>{
   const text=(pending||$('paste').value).trim();const system=($('upsys').value||'uploaded').trim();
   if(!text){$('upmsg').innerHTML='<span class="text-red-500">nothing to ingest — drop a file or paste text.</span>';return;}
   $('bUp').disabled=true;$('upmsg').innerHTML='<div class="text-zinc-500">ingesting <b>'+esc(system)+'</b> — building the graph<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span> (this can take ~1 min)</div><div class="skel mt-2 h-1.5 w-full"></div>';
   const upWs=activeWs;
   try{const up=await(await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text,system:system,workspace:activeWs})})).json();
     if(up.state==='error'||up.state==='busy'||up.error){$('bUp').disabled=false;$('upmsg').innerHTML='<span class="text-red-500">'+esc(up.error||'could not ingest')+'</span>';return;}
     const deadline=Date.now()+90000;  // stop polling after ~90s instead of hanging forever on a stuck ingest
     const t=setInterval(async()=>{
       if(Date.now()>deadline){clearInterval(t);$('bUp').disabled=false;$('upmsg').innerHTML='<span class="text-red-500">ingest is taking longer than expected — check Systems in a moment.</span>';return;}
       const st=await(await fetch('/ingest-status')).json();
       if(st.wid&&st.wid!==upWs)return;
       if(st.state==='done'){clearInterval(t);$('bUp').disabled=false;pending='';$('paste').value='';$('upmsg').innerHTML='<span class="text-emerald-600 dark:text-emerald-400">✓ ingested <b>'+esc(st.system)+'</b> — now searchable. Check Systems.</span>';}
       else if(st.state==='error'){clearInterval(t);$('bUp').disabled=false;$('upmsg').innerHTML='<span class="text-red-500">ingest failed: '+esc(st.error||'')+'</span>';}
     },2000);
   }catch(e){$('bUp').disabled=false;$('upmsg').innerHTML='<span class="text-red-500">upload failed</span>';}
 };

 // Settings — bring your own LLM provider + API key, applied at runtime (no restart)
 (function(){
   const m=$('setModal');if(!m)return;
   const PRESETS={
     openai:{model:'gpt-4o-mini',endpoint:'',key:true},
     anthropic:{model:'claude-3-5-haiku-latest',endpoint:'',key:true},
     gemini:{model:'openai/gemini-2.5-flash',endpoint:'https://generativelanguage.googleapis.com/v1beta/openai/',key:true},
     openrouter:{model:'openrouter/meta-llama/llama-3.3-70b-instruct',endpoint:'https://openrouter.ai/api/v1',key:true},
     groq:{model:'llama-3.3-70b-versatile',endpoint:'https://api.groq.com/openai/v1',key:true},
     lmstudio:{model:'openai/your-model-id',endpoint:'http://localhost:1234/v1',key:false},
     ollama:{model:'qwen2.5',endpoint:'http://localhost:11434',key:false},
     custom:{model:'',endpoint:'',key:true}
   };
   const PROVIDER_MAP={openai:'openai',anthropic:'anthropic',gemini:'custom',openrouter:'custom',groq:'custom',lmstudio:'custom',ollama:'ollama',custom:'custom'};
   // The backend collapses gemini/groq/openrouter/lmstudio to provider 'custom' — map a running config back to a dropdown value by endpoint.
   function detect(cfg){const ep=(cfg.endpoint||'').toLowerCase();const pv=(cfg.provider||'').toLowerCase();
     if(ep.indexOf('generativelanguage.googleapis')>=0)return'gemini';
     if(ep.indexOf('api.groq.com')>=0)return'groq';
     if(ep.indexOf('openrouter.ai')>=0)return'openrouter';
     if(ep.indexOf(':1234')>=0)return'lmstudio';
     if(pv==='ollama'||ep.indexOf(':11434')>=0)return'ollama';
     if(pv==='anthropic')return'anthropic';
     if(pv==='openai'&&!ep)return'openai';
     return'custom';}
   function fill(p){const d=PRESETS[p]||PRESETS.custom;$('setModelInput').value=d.model;$('setEndpoint').value=d.endpoint;$('setKey').placeholder=d.key?'sk-…':'(no key needed)';}
   $('setProvider').onchange=()=>fill($('setProvider').value);
   async function open(){$('setMsg').textContent='';$('setKey').value='';m.classList.remove('hidden');m.classList.add('flex');
     fill('groq');$('setProvider').value='groq';  // safe fallback (never default to OpenAI, which we don't demo)
     try{const cfg=await(await fetch('/llm-config')).json();const p=detect(cfg);$('setProvider').value=p;fill(p);
       if(cfg.model)$('setModelInput').value=cfg.model;if(cfg.endpoint)$('setEndpoint').value=cfg.endpoint;}catch(e){}
     renderSec();
     setTimeout(()=>$('setProvider').focus(),40);}
   // M5: surface the EXISTING single-tenant auth — read-only /health probe + client-token controls (no backend change).
   async function renderSec(){const ss=$('secState'),row=$('secTokenRow');if(!ss)return;
     let auth=false;try{const h=await(await fetch('/health')).json();auth=!!h.auth;}catch(e){}
     const hasTok=!!localStorage.getItem('lethe.token');
     if(auth)ss.innerHTML='<span class="font-medium text-cyan-700 dark:text-cyan-300">Protected</span> — this instance requires an access token. '+(hasTok?'A token is saved in this browser.':'No token saved yet — you will be prompted on the next API call.');
     else ss.innerHTML='<span class="font-medium text-zinc-700 dark:text-zinc-200">Open (local mode)</span> — no token required.'+(hasTok?' A leftover client token is saved.':'');
     if(row)row.classList.toggle('hidden',!(auth||hasTok));
     const sc=$('secClear');if(sc)sc.classList.toggle('hidden',!hasTok);}
   {const st=$('secSet');if(st)st.onclick=()=>{const v=($('secToken').value||'').trim();if(v){localStorage.setItem('lethe.token',v);$('secToken').value='';renderSec();}};}
   {const sc=$('secClear');if(sc)sc.onclick=()=>{localStorage.removeItem('lethe.token');renderSec();};}
   function close(){m.classList.add('hidden');m.classList.remove('flex');}
   $('setBtn').onclick=open;$('setCancel').onclick=close;
   m.addEventListener('click',e=>{if(e.target===m)close();});
   $('setSave').onclick=async()=>{
     const p=$('setProvider').value;
     const keyVal=$('setKey').value.trim()||(p==='lmstudio'?'lm-studio':'');  // LM Studio ignores the key but litellm wants a non-empty one
     const body={provider:PROVIDER_MAP[p]||'custom',model:$('setModelInput').value.trim(),endpoint:$('setEndpoint').value.trim(),api_key:keyVal};
     $('setMsg').innerHTML='<span class="text-zinc-500">applying &amp; testing<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span>';$('setSave').disabled=true;
     try{const j=await(await fetch('/llm-config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
       if(j.ok){$('setMsg').innerHTML='<span class="text-emerald-600 dark:text-emerald-400">Saved — now answering with '+esc(j.llm.label)+'.</span>';poll();setTimeout(close,950);}
       else{$('setMsg').innerHTML='<span class="text-red-500">'+esc(j.error||'could not apply')+'</span>';}
     }catch(e){$('setMsg').innerHTML='<span class="text-red-500">request failed</span>';}
     $('setSave').disabled=false;
   };
   $('setReset').onclick=async()=>{$('setMsg').innerHTML='<span class="text-zinc-500">resetting…</span>';
     try{await(await fetch('/llm-config/reset',{method:'POST'})).json();$('setMsg').innerHTML='<span class="text-emerald-600 dark:text-emerald-400">Back to the default model.</span>';$('setKey').value='';poll();}
     catch(e){$('setMsg').innerHTML='<span class="text-red-500">reset failed</span>';}
   };
   fill('groq');  // harmless initial state; open() re-fills from the active config
 })();

 // Add system — name + content -> ingest into the active workspace (reuses /upload). Re-adding a
 // decommissioned name just brings it back (no soft-undo; you provide fresh content).
 (function(){
   const m=$('addModal');if(!m)return;
   function open(){$('addName').value='';$('addText').value='';$('addMsg').textContent='';m.classList.remove('hidden');m.classList.add('flex');setTimeout(()=>$('addName').focus(),40);}
   function close(){m.classList.add('hidden');m.classList.remove('flex');}
   const ab=$('addSysBtn');if(ab)ab.onclick=open;
   $('addCancel').onclick=close;m.addEventListener('click',e=>{if(e.target===m)close();});
   $('addOk').onclick=async()=>{
     const name=($('addName').value||'').trim(),text=($('addText').value||'').trim();
     if(!name||!text){$('addMsg').innerHTML='<span class="text-red-500">Add a name and some content.</span>';return;}
     $('addOk').disabled=true;$('addMsg').innerHTML='<span class="text-zinc-500">building the graph<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span> (~30s)</span>';
     const upWs=activeWs;
     try{const r=await(await fetch('/upload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:text,system:name,workspace:activeWs})})).json();
       if(r.state==='error'||r.state==='busy'||r.error){$('addOk').disabled=false;$('addMsg').innerHTML='<span class="text-red-500">'+esc(r.error||'could not add')+'</span>';return;}
       const deadline=Date.now()+90000;  // stop polling after ~90s instead of hanging forever on a stuck ingest
       const t=setInterval(async()=>{
         if(Date.now()>deadline){clearInterval(t);$('addOk').disabled=false;$('addMsg').innerHTML='<span class="text-red-500">ingest is taking longer than expected — check Systems in a moment.</span>';return;}
         const st=await(await fetch('/ingest-status')).json();
         if(st.wid&&st.wid!==upWs)return;
         if(st.state==='done'){clearInterval(t);$('addOk').disabled=false;$('addMsg').innerHTML='<span class="text-emerald-600 dark:text-emerald-400">Added — '+esc(st.system)+' is in the graph.</span>';if(curView==='systems')loadSystems();setTimeout(close,1100);}
         else if(st.state==='error'){clearInterval(t);$('addOk').disabled=false;$('addMsg').innerHTML='<span class="text-red-500">failed: '+esc(st.error||'')+'</span>';}
       },2000);
     }catch(e){$('addOk').disabled=false;$('addMsg').innerHTML='<span class="text-red-500">request failed</span>';}
   };
 })();
</script>
</body></html>"""


LANDING = """<!doctype html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lethe — on-call memory that forgets, and proves it</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 26 24' fill='none' stroke='%2322d3ee' stroke-width='2.4' stroke-linecap='round'><path d='M2 12c2.2-4 4.4-4 6.6 0s4.4 4 6.6 0'/><path d='M15.2 12c2.2-4 4.4-4 6.6 0' opacity='.4'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
<style type="text/tailwindcss">
  @custom-variant dark (&:where(.dark, .dark *));
  /* Cool blue-tinted neutral palette — same as the app, so landing + app read as one cool blue-gray product. */
  @theme {
    --color-zinc-50:#f5f7fa; --color-zinc-100:#eef1f6; --color-zinc-200:#e0e5ee; --color-zinc-300:#cbd3e1;
    --color-zinc-400:#93a0b5; --color-zinc-500:#64718a; --color-zinc-600:#475066; --color-zinc-700:#353d50;
    --color-zinc-800:#222838; --color-zinc-900:#161b28; --color-zinc-950:#0d1119;
    --color-slate-100:#eef1f6; --color-slate-200:#e0e5ee; --color-slate-300:#cbd3e1; --color-slate-400:#93a0b5;
    --color-slate-500:#64718a; --color-slate-600:#475066; --color-slate-700:#353d50; --color-slate-800:#222838;
    --color-slate-900:#141a27;
  }
  body{font-family:'Geist',system-ui,sans-serif}
  html{scroll-behavior:smooth;font-size:19px}
  .serif{font-family:'Instrument Serif','Spectral',Georgia,serif;font-weight:400}
  @keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .lx{animation:fadein .35s ease both}
  #demo{transition:opacity .45s ease}
  .mono{font-family:'Geist Mono',ui-monospace,monospace}
  .lc-rail{position:absolute;height:2px;border-radius:2px;background:currentColor}
  .lc-fill{position:absolute;height:2px;width:0;border-radius:2px;background:linear-gradient(90deg,#22d3ee,#6366f1);box-shadow:0 0 8px rgba(34,211,238,.5);transition:width .1s linear}
  a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid #22d3ee;outline-offset:2px;border-radius:5px}
  .lc-dot{background:#cbd5e1;transition:background .3s ease,box-shadow .3s ease}
  .dark .lc-dot{background:#334155}
  .lc-stage.lit .lc-dot{background:#22d3ee;box-shadow:0 0 0 4px rgba(34,211,238,.15)}
  .lc-dot-hero{position:relative}
  .lc-dot-hero::after{content:"";position:absolute;inset:0;border-radius:9999px;background:#22d3ee;animation:lcping 2.2s ease-out infinite}
  @keyframes lcping{0%{transform:scale(1);opacity:.55}70%,100%{transform:scale(2.4);opacity:0}}
  @media (prefers-reduced-motion:reduce){.lc-dot-hero::after{animation:none}.lethe-mark{animation:none}.wv-f,.wv-b{animation:none}}
  .wv{position:absolute;top:0;left:0;height:100%;width:200%}
  .wv-f{animation:wflow 16s linear infinite}
  .wv-b{animation:wflow 26s linear infinite}
  @keyframes wflow{to{transform:translateX(-50%)}}
  @keyframes lshimmer{0%,100%{filter:hue-rotate(0deg)}50%{filter:hue-rotate(-22deg)}}
  .card{position:relative;background-image:radial-gradient(240px circle at var(--mx,-200px) var(--my,-200px), rgba(34,211,238,.16), transparent 60%);background-color:rgba(255,255,255,.78)}
  .dark .card{background-color:rgba(14,20,32,.66)}
  .reveal{opacity:0;transform:translateY(22px);transition:opacity .8s cubic-bezier(.22,1,.36,1),transform .8s cubic-bezier(.22,1,.36,1)}
  .reveal.in{opacity:1;transform:none}
  /* staggered scroll-reveal: a container's children cascade in one by one (--i set per child in JS) */
  .stg>*{opacity:0;transform:translateY(24px);transition:opacity .6s cubic-bezier(.22,1,.36,1),transform .6s cubic-bezier(.22,1,.36,1),box-shadow .35s ease,border-color .35s ease}
  .stg.in>*{opacity:1;transform:none;transition-delay:calc(var(--i,0)*80ms)}
  /* tactile hover-lift on cards (rounded-2xl/3xl only, so flow arrows are excluded) — elevation via shadow+border, no transform conflict with the reveal */
  .stg.in>.rounded-2xl:hover,.stg.in>.rounded-3xl:hover{transition-delay:0s;box-shadow:0 22px 55px -26px rgba(2,8,20,.6);border-color:rgba(34,211,238,.5)}
  /* benchmark proof bars grow in on reveal */
  .evbar-fill{width:0;transition:width 1.05s cubic-bezier(.22,1,.36,1) .15s}
  @media (prefers-reduced-motion:reduce){.reveal,.stg>*{opacity:1!important;transform:none!important;transition:none}.evbar-fill{transition:none}}
  /* signature scroll moment — pinned scrollytelling: the forget hero plays out as you scroll */
  .scrolly{height:330vh}
  .scrolly-sticky{position:sticky;top:0;display:flex;min-height:100vh;align-items:center;padding:4rem 0}
  .scrolly.noscroll{height:auto}
  .scrolly.noscroll .scrolly-sticky{position:static;min-height:0}
  .ss-el{opacity:0;transform:translateY(12px);transition:opacity .55s cubic-bezier(.22,1,.36,1),transform .55s cubic-bezier(.22,1,.36,1)}
  .ss-el.show{opacity:1;transform:none}
  .ss-diss{display:inline-block;transition:opacity .6s ease,transform .6s ease,filter .6s ease}
  .ss-diss.gone{opacity:0;transform:translateY(8px) scale(.82);filter:blur(5px)}
  #srail [data-s]{transition:color .35s ease,opacity .35s ease;opacity:.55}
  #srail [data-s].on{color:#22d3ee;opacity:1}
  .srail-line{flex:1;height:1px;margin:0 10px;background:currentColor;opacity:.2}
  #ssQ.reask>div{box-shadow:0 0 0 2px rgba(34,211,238,.55);transition:box-shadow .35s ease}
  @media (prefers-reduced-motion:reduce){.ss-el,.ss-diss{transition:none}}
  .grad{background:linear-gradient(90deg,#0891b2,#4f46e5);-webkit-background-clip:text;background-clip:text;color:transparent;-webkit-text-fill-color:transparent}
  .dark .grad{background:linear-gradient(90deg,#22d3ee,#6366f1);-webkit-background-clip:text;background-clip:text}
  .lethe-mark{font-weight:400;line-height:.82;letter-spacing:-.02em;font-size:clamp(5rem,29vw,24rem);margin-bottom:-.06em;background:linear-gradient(180deg,#0891b2 0%,#4f46e5 46%,rgba(79,70,229,0) 88%);-webkit-background-clip:text;background-clip:text;color:transparent;-webkit-text-fill-color:transparent;animation:lshimmer 7s ease-in-out infinite}
  .dark .lethe-mark{background:linear-gradient(180deg,#22d3ee 0%,#6366f1 46%,rgba(99,102,241,0) 88%);-webkit-background-clip:text;background-clip:text}
  .bg-grid{background-image:radial-gradient(rgba(100,116,139,.13) 1px,transparent 1px);background-size:26px 26px}
  .dark .bg-grid{background-image:radial-gradient(rgba(148,163,184,.07) 1px,transparent 1px)}
</style>
<style>
@keyframes rise{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:none}}
.rise{opacity:0;animation:rise .85s cubic-bezier(.22,1,.36,1) forwards}
@media (prefers-reduced-motion:reduce){.rise{opacity:1;animation:none}}
@keyframes wwmarq{to{transform:translateX(-50%)}}
.wwmarq{animation:wwmarq 38s linear infinite}
.wwwrap:hover .wwmarq{animation-play-state:paused}
.wwmask{-webkit-mask-image:linear-gradient(90deg,transparent,#000 11%,#000 89%,transparent);mask-image:linear-gradient(90deg,transparent,#000 11%,#000 89%,transparent)}
@media (prefers-reduced-motion:reduce){.wwmarq{animation:none}}
</style>
</head>
<body class="text-slate-900 antialiased dark:text-slate-100">
<a href="#main" class="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-lg focus:bg-zinc-950 focus:px-4 focus:py-2 focus:text-sm focus:text-white dark:focus:bg-white dark:focus:text-black">Skip to content</a>
<div aria-hidden="true" class="bg-grid fixed inset-0 -z-10 bg-[#f6f8fc] dark:bg-[#0a0e1a]"></div>
<div id="flowWave" aria-hidden="true" class="pointer-events-none fixed inset-x-0 bottom-0 -z-10 h-48 overflow-hidden" style="opacity:.14;-webkit-mask-image:linear-gradient(0deg,#000,#000 40%,transparent);mask-image:linear-gradient(0deg,#000,#000 40%,transparent)">
  <svg class="wv wv-b" viewBox="0 0 1440 90" preserveAspectRatio="none" fill="none"><path d="M0 55 Q120 30 240 55 T480 55 T720 55 T960 55 T1200 55 T1440 55" stroke="#6366f1" stroke-width="2" stroke-opacity=".55" stroke-linecap="round"/></svg>
  <svg class="wv wv-f" viewBox="0 0 1440 90" preserveAspectRatio="none" fill="none"><path d="M0 44 Q120 20 240 44 T480 44 T720 44 T960 44 T1200 44 T1440 44" stroke="#22d3ee" stroke-width="2" stroke-opacity=".6" stroke-linecap="round"/></svg>
</div>

<header class="sticky top-0 z-20 border-b border-zinc-300/70 bg-[#f4f7fc]/80 backdrop-blur dark:border-zinc-900 dark:bg-[#0a0e1a]/75">
  <div class="mx-auto flex max-w-5xl items-center justify-between px-5 py-3.5">
    <div class="flex items-center gap-2">
      <svg class="lethe-x" width="36" height="18" viewBox="0 0 40 20" fill="none" aria-label="Lethe"><style>@keyframes lxL{to{transform:translateX(-20px)}}@keyframes lxR{to{transform:translateX(20px)}}.lethe-x:hover .lxa{animation:lxL 2.2s linear infinite}.lethe-x:hover .lxb{animation:lxR 2.2s linear infinite}@media(prefers-reduced-motion:reduce){.lethe-x:hover .lxa,.lethe-x:hover .lxb{animation:none}}</style><defs><clipPath id="lxClipB"><rect width="40" height="20"/></clipPath></defs><g clip-path="url(#lxClipB)"><path class="lxb" d="M-20 10 Q-15 18 -10 10 T0 10 T10 10 T20 10 T30 10 T40 10 T50 10 T60 10" stroke="#6366f1" stroke-width="2.6" stroke-linecap="round"/><path class="lxa" d="M-20 10 Q-15 2 -10 10 T0 10 T10 10 T20 10 T30 10 T40 10 T50 10 T60 10" stroke="#22d3ee" stroke-width="2.6" stroke-linecap="round"/></g></svg>
      <span class="serif text-xl leading-none tracking-tight">Lethe</span>
    </div>
    <div class="flex items-center gap-2">
      <button id="theme" aria-label="Toggle theme" class="grid h-8 w-8 place-items-center rounded-md border border-zinc-300 text-zinc-600 transition hover:bg-zinc-100 dark:border-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-900">
        <svg class="block dark:hidden" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
        <svg class="hidden dark:block" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/></svg>
      </button>
      <a href="/app" class="rounded-full bg-zinc-950 px-5 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Launch app</a>
    </div>
  </div>
</header>

<main id="main">
<section class="mx-auto max-w-4xl px-5 pt-28 pb-20 text-center">
  <div class="rise mb-6 inline-flex items-center gap-2 rounded-full border border-zinc-300 px-3.5 py-1.5 text-xs text-zinc-500 dark:border-zinc-800 dark:text-zinc-400" style="animation-delay:.05s"><span class="h-1.5 w-1.5 rounded-full bg-cyan-400"></span><span class="mono">remember · recall · <span class="text-cyan-600 dark:text-cyan-400">forget()</span></span> · built on Cognee</div>
  <h1 class="rise serif text-6xl leading-[1.02] tracking-tight sm:text-7xl lg:text-[5.5rem]" style="animation-delay:.12s">On-call memory that<br><span class="grad italic">forgets — and proves&nbsp;it.</span></h1>
  <p class="rise mx-auto mt-6 max-w-xl text-base leading-relaxed text-zinc-500 dark:text-zinc-400" style="animation-delay:.24s">It <span class="font-medium text-zinc-900 dark:text-zinc-100">remembers</span> your runbooks across every session — and <span class="font-medium text-zinc-900 dark:text-zinc-100">forgets</span> the systems you kill, so you never chase a dead one at 3&nbsp;a.m.</p>
  <div class="rise mt-9 flex flex-wrap justify-center gap-3" style="animation-delay:.34s">
    <a href="/app" class="rounded-full bg-zinc-950 px-7 py-3 text-sm font-medium text-white transition duration-200 hover:-translate-y-0.5 hover:bg-zinc-800 hover:shadow-lg hover:shadow-cyan-500/10 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Launch the app →</a>
    <a href="#scrolly" class="rounded-xl border border-zinc-300 px-6 py-3 text-sm font-medium text-zinc-700 transition duration-200 hover:-translate-y-0.5 hover:bg-zinc-100 dark:border-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-900">Watch it forget &rarr;</a>
  </div>

</section>

<section class="mx-auto max-w-4xl px-5 pb-24">
  <div class="reveal overflow-hidden rounded-2xl border border-zinc-300 bg-white/60 shadow-2xl shadow-cyan-500/10 backdrop-blur dark:border-zinc-800 dark:bg-[#0b1018]/60">
    <div class="flex items-center gap-1.5 border-b border-zinc-200/80 px-3.5 py-2.5 dark:border-zinc-800/80">
      <span class="h-2.5 w-2.5 rounded-full bg-red-400/60"></span><span class="h-2.5 w-2.5 rounded-full bg-amber-400/60"></span><span class="h-2.5 w-2.5 rounded-full bg-emerald-400/60"></span>
      <span class="ml-2 mono text-[11px] text-zinc-400">lethe · interactive preview</span>
      <span class="ml-auto hidden mono text-[10px] text-zinc-400 sm:inline">drag a node · hover to trace · click the red one</span>
    </div>
    <div class="relative">
      <div id="lgraph" class="h-[300px] w-full sm:h-[360px]"></div>
      <div id="lgrec" class="pointer-events-none absolute left-1/2 top-3 hidden -translate-x-1/2 items-center gap-x-2.5 rounded-full border border-cyan-500/40 bg-cyan-500/10 px-3.5 py-1.5 mono text-[11px] text-cyan-700 backdrop-blur dark:text-cyan-300"><span class="inline-flex items-center gap-1 font-medium"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Forgotten</span><span class="text-cyan-500/40">·</span><span>2 docs</span><span class="text-cyan-500/40">·</span><span>10 nodes</span><span class="text-cyan-500/40">·</span><span>18 edges</span></div>
    </div>
    <div class="flex flex-wrap items-center justify-between gap-3 border-t border-zinc-200/80 px-3.5 py-3 dark:border-zinc-800/80">
      <span id="lgcap" class="text-xs text-zinc-500 dark:text-zinc-400">An interactive preview of an incident graph — drag a node, or forget the dead one.</span>
      <button id="lgbtn" class="shrink-0 rounded-lg border border-zinc-300 px-3.5 py-1.5 text-xs font-medium text-zinc-700 transition hover:-translate-y-0.5 hover:border-red-300 hover:text-red-600 dark:border-zinc-700 dark:text-zinc-300 dark:hover:border-red-800/70 dark:hover:text-red-400">Decommission legacy-cache</button>
    </div>
  </div>
  <p class="mt-3.5 text-center text-sm text-zinc-500 dark:text-zinc-400">The real thing builds this from your runbooks — <a href="/app" class="underline decoration-dotted underline-offset-2 hover:text-cyan-600 dark:hover:text-cyan-400">open the app</a> for the full graph.</p>
</section>

<section class="mx-auto max-w-3xl px-5 pb-24">
  <div class="rise mx-auto mt-0 flex max-w-3xl flex-col items-center gap-4" style="animation-delay:.44s">
    <span class="mono text-[10px] uppercase tracking-[0.25em] text-zinc-400 dark:text-zinc-600">works with</span>
    <div class="wwwrap wwmask w-full overflow-hidden">
      <div class="wwmarq flex w-max items-center">
        <div id="wwSet" class="flex shrink-0 items-center gap-x-11 pr-11">
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[15px] w-[15px] fill-current" aria-hidden="true"><rect x="1.5" y="9" width="1.7" height="6" rx=".85"/><rect x="4.6" y="6" width="1.7" height="12" rx=".85"/><rect x="7.7" y="3" width="1.7" height="18" rx=".85"/><rect x="10.8" y="8" width="1.7" height="8" rx=".85"/><rect x="13.9" y="1.5" width="1.7" height="21" rx=".85"/><rect x="17" y="7" width="1.7" height="10" rx=".85"/><rect x="20.1" y="4.5" width="1.7" height="15" rx=".85"/></svg><span class="text-[15px] font-semibold lowercase tracking-tight">cognee</span></span>
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[17px] w-[17px] fill-current" aria-hidden="true"><path d="M13.85 0a4.16 4.16 0 0 0-2.95 1.217L1.456 10.66a.835.835 0 0 0 0 1.18.835.835 0 0 0 1.18 0l9.442-9.442a2.49 2.49 0 0 1 3.541 0 2.49 2.49 0 0 1 0 3.541L8.59 12.97l-.1.1a.835.835 0 0 0 0 1.18.835.835 0 0 0 1.18 0l.1-.098 7.03-7.034a2.49 2.49 0 0 1 3.542 0l.049.05a2.49 2.49 0 0 1 0 3.54l-8.54 8.54a1.96 1.96 0 0 0 0 2.755l1.753 1.753a.835.835 0 0 0 1.18 0 .835.835 0 0 0 0-1.18l-1.753-1.753a.266.266 0 0 1 0-.394l8.54-8.54a4.185 4.185 0 0 0 0-5.9l-.05-.05a4.16 4.16 0 0 0-2.95-1.218c-.2 0-.401.02-.6.048a4.17 4.17 0 0 0-1.17-3.552A4.16 4.16 0 0 0 13.85 0m0 3.333a.84.84 0 0 0-.59.245L6.275 10.56a4.186 4.186 0 0 0 0 5.902 4.186 4.186 0 0 0 5.902 0L19.16 9.48a.835.835 0 0 0 0-1.18.835.835 0 0 0-1.18 0l-6.985 6.984a2.49 2.49 0 0 1-3.54 0 2.49 2.49 0 0 1 0-3.54l6.983-6.985a.835.835 0 0 0 0-1.18.84.84 0 0 0-.59-.245"/></svg><span class="text-sm font-medium">MCP</span></span>
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[17px] w-[17px] fill-current" aria-hidden="true"><path d="m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z"/></svg><span class="text-sm font-medium">Claude</span></span>
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[18px] w-[18px] fill-current" aria-hidden="true"><path d="M16.361 10.26a.894.894 0 0 0-.558.47l-.072.148.001.207c0 .193.004.217.059.353.076.193.152.312.291.448.24.238.51.3.872.205a.86.86 0 0 0 .517-.436.752.752 0 0 0 .08-.498c-.064-.453-.33-.782-.724-.897a1.06 1.06 0 0 0-.466 0zm-9.203.005c-.305.096-.533.32-.65.639a1.187 1.187 0 0 0-.06.52c.057.309.31.59.598.667.362.095.632.033.872-.205.14-.136.215-.255.291-.448.055-.136.059-.16.059-.353l.001-.207-.072-.148a.894.894 0 0 0-.565-.472 1.02 1.02 0 0 0-.474.007Zm4.184 2c-.131.071-.223.25-.195.383.031.143.157.288.353.407.105.063.112.072.117.136.004.038-.01.146-.029.243-.02.094-.036.194-.036.222.002.074.07.195.143.253.064.052.076.054.255.059.164.005.198.001.264-.03.169-.082.212-.234.15-.525-.052-.243-.042-.28.087-.355.137-.08.281-.219.324-.314a.365.365 0 0 0-.175-.48.394.394 0 0 0-.181-.033c-.126 0-.207.03-.355.124l-.085.053-.053-.032c-.219-.13-.259-.145-.391-.143a.396.396 0 0 0-.193.032zm.39-2.195c-.373.036-.475.05-.654.086-.291.06-.68.195-.951.328-.94.46-1.589 1.226-1.787 2.114-.04.176-.045.234-.045.53 0 .294.005.357.043.524.264 1.16 1.332 2.017 2.714 2.173.3.033 1.596.033 1.896 0 1.11-.125 2.064-.727 2.493-1.571.114-.226.169-.372.22-.602.039-.167.044-.23.044-.523 0-.297-.005-.355-.045-.531-.288-1.29-1.539-2.304-3.072-2.497a6.873 6.873 0 0 0-.855-.031zm.645.937a3.283 3.283 0 0 1 1.44.514c.223.148.537.458.671.662.166.251.26.508.303.82.02.143.01.251-.043.482-.08.345-.332.705-.672.957a3.115 3.115 0 0 1-.689.348c-.382.122-.632.144-1.525.138-.582-.006-.686-.01-.853-.042-.57-.107-1.022-.334-1.35-.68-.264-.28-.385-.535-.45-.946-.03-.192.025-.509.137-.776.136-.326.488-.73.836-.963.403-.269.934-.46 1.422-.512.187-.02.586-.02.773-.002zm-5.503-11a1.653 1.653 0 0 0-.683.298C5.617.74 5.173 1.666 4.985 2.819c-.07.436-.119 1.04-.119 1.503 0 .544.064 1.24.155 1.721.02.107.031.202.023.208a8.12 8.12 0 0 1-.187.152 5.324 5.324 0 0 0-.949 1.02 5.49 5.49 0 0 0-.94 2.339 6.625 6.625 0 0 0-.023 1.357c.091.78.325 1.438.727 2.04l.13.195-.037.064c-.269.452-.498 1.105-.605 1.732-.084.496-.095.629-.095 1.294 0 .67.009.803.088 1.266.095.555.288 1.143.503 1.534.071.128.243.393.264.407.007.003-.014.067-.046.141a7.405 7.405 0 0 0-.548 1.873c-.062.417-.071.552-.071.991 0 .56.031.832.148 1.279L3.42 24h1.478l-.05-.091c-.297-.552-.325-1.575-.068-2.597.117-.472.25-.819.498-1.296l.148-.29v-.177c0-.165-.003-.184-.057-.293a.915.915 0 0 0-.194-.25 1.74 1.74 0 0 1-.385-.543c-.424-.92-.506-2.286-.208-3.451.124-.486.329-.918.544-1.154a.787.787 0 0 0 .223-.531c0-.195-.07-.355-.224-.522a3.136 3.136 0 0 1-.817-1.729c-.14-.96.114-2.005.69-2.834.563-.814 1.353-1.336 2.237-1.475.199-.033.57-.028.776.01.226.04.367.028.512-.041.179-.085.268-.19.374-.431.093-.215.165-.333.36-.576.234-.29.46-.489.822-.729.413-.27.884-.467 1.352-.561.17-.035.25-.04.569-.04.319 0 .398.005.569.04a4.07 4.07 0 0 1 1.914.997c.117.109.398.457.488.602.034.057.095.177.132.267.105.241.195.346.374.43.14.068.286.082.503.045.343-.058.607-.053.943.016 1.144.23 2.14 1.173 2.581 2.437.385 1.108.276 2.267-.296 3.153-.097.15-.193.27-.333.419-.301.322-.301.722-.001 1.053.493.539.801 1.866.708 3.036-.062.772-.26 1.463-.533 1.854a2.096 2.096 0 0 1-.224.258.916.916 0 0 0-.194.25c-.054.109-.057.128-.057.293v.178l.148.29c.248.476.38.823.498 1.295.253 1.008.231 2.01-.059 2.581a.845.845 0 0 0-.044.098c0 .006.329.009.732.009h.73l.02-.074.036-.134c.019-.076.057-.3.088-.516.029-.217.029-1.016 0-1.258-.11-.875-.295-1.57-.597-2.226-.032-.074-.053-.138-.046-.141.008-.005.057-.074.108-.152.376-.569.607-1.284.724-2.228.031-.26.031-1.378 0-1.628-.083-.645-.182-1.082-.348-1.525a6.083 6.083 0 0 0-.329-.7l-.038-.064.131-.194c.402-.604.636-1.262.727-2.04a6.625 6.625 0 0 0-.024-1.358 5.512 5.512 0 0 0-.939-2.339 5.325 5.325 0 0 0-.95-1.02 8.097 8.097 0 0 1-.186-.152.692.692 0 0 1 .023-.208c.208-1.087.201-2.443-.017-3.503-.19-.924-.535-1.658-.98-2.082-.354-.338-.716-.482-1.15-.455-.996.059-1.8 1.205-2.116 3.01a6.805 6.805 0 0 0-.097.726c0 .036-.007.066-.015.066a.96.96 0 0 1-.149-.078A4.857 4.857 0 0 0 12 3.03c-.832 0-1.687.243-2.456.698a.958.958 0 0 1-.148.078c-.008 0-.015-.03-.015-.066a6.71 6.71 0 0 0-.097-.725C8.997 1.392 8.337.319 7.46.048a2.096 2.096 0 0 0-.585-.041Zm.293 1.402c.248.197.523.759.682 1.388.03.113.06.244.069.292.007.047.026.152.041.233.067.365.098.76.102 1.24l.002.475-.12.175-.118.178h-.278c-.324 0-.646.041-.954.124l-.238.06c-.033.007-.038-.003-.057-.144a8.438 8.438 0 0 1 .016-2.323c.124-.788.413-1.501.696-1.711.067-.05.079-.049.157.013zm9.825-.012c.17.126.358.46.498.888.28.854.36 2.028.212 3.145-.019.14-.024.151-.057.144l-.238-.06a3.693 3.693 0 0 0-.954-.124h-.278l-.119-.178-.119-.175.002-.474c.004-.669.066-1.19.214-1.772.157-.623.434-1.185.68-1.382.078-.062.09-.063.159-.012z"/></svg><span class="text-sm font-medium">Ollama</span></span>
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[17px] w-[17px] fill-current" aria-hidden="true"><path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z"/></svg><span class="text-sm font-medium">OpenAI</span></span>
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[17px] w-[17px] fill-current" aria-hidden="true"><path d="M16.778 1.844v1.919q-.569-.026-1.138-.032-.708-.008-1.415.037c-1.93.126-4.023.728-6.149 2.237-2.911 2.066-2.731 1.95-4.14 2.75-.396.223-1.342.574-2.185.798-.841.225-1.753.333-1.751.333v4.229s.768.108 1.61.333c.842.224 1.789.575 2.185.799 1.41.798 1.228.683 4.14 2.75 2.126 1.509 4.22 2.11 6.148 2.236.88.058 1.716.041 2.555.005v1.918l7.222-4.168-7.222-4.17v2.176c-.86.038-1.611.065-2.278.021-1.364-.09-2.417-.357-3.979-1.465-2.244-1.593-2.866-2.027-3.68-2.508.889-.518 1.449-.906 3.822-2.59 1.56-1.109 2.614-1.377 3.978-1.466.667-.044 1.418-.017 2.278.02v2.176L24 6.014Z"/></svg><span class="text-sm font-medium">OpenRouter</span></span>
          <span class="flex shrink-0 items-center text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><span class="text-[15px] font-semibold lowercase tracking-tight">groq</span></span>
          <span class="flex shrink-0 items-center gap-2 text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><svg viewBox="0 0 24 24" class="h-[16px] w-[16px] fill-current" aria-hidden="true"><path d="M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23"/></svg><span class="text-sm font-medium">Cursor</span></span>
          <span class="flex shrink-0 items-center text-zinc-500 transition hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-100"><span class="text-[15px] font-semibold tracking-tight">Antigravity</span></span>
        </div>
        <div id="wwClone" aria-hidden="true" class="flex shrink-0 items-center gap-x-11 pr-11"></div>
      </div>
    </div>
  </div>
  <script>(function(){var s=document.getElementById('wwSet'),c=document.getElementById('wwClone');if(s&&c){c.innerHTML=s.innerHTML;}})();</script>
</section>

<section class="mx-auto max-w-2xl px-5 pb-24">
  <div class="reveal rounded-2xl border border-zinc-300 bg-white/85 p-5 shadow-xl shadow-cyan-500/5 backdrop-blur dark:border-zinc-800 dark:bg-[#141b2b]/70">
    <div class="mb-4 flex items-center gap-1.5">
      <span class="h-2.5 w-2.5 rounded-full bg-red-400/70"></span><span class="h-2.5 w-2.5 rounded-full bg-amber-400/70"></span><span class="h-2.5 w-2.5 rounded-full bg-emerald-400/70"></span>
      <span class="ml-2 mono text-xs text-zinc-400">lethe · live</span>
    </div>
    <div id="demoLog" role="log" aria-live="polite" aria-relevant="additions text" aria-label="Live demo conversation" class="min-h-[150px] max-h-[340px] space-y-3 overflow-y-auto"></div>
    <div id="demoChips" class="mt-1 flex flex-wrap gap-2"></div>
    <form id="demoForm" class="mt-3 flex gap-2">
      <input id="demoInput" autocomplete="off" placeholder="Ask about the demo systems…" class="flex-1 rounded-xl border border-zinc-300 bg-transparent px-3.5 py-2 text-sm outline-none transition focus:border-cyan-400 dark:border-zinc-700 dark:placeholder:text-zinc-500"/>
      <button class="shrink-0 rounded-full bg-zinc-950 px-5 py-2 text-sm font-medium text-white transition hover:bg-zinc-800 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Ask</button>
    </form>
  </div>
  <p class="mt-3 text-center text-xs text-zinc-400">A real query against the live graph — not a script. For the full <span class="text-zinc-600 dark:text-zinc-300">forget</span> flip, <a href="/app" class="underline decoration-dotted underline-offset-2 hover:text-cyan-600 dark:hover:text-cyan-400">open the app</a>.</p>
</section>

<section id="scrolly" class="scrolly relative border-t border-zinc-300 dark:border-zinc-900">
  <div class="scrolly-sticky">
    <div class="mx-auto w-full max-w-3xl px-5">
      <div class="mb-7 text-center">
        <div class="mono text-xs uppercase tracking-[0.18em] text-zinc-400">the same question, twice</div>
        <h2 class="serif mt-1 text-4xl tracking-tight sm:text-5xl">Watch it forget.</h2>
      </div>
      <div id="srail" class="mx-auto mb-8 flex max-w-lg items-center justify-between mono text-[10px] uppercase tracking-wider text-zinc-400">
        <span data-s="0">Ask</span><span class="srail-line"></span><span data-s="1">Recommend</span><span class="srail-line"></span><span data-s="2">Forget</span><span class="srail-line"></span><span data-s="3">Ask again</span><span class="srail-line"></span><span data-s="4">Flip</span>
      </div>
      <div id="sstage" data-stage="0" class="relative mx-auto max-w-2xl rounded-2xl border border-zinc-300 bg-white/85 p-6 shadow-2xl shadow-cyan-500/10 backdrop-blur dark:border-zinc-800 dark:bg-[#141b2b]/85 sm:p-8">
        <div id="ssQ" class="ss-el flex justify-end"><div class="max-w-[88%] rounded-2xl rounded-br-md bg-zinc-950 px-4 py-2.5 text-sm text-white dark:bg-white dark:text-black">If auth-service latency is high, what should I check?</div></div>
        <div class="relative mt-4 min-h-[96px]">
          <div id="ssA1" class="ss-el absolute inset-x-0 top-0 flex justify-start"><div class="max-w-[92%] rounded-2xl rounded-bl-md border border-zinc-300 bg-white px-4 py-2.5 text-sm leading-relaxed dark:border-zinc-800 dark:bg-[#0e1422]">Check the <span id="ssLC" class="ss-diss mono rounded bg-red-500/10 px-1.5 py-0.5 text-red-600 dark:text-red-400">legacy-cache</span> — flush and resize the cluster to recover.</div></div>
          <div id="ssA2" class="ss-el absolute inset-x-0 top-0 flex justify-start"><div class="max-w-[92%] rounded-2xl rounded-bl-md border border-cyan-500/40 bg-cyan-500/[0.05] px-4 py-2.5 text-sm leading-relaxed">Check the <span class="mono rounded bg-cyan-500/10 px-1.5 py-0.5 text-cyan-600 dark:text-cyan-400">session-store</span> connection pool and its hit rate.</div></div>
        </div>
        <div id="ssRec" class="ss-el mt-4 flex flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-xl border border-cyan-500/30 bg-cyan-500/5 px-4 py-2 mono text-[11px] text-cyan-700 dark:text-cyan-300">
          <span class="inline-flex items-center gap-1 font-medium"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>Forgotten</span><span class="text-cyan-500/40">·</span><span>2 documents</span><span class="text-cyan-500/40">·</span><span>10 nodes</span><span class="text-cyan-500/40">·</span><span>18 edges removed</span>
        </div>
        <div id="ssCap" class="mt-6 text-center text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">You ask your on-call assistant.</div>
      </div>
      <div id="ssHint" class="mt-6 text-center mono text-[10px] uppercase tracking-wider text-zinc-400" style="opacity:.6">scroll &darr;</div>
    </div>
  </div>
</section>

<section class="border-t border-zinc-300 dark:border-zinc-900">
  <div class="mx-auto max-w-5xl px-5 py-24">
    <div class="reveal">
      <div class="mb-1 text-center mono text-xs uppercase tracking-[0.18em] text-zinc-400">the platform</div>
      <h2 class="serif mb-12 text-center text-3xl tracking-tight sm:text-4xl">Memory you can rely on.</h2>
      <div class="stg grid gap-x-10 gap-y-9 sm:grid-cols-2 lg:grid-cols-3">
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22z"/></svg></div><div><h3 class="text-sm font-semibold">Persistent threads</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Every conversation saved — pick it back up later.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></svg></div><div><h3 class="text-sm font-semibold">Isolated workspaces</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Separate knowledge bases, each with its own graph.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></div><div><h3 class="text-sm font-semibold">Multi-turn context</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Follow-ups understand what you just asked.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg></div><div><h3 class="text-sm font-semibold">Runs fully offline</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Point it at a local model — nothing leaves your machine.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg></div><div><h3 class="text-sm font-semibold">Verifiable deletion</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Hard-delete across graph and vectors — receipt included, re-arm in one click.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="6" r="2.5"/><circle cx="12" cy="18" r="2.5"/><path d="m7 7.5 3.5 8.5M17 7.5 13.5 16M7.2 6.4h9.6"/></svg></div><div><h3 class="text-sm font-semibold">Hybrid memory</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">A knowledge graph and a vector store, working together.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m7 11 2-2-2-2"/><path d="M11 13h4"/><rect width="18" height="18" x="3" y="3" rx="2"/></svg></div><div><h3 class="text-sm font-semibold">Callable over MCP</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Triage and decommission from Claude or Cursor — plus a one-command Claude Code plugin.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5"/><path d="M16 13H8"/><path d="M16 17H8"/></svg></div><div><h3 class="text-sm font-semibold">Citations that open</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Click a chip to read the original runbook — after a forget, it is verifiably gone too.</p></div></div>
        <div class="flex gap-3.5"><div class="mt-0.5 shrink-0 text-cyan-500"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg></div><div><h3 class="text-sm font-semibold">Memory health, measured</h3><p class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">One deterministic staleness score plus a reversible decay loop — free, 0 tokens.</p></div></div>
      </div>
    </div>
  </div>
</section>

<section id="how" class="border-t border-zinc-300 dark:border-zinc-900">
  <div class="mx-auto max-w-5xl px-5 py-24">
    <div class="reveal text-center">
      <div class="mb-1 mono text-xs uppercase tracking-[0.18em] text-zinc-400">how it works</div>
      <h2 class="serif text-3xl tracking-tight sm:text-4xl">The whole loop — including the verb everyone skips.</h2>
      <p class="mx-auto mt-4 max-w-2xl text-base leading-relaxed text-zinc-500 dark:text-zinc-400">Every step is a real call in the codebase — remember, recall, and the one almost no one ships: <span class="font-medium text-zinc-900 dark:text-zinc-100">forget</span>.</p>
    </div>
    <div id="lifecycle" class="relative mt-12">
      <div class="lc-rail hidden text-zinc-200 dark:text-zinc-800 sm:block" aria-hidden="true"></div>
      <div id="lcFill" class="lc-fill hidden sm:block" aria-hidden="true"></div>
      <ol class="relative grid grid-cols-2 gap-3 sm:grid-cols-5">
        <li class="lc-stage rounded-2xl border border-zinc-300 p-5 text-center dark:border-zinc-800"><span class="lc-dot mx-auto mb-3 block h-2.5 w-2.5 rounded-full" aria-hidden="true"></span><div class="mono text-xs text-cyan-600 dark:text-cyan-400">cognee.add</div><div class="mt-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">Remember</div><div class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">ingest runbooks</div></li>
        <li class="lc-stage rounded-2xl border border-zinc-300 p-5 text-center dark:border-zinc-800"><span class="lc-dot mx-auto mb-3 block h-2.5 w-2.5 rounded-full" aria-hidden="true"></span><div class="mono text-xs text-cyan-600 dark:text-cyan-400">cognee.cognify</div><div class="mt-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">Structure</div><div class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">graph + vectors</div></li>
        <li class="lc-stage rounded-2xl border border-zinc-300 p-5 text-center dark:border-zinc-800"><span class="lc-dot mx-auto mb-3 block h-2.5 w-2.5 rounded-full" aria-hidden="true"></span><div class="mono text-xs text-cyan-600 dark:text-cyan-400">cognee.search</div><div class="mt-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">Recall</div><div class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">graph-grounded triage</div></li>
        <li class="lc-stage lc-hero rounded-2xl border border-cyan-500/50 bg-cyan-500/[0.04] p-5 text-center"><span class="lc-dot lc-dot-hero mx-auto mb-3 block h-2.5 w-2.5 rounded-full" aria-hidden="true"></span><div class="mono text-xs text-cyan-600 dark:text-cyan-400">cognee.forget</div><div class="mt-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">Forget <span class="mono text-[10px] text-cyan-500">the differentiator</span></div><div class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">verifiable hard-delete</div></li>
        <li class="lc-stage rounded-2xl border border-zinc-300 p-5 text-center dark:border-zinc-800"><span class="lc-dot mx-auto mb-3 block h-2.5 w-2.5 rounded-full" aria-hidden="true"></span><div class="mono text-xs text-cyan-600 dark:text-cyan-400">cognee.prune</div><div class="mt-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">Reset</div><div class="mt-1 text-xs text-zinc-500 dark:text-zinc-400">rebuild from clean</div></li>
      </ol>
    </div>
    <div class="reveal mt-8 mx-auto max-w-2xl rounded-2xl border border-dashed border-zinc-300 dark:border-zinc-800 p-5">
      <div class="mono text-xs text-zinc-400">deliberately omitted</div>
      <div class="mt-1 text-sm leading-relaxed text-zinc-500 dark:text-zinc-400">Cognee also ships <span class="mono text-zinc-700 dark:text-zinc-300">improve()</span> — memory that silently grows itself. We skip it: memory grows on <span class="font-medium text-zinc-900 dark:text-zinc-100">explicit ingest</span>, shrinks on <span class="font-medium text-zinc-900 dark:text-zinc-100">verified forget</span>.</div>
    </div>
  </div>
</section>

<section id="evSection" class="hidden border-t border-zinc-300 dark:border-zinc-900">
  <div class="mx-auto max-w-5xl px-5 py-24">
    <div class="reveal text-center">
      <div class="mb-1 mono text-xs uppercase tracking-[0.18em] text-zinc-400">the stale-advice benchmark</div>
      <h2 class="serif text-3xl tracking-tight sm:text-4xl">Forgetting, proven.</h2>
      <p class="mx-auto mt-4 max-w-2xl text-base leading-relaxed text-zinc-500 dark:text-zinc-400">Most memory products claim they handle stale knowledge. We measure it — decommission three systems, re-ask, and let a blind judge score every answer.</p>
    </div>
    <div id="evGrid" class="reveal mt-12 grid grid-cols-1 gap-4 sm:grid-cols-3"></div>
    <div id="evProof" class="reveal mt-4"></div>
    <p id="evCap" class="reveal mt-6 text-center mono text-xs text-zinc-400"></p>
  </div>
</section>

<section class="border-t border-zinc-300 dark:border-zinc-900">
  <div class="mx-auto max-w-3xl px-5 py-24 text-center">
    <h2 class="reveal serif text-4xl tracking-tight sm:text-5xl">Everyone builds AI that remembers more.</h2>
    <p class="mx-auto mt-4 max-w-xl text-base leading-relaxed text-zinc-500 dark:text-zinc-400">The whole field accumulates — and stale runbooks mislead as often as they help. Lethe makes the opposite move: <span class="font-medium text-zinc-900 dark:text-zinc-100">forgetting the dead thing, on command and verifiably</span>.</p>
  </div>
</section>

</main>
<footer class="relative overflow-hidden border-t border-zinc-300 dark:border-zinc-900">
  <div class="mx-auto max-w-5xl px-5 pt-20 pb-6 text-center">
    <h2 class="serif text-3xl tracking-tight">Stop giving 3 a.m. advice about systems that no longer exist.</h2>
    <a href="/app" class="mt-6 inline-block rounded-full bg-zinc-950 px-7 py-3 text-sm font-medium text-white transition duration-200 hover:-translate-y-0.5 hover:bg-zinc-800 hover:shadow-lg hover:shadow-cyan-500/10 dark:bg-white dark:text-black dark:hover:bg-zinc-200">Launch the app →</a>
    <p class="mt-8 text-xs text-zinc-400">Built on Cognee · self-hosted · remember + verifiable forget</p>
  </div>
  <div aria-hidden="true" class="relative mx-auto h-24 w-full max-w-6xl overflow-hidden" style="-webkit-mask-image:linear-gradient(90deg,transparent,#000 14%,#000 86%,transparent);mask-image:linear-gradient(90deg,transparent,#000 14%,#000 86%,transparent)">
    <svg class="wv wv-b" viewBox="0 0 1440 60" preserveAspectRatio="none" fill="none"><path d="M0 37 Q90 19 180 37 T360 37 T540 37 T720 37 T900 37 T1080 37 T1260 37 T1440 37" stroke="#6366f1" stroke-width="2" stroke-opacity=".35" stroke-linecap="round"/></svg>
    <svg class="wv wv-f" viewBox="0 0 1440 60" preserveAspectRatio="none" fill="none"><defs><linearGradient id="lwave" x1="0" x2="1" y1="0" y2="0"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#6366f1"/></linearGradient></defs><path d="M0 30 Q90 9 180 30 T360 30 T540 30 T720 30 T900 30 T1080 30 T1260 30 T1440 30" stroke="url(#lwave)" stroke-width="2.5" stroke-linecap="round"/></svg>
  </div>
  <div aria-hidden="true" class="reveal lethe-mark serif italic select-none px-2 text-center">Lethe</div>
</footer>

<script>
 const root=document.documentElement;
 if(localStorage.theme==='light')root.classList.remove('dark');
 document.getElementById('theme').onclick=()=>{root.classList.toggle('dark');localStorage.theme=root.classList.contains('dark')?'dark':'light';};
 document.querySelectorAll('.card').forEach(c=>{c.addEventListener('mousemove',e=>{const r=c.getBoundingClientRect();c.style.setProperty('--mx',(e.clientX-r.left)+'px');c.style.setProperty('--my',(e.clientY-r.top)+'px');});c.addEventListener('mouseleave',()=>{c.style.setProperty('--mx','-200px');c.style.setProperty('--my','-200px');});});
 document.querySelectorAll('.stg').forEach(g=>[...g.children].forEach((c,i)=>c.style.setProperty('--i',i)));
 const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}}),{threshold:.12});
 document.querySelectorAll('.reveal,.stg').forEach(el=>io.observe(el));

 // signature scroll moment — the forget hero plays out as you scroll through a pinned section
 (function(){
   const sec=document.getElementById('scrolly'),stage=document.getElementById('sstage');
   if(!sec||!stage)return;
   const $=id=>document.getElementById(id);
   const ssQ=$('ssQ'),ssA1=$('ssA1'),ssA2=$('ssA2'),ssLC=$('ssLC'),ssRec=$('ssRec'),ssCap=$('ssCap'),ssHint=$('ssHint'),srail=$('srail');
   const CAPS=['You ask your on-call assistant.',
     'It recommends checking the legacy-cache — flush and resize the cluster.',
     'You decommission legacy-cache. Gone — across the graph and the vectors.',
     'Now ask the exact same question again — byte for byte.',
     'The answer flips to the session-store. The dead system is gone for good.'];
   let cur=-1;
   function setStage(s){
     if(s===cur)return; cur=s; stage.dataset.stage=s;
     ssQ.classList.toggle('show',s>=0);
     ssA1.classList.toggle('show',s>=1&&s<3);
     ssLC.classList.toggle('gone',s>=2);
     ssRec.classList.toggle('show',s>=2);
     ssA2.classList.toggle('show',s>=4);
     ssQ.classList.toggle('reask',s===3);
     if(ssCap)ssCap.textContent=CAPS[s]||CAPS[0];
     if(ssHint)ssHint.style.opacity=s>=4?'0':'.6';
     if(srail)srail.querySelectorAll('[data-s]').forEach(el=>el.classList.toggle('on',(+el.dataset.s)<=s));
   }
   if(matchMedia('(prefers-reduced-motion:reduce)').matches){sec.classList.add('noscroll');setStage(4);return;}
   let tick=false;
   function compute(){tick=false;
     const total=sec.offsetHeight-window.innerHeight;
     const scrolled=Math.min(Math.max(-sec.getBoundingClientRect().top,0),Math.max(total,1));
     const prog=total>0?scrolled/total:0;
     setStage(Math.max(0,Math.min(4,Math.floor(prog*5))));
   }
   addEventListener('scroll',()=>{if(!tick){tick=true;requestAnimationFrame(compute);}},{passive:true});
   addEventListener('resize',()=>{if(!tick){tick=true;requestAnimationFrame(compute);}},{passive:true});
   compute();
 })();

 // measured proof — pulls the REAL correctness benchmark (blind independent judge, 0-2). Stays HIDDEN
 // until that benchmark has actually been run, so the landing never shows an unproven/circular number.
 (function(){
   const fmt=x=>(typeof x==='number')?x.toFixed(1):'—';
   fetch('/evidence').then(r=>r.json()).then(d=>{
     if(!d||!d.available)return;
     const sec=document.getElementById('evSection');if(!sec)return;
     const clamp=x=>Math.max(0,Math.min(100,x/2*100));
     // bars start at width:0 (CSS) and grow to data-w on reveal; the thin marker shows the BEFORE value
     const mbar=(bef,aft)=>'<div class="relative mt-3.5 h-1.5 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800/80"><div class="evbar-fill absolute inset-y-0 left-0 rounded-full '+(aft>bef?'bg-cyan-500':'bg-zinc-400 dark:bg-zinc-500')+'" data-w="'+clamp(aft)+'%"></div><div class="absolute inset-y-0 w-px bg-zinc-500/70 dark:bg-zinc-400/70" style="left:calc('+clamp(bef)+'% - 0.5px)"></div></div>';
     // the AFTER number counts up from BEFORE on reveal (data-to / data-from), so the proof feels measured live
     const metric=(label,bef,aft,desc)=>{const up=aft>bef,acc=up?'text-cyan-600 dark:text-cyan-400':'text-zinc-700 dark:text-zinc-100';
       return '<div class="rounded-2xl border border-zinc-300 p-6 dark:border-zinc-800">'
         +'<div class="mono text-[11px] uppercase tracking-[0.16em] text-zinc-400">'+label+'</div>'
         +'<div class="mt-3 flex items-baseline gap-1.5"><span class="serif text-3xl text-zinc-400 dark:text-zinc-500">'+bef.toFixed(1)+'</span><span class="text-base text-zinc-300 dark:text-zinc-600">&rarr;</span><span class="serif text-[2.7rem] leading-none '+acc+'" data-to="'+aft+'" data-from="'+bef+'">'+bef.toFixed(1)+'</span><span class="ml-0.5 mono text-xs text-zinc-400">/2</span></div>'
         +mbar(bef,aft)
         +'<div class="mt-3.5 text-sm leading-snug text-zinc-500 dark:text-zinc-400">'+desc+'</div></div>';};
     const grid=document.getElementById('evGrid'); grid.classList.add('stg');
     grid.innerHTML=
       metric('Update', d.stale_before, d.stale_after, 'Stale advice gets corrected once the dead system is forgotten.')
       +(d.abstention_after!=null?metric('Abstention', d.abstention_before, d.abstention_after, 'Correctly answers &ldquo;not documented&rdquo; about a forgotten system.'):'')
       +metric('Control', d.control_before, d.control_after, 'Unrelated answers stay correct &mdash; the forget is surgical.');
     [...grid.children].forEach((c,i)=>c.style.setProperty('--i',i));
     if(d.retrieval_proof_pass!=null){
       const ok=d.retrieval_proof_pass;
       document.getElementById('evProof').innerHTML='<div class="mx-auto max-w-2xl rounded-2xl border p-5 text-center '+(ok?'border-emerald-500/40':'border-zinc-300 dark:border-zinc-800')+'"><span class="mono text-xs '+(ok?'text-emerald-600 dark:text-emerald-400':'text-zinc-400')+'">'+(ok?'&#10003; retrieval-layer deletion verified':'retrieval-layer deletion: not verified')+'</span><div class="mt-1 text-sm text-zinc-500 dark:text-zinc-400">after a forget, a chunk-level search returns <span class="font-medium text-zinc-900 dark:text-zinc-100">zero</span> chunks from the forgotten document &mdash; gone from the index, not merely rephrased in the answer</div></div>';
     }
     document.getElementById('evCap').textContent='The Stale-Advice Eradication benchmark · independently judged ('+(d.judge_model||'a different model')+'), blind to before/after · research/forget_correctness_benchmark.py'+(d.generated_at?' · last run '+String(d.generated_at).slice(0,10):'');
     sec.classList.remove('hidden');
     const reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
     const reveal=()=>{
       sec.querySelectorAll('.reveal,.stg').forEach(e=>e.classList.add('in'));
       grid.querySelectorAll('.evbar-fill').forEach(b=>{b.style.width=b.dataset.w;});
       grid.querySelectorAll('[data-to]').forEach(el=>{const to=+el.dataset.to,from=+(el.dataset.from||0);
         if(reduce){el.textContent=to.toFixed(1);return;}
         const t0=performance.now(),dur=950;(function step(t){const k=Math.min(1,(t-t0)/dur),e=1-Math.pow(1-k,3);el.textContent=(from+(to-from)*e).toFixed(1);if(k<1)requestAnimationFrame(step);})(t0);});
     };
     if(reduce){reveal();}
     else{const o=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){reveal();o.disconnect();}}),{threshold:.18});o.observe(sec);}
   }).catch(()=>{});
 })();

 // flow wave — the river of Lethe: barely there at the top, grows a touch as you scroll, then fades out
 // as the footer river takes over (the merge). rAF-throttled; respects reduced-motion.
 (function(){
   const fw=document.getElementById('flowWave');if(!fw)return;
   if(matchMedia('(prefers-reduced-motion:reduce)').matches){fw.style.opacity='.12';return;}
   const footer=document.querySelector('footer');let tick=false;
   function apply(){tick=false;const sc=window.scrollY,vh=window.innerHeight;
     const prog=Math.min(1,sc/(vh*1.1));let op=.12+prog*.34;
     if(footer){const ft=footer.getBoundingClientRect().top;if(ft<vh)op*=Math.max(0,Math.min(1,ft/(vh*.7)));}
     fw.style.opacity=op.toFixed(3);}
   window.addEventListener('scroll',()=>{if(!tick){tick=true;requestAnimationFrame(apply);}},{passive:true});
   apply();
 })();

 // live demo — REAL queries against the running knowledge graph. No scripted/hardcoded answers: each
 // bubble is the actual /ask response. (A live FORGET can't run on the shared public graph — that would
 // wipe the demo for the next visitor — so the forget flip is in the app + the receipt below.)
 (function(){
   const log=document.getElementById('demoLog'),chips=document.getElementById('demoChips'),
         form=document.getElementById('demoForm'),input=document.getElementById('demoInput');
   if(!log||!form)return;
   const samples=['If auth-service latency is high, what should I check?','Who owns the payments-service?','What breaks if the auth-service fails?'];
   function bub(role,html){const w=document.createElement('div');w.className='flex '+(role==='u'?'justify-end':'justify-start');
     const c=document.createElement('div');c.className='max-w-[88%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed '+(role==='u'?'rounded-br-md bg-zinc-950 text-white dark:bg-white dark:text-black':'rounded-bl-md border border-zinc-300 bg-white dark:border-zinc-800 dark:bg-[#1a2233]');
     c.innerHTML=html;w.appendChild(c);log.appendChild(w);log.scrollTop=log.scrollHeight;return c;}
   bub('a','Ask about the demo systems — click a question below or type your own. These are real answers, straight from the knowledge graph.');
   let busy=false;
   async function ask(q){
     q=(q||'').trim();if(busy||!q)return;busy=true;
     bub('u',q.replace(/[<&]/g,m=>m==='<'?'&lt;':'&amp;'));
     const a=bub('a','<span class="text-zinc-400">thinking…</span>');
     const ctl=new AbortController();const to=setTimeout(()=>ctl.abort(),30000);
     try{
       const r=await fetch('/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q,workspace:'incidents'}),signal:ctl.signal});
       const j=await r.json();a.textContent=j.answer||'No answer.';
     }catch(e){a.innerHTML='<span class="text-zinc-400">Could not reach the model right now — try it in the <a href="/app" class="underline">app</a>.</span>';}
     finally{clearTimeout(to);busy=false;log.scrollTop=log.scrollHeight;}
   }
   samples.forEach(s=>{const b=document.createElement('button');b.type='button';b.textContent=s;
     b.className='rounded-full border border-zinc-300 px-3 py-1.5 text-xs text-zinc-600 transition hover:-translate-y-0.5 hover:border-cyan-400 hover:text-cyan-600 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-cyan-400 dark:hover:text-cyan-400';
     b.onclick=()=>ask(s);chips.appendChild(b);});
   form.onsubmit=e=>{e.preventDefault();const v=input.value;input.value='';ask(v);};
 })();

 // lifecycle pipeline — connector draws as you scroll, each stage's dot lights as the line reaches it.
 // Geometry is MEASURED from the real dot centers so the rail/fill align exactly at every breakpoint.
 (function(){
   const sec=document.getElementById('lifecycle');if(!sec)return;
   const fill=document.getElementById('lcFill'),rail=sec.querySelector('.lc-rail');
   const stages=[...sec.querySelectorAll('.lc-stage')];
   const dots=stages.map(s=>s.querySelector('.lc-dot'));
   if(!fill||!dots.length||dots.some(d=>!d))return;
   const reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
   let cx=[],x0=0,span=1;
   function measure(){const sr=sec.getBoundingClientRect();
     const c=dots.map(d=>{const r=d.getBoundingClientRect();return {x:r.left+r.width/2-sr.left,y:r.top+r.height/2-sr.top};});
     cx=c.map(p=>p.x);x0=cx[0];span=Math.max(1,cx[cx.length-1]-x0);
     const top=c[0].y+'px';
     if(rail){rail.style.left=x0+'px';rail.style.width=span+'px';rail.style.top=top;}
     fill.style.left=x0+'px';fill.style.top=top;}
   function paint(p){fill.style.width=(p*span).toFixed(1)+'px';
     stages.forEach((s,i)=>s.classList.toggle('lit',p>=(cx[i]-x0)/span-0.001));}
   if(reduce){measure();paint(1);return;}
   let running=false,ticking=false;
   function prog(){const r=sec.getBoundingClientRect(),vh=innerHeight;return Math.min(1,Math.max(0,(vh*0.85-r.top)/(vh*0.5)));}
   function render(){ticking=false;measure();paint(prog());}
   function onScroll(){if(!ticking){ticking=true;requestAnimationFrame(render);}}
   render();
   const io=new IntersectionObserver(es=>es.forEach(e=>{
     if(e.isIntersecting){if(!running){running=true;addEventListener('scroll',onScroll,{passive:true});}render();}
     else if(running){running=false;removeEventListener('scroll',onScroll);}
   }),{threshold:0});
   io.observe(sec);
   addEventListener('resize',()=>requestAnimationFrame(render),{passive:true});
 })();

 // live landing graph — a real vis-network physics slice you can drag + forget; recolors with the theme.
 (function(){
   const box=document.getElementById('lgraph');if(!box)return;
   const cap=document.getElementById('lgcap'),btn=document.getElementById('lgbtn'),rec=document.getElementById('lgrec');
   const reduce=matchMedia('(prefers-reduced-motion:reduce)').matches;
   function loadVisL(cb){if(window.vis)return cb();var s=document.createElement('script');s.src='https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js';s.onload=cb;s.onerror=function(){box.innerHTML='<div class="grid h-full place-items-center text-xs text-zinc-400">graph unavailable</div>';};document.head.appendChild(s);}
   const NODES=[{id:'auth',label:'auth-service',val:22},{id:'lc',label:'legacy-cache',val:14,dead:true},{id:'ss',label:'session-store',val:13},{id:'pay',label:'payments-service',val:13},{id:'api',label:'api-gateway',val:15},{id:'idx',label:'search-index',val:11},{id:'notif',label:'notifications',val:10},{id:'mon',label:'monitoring',val:9}];
   const EDGES=[['auth','lc'],['auth','ss'],['lc','ss'],['auth','api'],['api','pay'],['pay','idx'],['auth','mon'],['notif','api'],['api','idx']];
   function pal(){return document.documentElement.classList.contains('dark')
     ?{nb:'#22d3ee',nbr:'#0891b2',db:'#f87171',dbr:'#ef4444',edge:'rgba(100,116,139,.5)',font:'#e2e8f0',halo:'#0a0e1a',glow:'rgba(34,211,238,.85)'}
     :{nb:'#0891b2',nbr:'#0e7490',db:'#ef4444',dbr:'#dc2626',edge:'rgba(148,163,184,.7)',font:'#0f172a',halo:'#f4f7fc',glow:'rgba(8,145,178,.5)'};}
   function ncol(dead,p){return dead?{background:p.db,border:p.dbr}:{background:p.nb,border:p.nbr};}
   function nfont(p){return {color:p.font,size:13,strokeWidth:4,strokeColor:p.halo,face:"'Geist Mono',monospace"};}
   let net,nds,eds,gone=false,auto=!reduce;
   function build(){
     const p=pal();
     nds=new vis.DataSet(NODES.map(n=>({id:n.id,label:n.label,shape:'dot',value:n.val,color:ncol(n.dead,p),font:nfont(p)})));
     eds=new vis.DataSet(EDGES.map((e,i)=>({id:'e'+i,from:e[0],to:e[1],color:{color:p.edge},width:1.2})));
     net=new vis.Network(box,{nodes:nds,edges:eds},{
       nodes:{borderWidth:2,scaling:{min:8,max:24}},
       edges:{smooth:{type:'continuous',roundness:.45}},
       physics:{solver:'forceAtlas2Based',forceAtlas2Based:{gravitationalConstant:-65,centralGravity:.015,springLength:95,springConstant:.08,damping:.5,avoidOverlap:.7},stabilization:{iterations:220,fit:true},minVelocity:.55},
       interaction:{hover:true,dragNodes:true,dragView:false,zoomView:false}});
     net.on('hoverNode',pr=>{box.style.cursor='grab';const id=pr.node,nb=new Set([id]);eds.get().forEach(e=>{if(e.from===id)nb.add(e.to);if(e.to===id)nb.add(e.from);});const p2=pal();
       nds.update(nds.getIds().map(i=>({id:i,opacity:nb.has(i)?1:.22,shadow:i===id?{enabled:true,color:p2.glow,size:26,x:0,y:0}:{enabled:false}})));});
     net.on('blurNode',()=>{box.style.cursor='default';nds.update(nds.getIds().map(i=>({id:i,opacity:1,shadow:{enabled:false}})));});
     net.on('click',pr=>{if(pr.nodes&&pr.nodes[0]==='lc')toggle();});
     net.on('dragStart',()=>{auto=false;});
   }
   function recolor(){if(!nds)return;const p=pal();nds.update(NODES.filter(n=>nds.get(n.id)).map(n=>({id:n.id,color:ncol(n.dead,p),font:nfont(p)})));eds.update(eds.getIds().map(id=>({id,color:{color:p.edge}})));}
   function showRec(on){if(!rec)return;rec.classList.toggle('hidden',!on);rec.classList.toggle('flex',on);}
   function forget(){if(gone||!nds.get('lc'))return;gone=true;
     nds.update({id:'lc',opacity:.04,shadow:{enabled:true,color:'rgba(248,113,113,.7)',size:30,x:0,y:0}});
     const le=eds.get().filter(e=>e.from==='lc'||e.to==='lc').map(e=>e.id);
     setTimeout(()=>{try{eds.remove(le);nds.remove('lc');}catch(e){}},reduce?0:360);
     showRec(true);if(cap)cap.textContent='legacy-cache forgotten — gone from the graph and the vectors.';if(btn)btn.textContent='Replay';}
   function restore(){gone=false;showRec(false);if(cap)cap.textContent='An interactive preview of an incident graph — drag a node, or forget the dead one.';if(btn)btn.textContent='Decommission legacy-cache';
     const p=pal();if(!nds.get('lc'))nds.add({id:'lc',label:'legacy-cache',shape:'dot',value:14,color:ncol(true,p),font:nfont(p)});
     if(!eds.get('e0'))eds.add({id:'e0',from:'auth',to:'lc',color:{color:p.edge},width:1.2});
     if(!eds.get('e2'))eds.add({id:'e2',from:'lc',to:'ss',color:{color:p.edge},width:1.2});}
   function toggle(){auto=false;if(gone)restore();else forget();}
   loadVisL(function(){build();if(btn)btn.onclick=toggle;
     if(!reduce)(function loop(){if(!auto)return;setTimeout(function(){if(!auto)return;forget();setTimeout(function(){if(!auto)return;restore();setTimeout(loop,1400);},2600);},3400);})();
     new MutationObserver(recolor).observe(document.documentElement,{attributes:true,attributeFilter:['class']});});
 })();

</script>
</body></html>"""


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


@app.get("/", response_class=HTMLResponse)
async def index():
    return LANDING


@app.get("/app", response_class=HTMLResponse)
async def app_view():
    return PAGE
