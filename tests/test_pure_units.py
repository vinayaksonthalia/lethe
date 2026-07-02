"""Pure-unit tests — load-bearing functions + the new security guards.

No mocks needed: every function here is deterministic and side-effect-free (the
SSRF guard does real DNS, but literal-IP cases need no network; public-host cases
are guarded by `needs_dns`).
"""
import re

import pytest

import app as appmod
import incident_brain as ib
from conftest import needs_dns


# ---------------------------------------------------------------------------
# 1. _validate_llm_endpoint — the SSRF guard (app.py ~206)
# ---------------------------------------------------------------------------

# Literal loopback IPs / localhost need NO DNS (getaddrinfo on a literal address
# does not hit the network), so these run even offline.
@pytest.mark.parametrize("url", [
    "http://localhost:11434",
    "http://127.0.0.1:11434",
])
def test_ssrf_allows_literal_localhost(url):
    ok, why = appmod._validate_llm_endpoint(url)
    assert ok, why


# Literal private / link-local IPs are rejected with NO DNS needed.
@pytest.mark.parametrize("url,bad_ip", [
    ("http://169.254.169.254/", "169.254.169.254"),  # cloud metadata (link-local)
    ("https://10.0.0.5/", "10.0.0.5"),                 # RFC1918 private
    ("http://192.168.1.1/", "192.168.1.1"),            # RFC1918 private
])
def test_ssrf_rejects_internal_literal_ips(url, bad_ip):
    ok, why = appmod._validate_llm_endpoint(url)
    assert ok is False
    assert bad_ip in why


def test_ssrf_empty_url_allowed():
    # empty = "no override, use provider default" -> allowed
    assert appmod._validate_llm_endpoint("")[0] is True
    assert appmod._validate_llm_endpoint(None)[0] is True


@needs_dns
@pytest.mark.parametrize("url", [
    "https://api.groq.com",
    "https://generativelanguage.googleapis.com",
    "https://api.openai.com",
    "https://openrouter.ai",
])
def test_ssrf_allows_legit_public_providers(url):
    ok, why = appmod._validate_llm_endpoint(url)
    assert ok, f"{url} should be allowed but: {why}"


# ---------------------------------------------------------------------------
# 2. esc() — the client-side XSS escape (app.py ~1251)
#
# NOTE: esc() is a JAVASCRIPT one-liner embedded in the served HTML, NOT a Python
# function, so it cannot be imported/called from Python. We test the load-bearing
# behavior two ways: (a) assert the exact mapping line is present + correct in the
# served page source (so a regression in the real code is caught), and (b) replicate
# the documented mapping in Python and assert a real XSS payload is fully neutralized.
# ---------------------------------------------------------------------------

_ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;", "`": "&#96;"}


def _esc(s):  # faithful port of the JS esc() in app.py
    return re.sub(r"[&<>\"'`]", lambda m: _ESC_MAP[m.group(0)], str(s))


def test_esc_source_line_present_and_correct():
    src = appmod.PAGE
    # The exact escape mapping must be present in the served page (regression canary).
    assert "const esc=" in src
    for ch, ent in _ESC_MAP.items():
        assert ent in src, f"esc() in app.py no longer maps to {ent!r}"


def test_esc_neutralizes_xss_payload():
    payload = 'x"><img src=x onerror=alert(1)>'
    out = _esc(payload)
    assert "<" not in out
    assert ">" not in out
    assert '"' not in out
    assert "&lt;img" in out and "&gt;" in out and "&quot;" in out


# ---------------------------------------------------------------------------
# 3. _safe_wid — workspace-id path-safety (app.py ~92)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wid", ["incidents", "ws_9d414d86"])
def test_safe_wid_accepts_valid(wid):
    assert appmod._safe_wid(wid) == wid


@pytest.mark.parametrize("wid", ["../etc", "ws_BADUPPER", "", "ws_../x"])
def test_safe_wid_rejects_invalid(wid):
    with pytest.raises(ValueError):
        appmod._safe_wid(wid)


# ---------------------------------------------------------------------------
# 4. incident_brain._smalltalk (incident_brain.py ~87)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("q", ["hi", "thanks", "hello there"])
def test_smalltalk_true_for_greetings(q):
    assert ib._smalltalk(q) is not None


def test_smalltalk_false_for_real_question():
    q = "If auth-service latency is high, what should I check?"
    assert ib._smalltalk(q) is None


# ---------------------------------------------------------------------------
# 5. _citations — provenance gating (app.py ~642)
# ---------------------------------------------------------------------------

def _seed_citation_texts():
    return {"tws": {
        "legacy-cache": ["Runbook: legacy-cache (memcached). When auth-service latency is high, "
                         "flush and resize the legacy-cache cluster to recover. It sits in front of "
                         "the auth-service session reads."],
        "search-index": ["Runbook: search-index. The search-index powers product search and is "
                         "independent of login and payments."],
    }}


def test_citations_cites_grounded_doc():
    appmod.S["texts"] = _seed_citation_texts()
    answer = ("When auth-service latency is high, flush and resize the legacy-cache memcached "
              "cluster to recover.")
    cites = appmod._citations("tws", answer)
    assert cites == ["legacy-cache"]  # the search-index doc shares nothing distinctive -> not cited


def test_citations_gates_ungrounded_answer():
    appmod.S["texts"] = _seed_citation_texts()
    answer = "That is not documented in the runbooks."
    assert appmod._citations("tws", answer) == []


def test_citations_empty_when_no_texts():
    appmod.S["texts"] = {"tws": {}}
    assert appmod._citations("tws", "anything at all here") == []


# ---------------------------------------------------------------------------
# 6. _get_ws — fail-closed workspace resolution (app.py ~138)
# ---------------------------------------------------------------------------

def test_get_ws_default_resolves_by_id_not_position():
    # golden 'incidents' deliberately NOT at index 0 — must still resolve to main_dataset.
    appmod.S["workspaces"] = [
        {"id": "ws_9d414d86", "name": "W1", "dataset": "ws_9d414d86"},
        appmod.DEFAULT_WS,
    ]
    for wid in ("incidents", ""):
        ws = appmod._get_ws(wid)
        assert ws["id"] == "incidents"
        assert ws["dataset"] == "main_dataset"


def test_get_ws_unknown_fails_closed():
    appmod.S["workspaces"] = [appmod.DEFAULT_WS]
    # an unknown id must NOT alias to the golden default -> None (fail closed)
    assert appmod._get_ws("ws_doesnotexist") is None
