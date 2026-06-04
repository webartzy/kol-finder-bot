import asyncio, httpx, json

async def test():
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://api.twitterapi.io/twitter/user/info",
            headers={"X-API-Key": "new1_a4a461203a614802a34988202529b90e"},
            params={"userName": "LangChain"}
        )
        print(json.dumps(r.json(), indent=2))

asyncio.run(test())
