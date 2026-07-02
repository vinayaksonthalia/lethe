"""SMOKE TEST: does cognee.forget() cleanly remove ONE system's influence?

The hero ("memory that forgets the stale stuff") rests on this and it's NEVER been tested.
Not a benchmark — just: ingest 4 systems -> query (redis surfaces) -> forget redis ->
re-query -> is redis GONE, and do the OTHER systems still answer (forget didn't nuke the graph)?

Reports plainly: does forget DETERMINISTICALLY remove a system's influence? yes/no + before/after.
Model = OpenRouter-llama (from .env, Groq tapped). temp 0.
"""
import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
os.environ["LOG_LEVEL"] = "ERROR"; os.environ["COGNEE_LOG_FILE"] = "false"
os.environ["CACHING"] = "false"  # rule out cached query answers masking forget()
warnings.filterwarnings("ignore")
import cognee
from cognee import SearchType
from cognee.modules.users.methods import get_default_user

DOCS = {
    "redis":  "The redis-cache cluster stores all user sessions for the AuthService. If the redis-cache fails, user logins break across the platform.",
    "search": "The search-index service powers product search on the storefront. It is owned by the discovery team.",
    "email":  "The email-relay service sends all transactional emails such as receipts. It is owned by the communications team.",
    "cdn":    "The image-cdn service serves product images to users worldwide. It is owned by the edge team.",
}
Q_REDIS = "Tell me about the redis-cache: what is it and what depends on it?"
Q_EMAIL = "Tell me about the email-relay service: what is it and who owns it?"

def data_id_of(add_result):
    info = getattr(add_result, "data_ingestion_info", None)
    if info and isinstance(info, list) and isinstance(info[0], dict):
        return info[0].get("data_id")
    return None

def flat(x):
    if isinstance(x, list): return " || ".join(flat(i) for i in x)
    if isinstance(x, dict): return flat(x.get("search_result", x)) if "search_result" in x else str(x)
    return str(x)

async def ask(q, user):
    return flat(await cognee.recall(query_text=q, query_type=SearchType.GRAPH_COMPLETION, user=user))

async def main():
    await cognee.prune.prune_data(); await cognee.prune.prune_system(metadata=True)
    ids = {}
    for name, text in DOCS.items():
        r = await cognee.add(text)
        ids[name] = data_id_of(r)
    print("data_ids:", {k: str(v)[:8] for k, v in ids.items()})
    await cognee.cognify()
    user = await get_default_user()

    print("\n=== BEFORE forget ===")
    b_redis = await ask(Q_REDIS, user)
    b_email = await ask(Q_EMAIL, user)
    print("  Q(redis):", b_redis[:160], "\n    -> mentions redis?", "redis" in b_redis.lower())
    print("  Q(email):", b_email[:160], "\n    -> mentions email?", "email" in b_email.lower())

    print("\n=== forget(redis data_id) ===")
    res = await cognee.forget(data_id=ids["redis"], dataset="main_dataset")
    print("  forget returned:", str(res)[:160])

    print("\n=== AFTER forget ===")
    a_redis = await ask(Q_REDIS, user)
    a_email = await ask(Q_EMAIL, user)
    print("  Q(redis):", a_redis[:160], "\n    -> still mentions redis?", "redis" in a_redis.lower())
    print("  Q(email):", a_email[:160], "\n    -> still mentions email?", "email" in a_email.lower())

    print("\n" + "=" * 60)
    redis_gone = ("redis" in b_redis.lower()) and ("redis" not in a_redis.lower())
    email_kept = "email" in a_email.lower()
    print(f"  redis surfaced before & GONE after: {redis_gone}")
    print(f"  email still works after (graph not nuked): {email_kept}")
    if redis_gone and email_kept:
        print(">>> forget() WORKS cleanly: deterministically removed the system, kept the rest. Hero is real.")
    elif not redis_gone:
        print(">>> forget() did NOT remove redis's influence — hero is at risk. Investigate.")
    else:
        print(">>> forget() removed redis but also broke unrelated answers — too blunt. Investigate.")
    print("=" * 60)

asyncio.run(main())
