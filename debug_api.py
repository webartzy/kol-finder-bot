import asyncio
import json
import httpx

API_KEY = "new1_a4a461203a614802a34988202529b90e"
BASE = "https://api.twitterapi.io"
HEADERS = {"X-API-Key": API_KEY}
TEST_USER = "orangie"


async def get(client: httpx.AsyncClient, url: str, params: dict) -> dict | None:
    req = client.build_request("GET", url, params=params, headers=HEADERS)
    print(f"\n>>> GET {req.url}")
    try:
        r = await client.send(req)
        print(f"    Status: {r.status_code}")
        try:
            data = r.json()
            print(f"    Response:\n{json.dumps(data, indent=2)[:2000]}")
            return data
        except Exception:
            print(f"    Raw text: {r.text[:500]}")
    except Exception as e:
        print(f"    ERROR: {e}")
    return None


async def main() -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        print("=" * 60)
        print("STEP 1: Confirm user exists via /user/info")
        print("=" * 60)
        await get(client, f"{BASE}/twitter/user/info", {"userName": TEST_USER})

        print("\n" + "=" * 60)
        print("STEP 2: Try /twitter/user/following")
        print("=" * 60)
        await get(client, f"{BASE}/twitter/user/following", {"userName": TEST_USER, "count": 5})

        print("\n" + "=" * 60)
        print("STEP 3: Try /twitter/user/followings (plural)")
        print("=" * 60)
        await get(client, f"{BASE}/twitter/user/followings", {"userName": TEST_USER, "count": 5})

        print("\n" + "=" * 60)
        print("STEP 4: Try /twitter/user/following with userId instead")
        print("(re-reading user/info to grab the id first)")
        print("=" * 60)
        try:
            r = await client.get(
                f"{BASE}/twitter/user/info",
                headers=HEADERS,
                params={"userName": TEST_USER},
            )
            info = r.json()
            user = info.get("data") if isinstance(info.get("data"), dict) else info
            user_id = user.get("id") or user.get("userId") or user.get("user_id") or user.get("rest_id")
            print(f"    Detected id field -> {user_id!r}")
            if user_id:
                await get(client, f"{BASE}/twitter/user/following", {"userId": user_id, "count": 5})
                await get(client, f"{BASE}/twitter/user/followings", {"userId": user_id, "count": 5})
            else:
                print("    Could not extract userId from /user/info response")
        except Exception as e:
            print(f"    ERROR fetching user info for id: {e}")


if __name__ == "__main__":
    asyncio.run(main())
