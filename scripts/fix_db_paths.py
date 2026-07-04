"""Repair absolute DB paths inside cognee_db after relocating a snapshot.

cognee 1.1.3 stores an ABSOLUTE `vector_database_url` (and sometimes
`graph_database_url`) per dataset in the `dataset_database` table. Restore the
golden snapshot onto a different machine/path (e.g. into a Docker image) and
those URLs still point at the ORIGINAL machine's home dir — lancedb then quietly
creates a fresh EMPTY store at the phantom path and every answer degrades to
"not documented" (found by the v4 deploy portability probe, 2026-07-04).

This rewrites any stored `.../databases/<rest>` URL onto the CURRENT
SYSTEM_ROOT_DIRECTORY, and prints a receipt of every change. Idempotent; safe to
run at every image build, right after reset_demo.py.
"""
import os, sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except ImportError:
    pass

SYSTEM = os.environ["SYSTEM_ROOT_DIRECTORY"]
DB = os.path.join(SYSTEM, "databases", "cognee_db")
MARKER = "/databases/"


def _relocate(url: str) -> str | None:
    """Map any absolute .../databases/<rest> URL onto the current system root."""
    if not url:
        return None
    i = url.find(MARKER)
    if i == -1:
        return None
    fixed = os.path.join(SYSTEM, "databases", url[i + len(MARKER):])
    return fixed if fixed != url else None


def main():
    if not os.path.exists(DB):
        raise SystemExit(f"no cognee_db at {DB} — run scripts/reset_demo.py first")
    con = sqlite3.connect(DB)
    cur = con.cursor()
    changed = 0
    rows = cur.execute(
        "SELECT rowid, vector_database_url, graph_database_url FROM dataset_database"
    ).fetchall()
    for rowid, vurl, gurl in rows:
        for col, url in (("vector_database_url", vurl), ("graph_database_url", gurl)):
            fixed = _relocate(url)
            if fixed:
                cur.execute(f"UPDATE dataset_database SET {col}=? WHERE rowid=?", (fixed, rowid))
                print(f"fixed {col}:\n  {url}\n  -> {fixed}")
                changed += 1
    con.commit()
    con.close()
    print(f"{changed} path(s) repaired" if changed else "nothing to repair — all paths already local")


if __name__ == "__main__":
    main()
