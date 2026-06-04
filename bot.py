import asyncio
import json
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
import httpx

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = "8676881479:AAHJ7ATaSOm78m4TSqR7TmufcfWgeLNkc1U"
TWITTER_API_KEY = "new1_a4a461203a614802a34988202529b90e"
TWITTER_BASE_URL = "https://api.twitterapi.io"
HEADERS = {"X-API-Key": TWITTER_API_KEY}

# {chat_id: {"results": [...], "offset": int, "label": str}}
chat_state: dict[int, dict] = {}

MORE_TRIGGERS = {"more", "next", "10 more", "next 10", "show more", "load more"}
SPAM_WORDS = {"airdrop", "giveaway", "shill"}
EXEC_WORDS = {"ceo", "cto", "coo", "cfo", "founder", "co-founder", "cofounder", "president", "chairman"}

TWITTER_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{1,50})(?:[/?#].*)?',
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{1,50}$')

START_TEXT = (
    "KOL Finder\n\n"
    "Mode 1 - Network Discovery\n"
    "Send comma-separated usernames or Twitter/X links:\n"
    "  nabu_lines, Drewfromweb3, orangie\n"
    "  https://x.com/nabu_lines, https://x.com/orangie\n\n"
    "Mode 2 - Smart Search\n"
    "Send a plain text description:\n"
    "  crypto influencers\n"
    "  AI developers\n"
    "  fitness coaches\n\n"
    'Send "more" or "next" for the next 10 results.'
)


# ---------------------------------------------------------------------------
# Input classification
# ---------------------------------------------------------------------------

def extract_usernames(text: str) -> list[str] | None:
    """Return deduplicated username list if input looks like seed accounts, else None."""
    # Matches with or without https:// prefix
    url_matches = TWITTER_URL_RE.findall(text)
    if url_matches:
        seen: set[str] = set()
        result: list[str] = []
        for u in url_matches:
            low = u.lower()
            if low not in seen:
                seen.add(low)
                result.append(u)
        return result

    # Comma-separated usernames (2 or more)
    parts = [p.strip().lstrip("@") for p in text.split(",")]
    parts = [p for p in parts if p and USERNAME_RE.match(p)]
    if len(parts) >= 2:
        return parts

    # Single @username
    stripped = text.strip()
    if stripped.startswith("@") and USERNAME_RE.match(stripped[1:]):
        return [stripped[1:]]

    return None


def is_spam(bio: str) -> bool:
    bio_lower = bio.lower()
    return any(word in bio_lower for word in SPAM_WORDS) or any(word in bio_lower for word in EXEC_WORDS)


def is_org_account(user: dict) -> bool:
    return (user.get("verifiedType") or "").lower() == "business"


# ---------------------------------------------------------------------------
# Twitter API helpers
# ---------------------------------------------------------------------------

def _username_from_user(u: dict) -> str:
    return (u.get("screen_name") or u.get("username") or u.get("userName") or "").lower()


def _followers_from_user(u: dict) -> int:
    # /followings endpoint uses "followers_count"; /user/info uses "followers"
    return u.get("followers_count") or u.get("followersCount") or u.get("followers") or 0


async def fetch_following(username: str, client: httpx.AsyncClient) -> list[dict]:
    """Return list of full user objects that `username` follows."""
    try:
        r = await client.get(
            f"{TWITTER_BASE_URL}/twitter/user/followings",
            headers=HEADERS,
            params={"userName": username, "count": 100},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        users = data.get("followings") or []
        print(f"[DEBUG] followings({username}): {len(users)} accounts")
        return users
    except Exception as e:
        print(f"[DEBUG] followings({username}) error: {e}")
        return []


async def fetch_user_info(username: str, client: httpx.AsyncClient) -> dict | None:
    try:
        r = await client.get(
            f"{TWITTER_BASE_URL}/twitter/user/info",
            headers=HEADERS,
            params={"userName": username},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        user = data.get("data") if isinstance(data.get("data"), dict) else data
        print(f"[DEBUG] info({username}): followers={user.get('followers_count', '?')}")
        return user
    except Exception as e:
        print(f"[DEBUG] info({username}) error: {e}")
        return None


def _parse_tweet_date(s: str) -> datetime | None:
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%a %b %d %H:%M:%S +0000 %Y",
    ):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


async def is_recently_active(username: str, client: httpx.AsyncClient) -> bool:
    """Return True if the account tweeted in the last 30 days. On API error, assume active."""
    try:
        r = await client.get(
            f"{TWITTER_BASE_URL}/twitter/tweet/advanced_search",
            headers=HEADERS,
            params={"query": f"from:{username}", "queryType": "Latest", "count": 1},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        tweets = data.get("tweets") or data.get("data", {}).get("tweets") or []
        if not tweets:
            return False
        created = tweets[0].get("createdAt") or tweets[0].get("created_at") or ""
        dt = _parse_tweet_date(created)
        if dt is None:
            return True  # unparseable date → assume active
        return dt >= datetime.now(timezone.utc) - timedelta(days=30)
    except Exception as e:
        print(f"[DEBUG] is_recently_active({username}) error: {e}")
        return True  # API failure → don't exclude


async def tweet_search(query: str) -> list[dict]:
    """Advanced tweet search; returns filtered, sorted author list."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"{TWITTER_BASE_URL}/twitter/tweet/advanced_search",
                headers=HEADERS,
                params={"query": query, "queryType": "Top", "count": 50},
            )
            print(f"[DEBUG] tweet_search {r.url} -> {r.status_code}")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        print(f"[DEBUG] tweet_search error: {e}")
        return []

    tweets = data.get("tweets") or data.get("data", {}).get("tweets") or []
    seen: set[str] = set()
    authors: list[dict] = []

    for tweet in tweets:
        author = tweet.get("author") or tweet.get("user") or {}
        if not author:
            continue
        username = (author.get("userName") or author.get("screen_name") or author.get("username") or "").lower()
        if not username or username in seen:
            continue
        seen.add(username)

        if any(k for k in author if 'verif' in k.lower() or 'badge' in k.lower()):
            print(f"[DEBUG VERIFIED] {author.get('name')}: {json.dumps({k:v for k,v in author.items() if 'verif' in k.lower() or 'badge' in k.lower() or 'type' in k.lower()})}")

        if is_org_account(author):
            continue

        followers = author.get("followers_count") or author.get("followersCount") or 0
        if followers <= 10000:
            continue

        bio = author.get("description") or author.get("bio") or ""
        if is_spam(bio):
            continue

        authors.append({
            "name": author.get("name") or "N/A",
            "username": author.get("userName") or author.get("screen_name") or author.get("username") or "N/A",
            "followers_count": followers,
        })

    authors.sort(key=lambda u: u["followers_count"], reverse=True)
    print(f"[DEBUG] tweet_search: {len(authors)} authors after filter")
    return authors


# ---------------------------------------------------------------------------
# Network discovery
# ---------------------------------------------------------------------------

async def discover_from_seeds(seeds: list[str]) -> list[dict]:
    seed_lower = {s.lower() for s in seeds}

    async with httpx.AsyncClient(timeout=20) as client:
        following_lists = await asyncio.gather(*[fetch_following(s, client) for s in seeds])

        # Build: username -> user dict, count appearances across seed following lists
        user_by_name: dict[str, dict] = {}
        follow_counter: Counter = Counter()

        for following in following_lists:
            seen_in_list: set[str] = set()
            for u in following:
                name = _username_from_user(u)
                if not name or name in seed_lower:
                    continue
                if any(k for k in u if 'verif' in k.lower() or 'badge' in k.lower()):
                    print(f"[DEBUG VERIFIED] {u.get('name')}: {json.dumps({k:v for k,v in u.items() if 'verif' in k.lower() or 'badge' in k.lower() or 'type' in k.lower()})}")
                user_by_name[name] = u
                if name not in seen_in_list:
                    seen_in_list.add(name)
                    follow_counter[name] += 1

        if len(seeds) >= 2:
            candidates = [n for n, c in follow_counter.items() if c >= 2]
            print(f"[DEBUG] {len(candidates)} candidates in 2+ following lists")
        else:
            candidates = list(follow_counter.keys())
            print(f"[DEBUG] {len(candidates)} candidates from single seed")

        if not candidates:
            return []

        # Filter: 10k < followers <= 500k
        # Use follower count from following response; fetch_user_info as fallback when field is 0/missing
        follower_filtered: list[dict] = []
        need_fetch: list[str] = []

        for name in candidates:
            u = user_by_name[name]
            if is_org_account(u):
                continue
            bio = u.get("description") or u.get("bio") or ""
            if is_spam(bio):
                continue
            followers = _followers_from_user(u)
            if 10_000 < followers <= 500_000:
                follower_filtered.append(u)
            elif followers == 0:
                need_fetch.append(name)

        if need_fetch:
            sem = asyncio.Semaphore(8)

            async def guarded(username: str) -> dict | None:
                async with sem:
                    return await fetch_user_info(username, client)

            profiles = await asyncio.gather(*[guarded(n) for n in need_fetch])
            for p in profiles:
                if p:
                    if is_org_account(p):
                        continue
                    bio = p.get("description") or p.get("bio") or ""
                    if not is_spam(bio) and 10_000 < _followers_from_user(p) <= 500_000:
                        follower_filtered.append(p)

        print(f"[DEBUG] {len(follower_filtered)} users passed 10k-500k filter")

        if not follower_filtered:
            return []

        # Activity filter: tweeted in last 30 days
        act_sem = asyncio.Semaphore(5)

        async def check_active(u: dict) -> tuple[dict, bool]:
            name = _username_from_user(u)
            async with act_sem:
                active = await is_recently_active(name, client)
            return u, active

        activity = await asyncio.gather(*[check_active(u) for u in follower_filtered])
        results = [u for u, active in activity if active]
        print(f"[DEBUG] {len(results)} users passed activity filter")

    results.sort(key=_followers_from_user, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_user(index: int, user: dict) -> str:
    name = user.get("name") or "N/A"
    username = _username_from_user(user) or "N/A"
    followers = _followers_from_user(user)
    return (
        f"{index}. {name}\n"
        f"@{username} -> https://x.com/{username}\n"
        f"Followers: {followers:,}"
    )


def render_page(results: list[dict], offset: int, label: str) -> str:
    page = results[offset:offset + 10]
    lines = [f"Results {offset + 1}-{offset + len(page)} for: {label}"]
    for i, user in enumerate(page, offset + 1):
        lines.append("")
        lines.append(format_user(i, user))
    if offset + len(page) < len(results):
        lines.append("")
        lines.append('Send "more" or "next" for the next 10 results.')
    return "\n".join(lines)


def render_all_messages(results: list[dict], label: str) -> list[str]:
    """Return list of message strings (each under 3800 chars) covering all results."""
    header = f"Found {len(results)} accounts for: {label}\n"
    messages: list[str] = []
    current = header

    for i, user in enumerate(results, 1):
        entry = "\n" + format_user(i, user) + "\n"
        if len(current) + len(entry) > 3800:
            messages.append(current.rstrip())
            current = entry
        else:
            current += entry

    if current.strip():
        messages.append(current.rstrip())

    return messages


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # More / pagination
    if text.lower() in MORE_TRIGGERS:
        state = chat_state.get(chat_id)
        if not state:
            await update.message.reply_text("No previous search. Send usernames or a description first.")
            return
        offset = state["offset"]
        if offset >= len(state["results"]):
            await update.message.reply_text("No more results.")
            return
        state["offset"] = offset + 10
        await update.message.reply_text(render_page(state["results"], offset, state["label"]))
        return

    # Mode 1 - Network Discovery
    seeds = extract_usernames(text)
    if seeds:
        msg = await update.message.reply_text(
            f"Fetching following lists for {len(seeds)} account(s).\n"
            "Checking followers and activity... this may take 20-40 seconds."
        )
        try:
            results = await discover_from_seeds(seeds)
        except Exception as e:
            await msg.edit_text(f"Error during network analysis: {e}")
            return

        if not results:
            await msg.edit_text(
                "No results found. No followed accounts matched the filters "
                "(10k-500k followers, active in last 30 days).\n"
                "Try different seed accounts."
            )
            return

        label = ", ".join(seeds)
        pages = render_all_messages(results, label)
        await msg.edit_text(pages[0])
        for page in pages[1:]:
            await update.message.reply_text(page)
        return

    # Mode 2 - Smart Search
    msg = await update.message.reply_text("Searching tweets...")
    try:
        results = await tweet_search(text)
    except Exception as e:
        await msg.edit_text(f"Error during search: {e}")
        return

    if not results:
        await msg.edit_text("No results found. Try different keywords.")
        return

    chat_state[chat_id] = {"results": results, "offset": 10, "label": text}
    await msg.edit_text(render_page(results, 0, text))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Bot is running... Press Ctrl+C to stop.")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")
