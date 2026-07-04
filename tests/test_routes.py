"""Route tests via FastAPI TestClient.

Every test mocks ib.ask / ib.forget_system and seeds in-memory app.S state, so NO
real cognee / LLM / DB call ever happens and the live :8077 server is never touched.
The `loopback_client` fixture spoofs request.client.host = 127.0.0.1 so the no-token
auth gate treats requests as the local demo (allowed). Tests that exercise the auth
fail-closed path use a NON-loopback client on purpose.
"""
import pytest
from fastapi.testclient import TestClient

import incident_brain as ib


# ---------------------------------------------------------------------------
# 7. /ask returns {answer, citations} (ib.ask mocked)
# ---------------------------------------------------------------------------

def test_ask_returns_answer_and_citations(loopback_client, ib_mod):
    async def fake_ask(q, user=None, history=None, dataset=None, feedback_influence=0.0, **kwargs):
        return "Check the legacy-cache and flush it."
    ib_mod.ask = fake_ask

    r = loopback_client.post("/ask", json={"query": "auth slow?", "workspace": "incidents"})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"answer", "citations"}
    assert body["answer"] == "Check the legacy-cache and flush it."
    assert isinstance(body["citations"], list)


# ---------------------------------------------------------------------------
# 8. /upload into default 'incidents' is REJECTED (golden guard), no ingest
# ---------------------------------------------------------------------------

def test_upload_into_default_workspace_rejected(loopback_client, monkeypatch):
    # If the golden guard ever fails to short-circuit, this would schedule a real
    # ingest task -> blow up the test. Make that path explode loudly to prove it's
    # never reached.
    import app as appmod

    def _boom(*a, **k):
        raise AssertionError("_ingest_text must NOT run for the default workspace")
    monkeypatch.setattr(appmod, "_ingest_text", _boom)

    r = loopback_client.post("/upload", json={"text": "secret doc", "system": "x", "workspace": "incidents"})
    assert r.status_code == 200
    assert "disabled to protect the demo data" in r.json()["error"]
    # ingest lock state untouched (no ingest started)
    assert appmod.S["ingest"]["state"] == "idle"


# ---------------------------------------------------------------------------
# 9. Security headers present on /app
# ---------------------------------------------------------------------------

def test_security_headers_on_app(loopback_client):
    r = loopback_client.get("/app")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


# ---------------------------------------------------------------------------
# 10. /health exposure: no token -> includes systems+llm; token set + anon -> omits
# ---------------------------------------------------------------------------

def test_health_no_token_includes_systems_and_llm(loopback_client):
    r = loopback_client.get("/health")
    body = r.json()
    assert "systems" in body
    assert "llm" in body
    assert body["auth"] is False


def test_health_token_set_anonymous_omits_systems_and_llm(ready_app):
    ready_app._AUTH_TOKEN = "secrettoken"  # restored by the `app` fixture teardown
    # /health is auth-open, so the middleware lets the anonymous request through; the
    # handler itself must withhold systems+llm from an unauthenticated caller.
    c = TestClient(ready_app.app, client=("127.0.0.1", 50000))
    body = c.get("/health").json()
    assert "systems" not in body
    assert "llm" not in body
    assert body["auth"] is True
    # an AUTHENTICATED caller (correct bearer) still gets them
    body2 = c.get("/health", headers={"Authorization": "Bearer secrettoken"}).json()
    assert "systems" in body2 and "llm" in body2


# ---------------------------------------------------------------------------
# 11. /llm-config POST with metadata endpoint -> rejected {ok:false} (SSRF), no mutation
# ---------------------------------------------------------------------------

def test_llm_config_rejects_ssrf_no_mutation(loopback_client, monkeypatch):
    import app as appmod
    import litellm

    # If the SSRF guard ever fails, the handler would call litellm.acompletion +
    # _apply_llm_config. Make BOTH explode so a regression can't silently pass.
    async def _boom_completion(*a, **k):
        raise AssertionError("litellm.acompletion must NOT be called for a rejected SSRF endpoint")
    monkeypatch.setattr(litellm, "acompletion", _boom_completion)
    monkeypatch.setattr(appmod, "_apply_llm_config",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("config must not be applied")))

    r = loopback_client.post("/llm-config", json={
        "provider": "openai", "model": "x",
        "endpoint": "http://169.254.169.254/", "api_key": "k",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# 12. Auth fail-closed: no token + NON-loopback client -> mutating route 403
# ---------------------------------------------------------------------------

def test_mutating_route_fails_closed_for_remote_client(ready_app):
    assert ready_app._AUTH_TOKEN == ""  # no token configured
    # default TestClient client host is 'testclient' (NOT loopback) -> remote caller.
    remote = TestClient(ready_app.app)
    r = remote.post("/forget", json={"system": "legacy-cache", "workspace": "incidents"})
    assert r.status_code == 403
    assert "not configured for remote access" in r.json()["detail"]


def test_public_demo_mode_allows_remote_readonly(ready_app, monkeypatch):
    # LETHE_PUBLIC_DEMO=1 (hosted demo) opts out of the remote fail-closed gate — an
    # anonymous remote caller can use the API (rate limiting is the abuse guard there).
    monkeypatch.setattr(ready_app, "_PUBLIC_DEMO", True)
    remote = TestClient(ready_app.app)  # non-loopback client host
    r = remote.get("/systems?workspace=incidents")
    assert r.status_code == 200
    assert any(s["name"] == "legacy-cache" for s in r.json()["systems"])


def test_mutating_route_allowed_for_loopback_client(loopback_client, ib_mod, monkeypatch):
    # Sibling assertion to the above: the SAME route, from loopback, passes the gate.
    # /forget reads the graph BEFORE the n==0 shortcut, so stub _graph_counts (would
    # otherwise hit real cognee) and forget_system (0 docs -> "no documents" message,
    # never reaching the real delete / proof re-query).
    import app as appmod

    async def fake_counts(ws):
        return (None, None)
    monkeypatch.setattr(appmod, "_graph_counts", fake_counts)

    async def fake_forget(name, ledger, dataset="main_dataset"):
        return 0
    ib_mod.forget_system = fake_forget

    r = loopback_client.post("/forget", json={"system": "nonexistent", "workspace": "incidents"})
    assert r.status_code == 200
    assert "No documents tagged" in r.json()["message"]


# ---------------------------------------------------------------------------
# 13. /source/{system} citation peek — golden wiki docs, ledger-gated (404 post-forget)
# ---------------------------------------------------------------------------

def test_source_peek_returns_wiki_docs_and_404s_when_absent(loopback_client):
    # legacy-cache is in the seeded live ledger AND has 2 docs in the static WIKI
    # (runbook + post-mortem) -> 200 with both, cognee-free.
    r = loopback_client.get("/source/legacy-cache")
    assert r.status_code == 200
    docs = r.json()["docs"]
    assert len(docs) == 2
    assert all(d["title"] and d["text"] for d in docs)
    assert any("cache" in d["text"].lower() for d in docs)
    # A system that is NOT in the live ledger -> 404 (this is exactly what makes the
    # peek vanish after a forget, so it can't contradict Proof-of-Forgetting).
    assert loopback_client.get("/source/nonexistent").status_code == 404
