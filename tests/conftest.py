"""Shared pytest fixtures for the Lethe regression suite.

HERMETIC GUARANTEE: nothing here (or in any test) touches real cognee, the real
data dir (~/.cognee-incident-detective/), the golden ledger.json / golden_snapshot/,
or the live server on :8077. Importing `app` runs only module-level code (it does
`import cognee`, which is fine — cognee is installed) but NOT the FastAPI startup
event, so no graph/DB/LLM work happens. Every route test mocks ib.ask /
ib.forget_system / litellm, and seeds the in-memory `app.S` state by hand.
"""
import importlib
import socket

import pytest

# Import the modules under test once. This executes module-level code only
# (incident_brain sets CACHING=false then imports cognee; app imports both and
# defines routes). The @app.on_event("startup") handler is NOT triggered by import,
# so load_ledger() / get_default_user() / any cognee call never runs here.
import incident_brain as ib  # noqa: E402
import app as appmod  # noqa: E402


@pytest.fixture
def app():
    """The FastAPI module, with in-memory state reset to a clean baseline after each test.

    We snapshot the keys we mutate and restore them afterwards so tests can't bleed
    state into one another (or into anything else that imports `app` in-process).
    """
    saved_S = dict(appmod.S)
    saved_token = appmod._AUTH_TOKEN
    saved_ask = ib.ask
    saved_forget = ib.forget_system
    try:
        yield appmod
    finally:
        appmod.S.clear()
        appmod.S.update(saved_S)
        appmod._AUTH_TOKEN = saved_token
        ib.ask = saved_ask
        ib.forget_system = saved_forget


@pytest.fixture
def ib_mod():
    """incident_brain, with ask/forget_system restored after the test (in case a test
    monkeypatches them directly rather than via the `app` fixture)."""
    saved_ask = ib.ask
    saved_forget = ib.forget_system
    saved_smalltalk = ib._smalltalk
    try:
        yield ib
    finally:
        ib.ask = saved_ask
        ib.forget_system = saved_forget
        ib._smalltalk = saved_smalltalk


def _ready_state(appmod):
    """Seed `app.S` to a minimal READY state so route handlers run their happy path
    without ever calling cognee. Only the default golden workspace is registered."""
    appmod.S["ready"] = True
    appmod.S["status"] = "ready"
    appmod.S["user"] = object()  # opaque; ib.ask is mocked so .id is never read
    appmod.S["ledger"] = {"api-gateway": ["d1"], "legacy-cache": ["d2"]}
    appmod.S["workspaces"] = [appmod.DEFAULT_WS]
    appmod.S["ws_ledgers"] = {"incidents": {"api-gateway": ["d1"], "legacy-cache": ["d2"]}}
    appmod.S["texts"] = {"incidents": {}}
    appmod.S["tombstones"] = {"incidents": set()}
    appmod.S["reviewed"] = {"incidents": {}}
    appmod.S["ingest"] = {"state": "idle"}
    appmod.S["llm"] = {"provider": "custom", "model": "m", "label": "m", "local": False}


@pytest.fixture
def ready_app(app):
    """`app` already seeded to READY state (golden workspace only)."""
    _ready_state(app)
    return app


@pytest.fixture
def loopback_client(ready_app):
    """A TestClient whose request.client.host is loopback, so the no-token auth gate
    treats it as the local demo (allowed) rather than failing closed (403)."""
    from fastapi.testclient import TestClient

    return TestClient(ready_app.app, client=("127.0.0.1", 50000))


def _dns_available():
    try:
        socket.getaddrinfo("api.openai.com", 443, proto=socket.IPPROTO_TCP)
        return True
    except Exception:
        return False


HAS_DNS = _dns_available()
needs_dns = pytest.mark.skipif(not HAS_DNS, reason="public-host SSRF cases need real DNS (offline)")
