"""Smoke test the WEB layer in-process (no server/port): does the forget beat survive the
/ask + /forget ROUTES and return CLEAN, READABLE answers? Prints full strings — read them.
"""
from fastapi.testclient import TestClient
import sys, os; sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root (this file lives in scripts/)
import app

HERO = "If auth-service latency is high, what should I check, and what's in the auth read path?"
LOOKUP = "What is the legacy-cache and what depends on it?"

with TestClient(app.app) as c:  # entering context triggers startup ingest (~1 min)
    print("health:", c.get("/health").json())
    print("\n[BEFORE /ask hero]:\n  ", c.post("/ask", json={"query": HERO}).json()["answer"])
    print("\n[/forget]:", c.post("/forget", json={"system": "legacy-cache"}).json()["message"])
    print("\n[AFTER /ask hero]:\n  ", c.post("/ask", json={"query": HERO}).json()["answer"])
    print("\n[AFTER /ask name-lookup]:\n  ", c.post("/ask", json={"query": LOOKUP}).json()["answer"])
