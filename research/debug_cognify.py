import os, asyncio, warnings
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"
warnings.filterwarnings("ignore")
import cognee

async def main():
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)
    await cognee.add("The AuthService depends on the eu-west Redis cache. Sana maintains the Redis cache.")
    print(">>> calling cognify ...")
    info = await cognee.cognify()
    print(">>> COGNIFY RESULT:", info)

asyncio.run(main())
