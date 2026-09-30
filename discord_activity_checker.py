"""
Discord Channel Activity Checker
Extracts user IDs, usernames, and display names of members who sent a message
or reacted in a channel between two dates.

Configuration is read from config.json (channel_id, guild_id, token), with
values in the gitignored config.json.local taking precedence.
Supports checkpoint/resume: if the run is interrupted, restart with the same
dates and it will continue from where it left off.
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_FILE = "config.json"
# Gitignored; values here override CONFIG_FILE. Put the real secrets here.
LOCAL_CONFIG_FILE = "config.json.local"


def read_json_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        sys.exit(f"[ERROR] {path} is not valid JSON: {exc}")

    if not isinstance(data, dict):
        sys.exit(f"[ERROR] {path} must contain a JSON object.")
    return data


def load_config() -> dict:
    cfg = read_json_file(CONFIG_FILE)
    cfg.update(read_json_file(LOCAL_CONFIG_FILE))

    for key in ("channel_id", "guild_id", "token"):
        if not cfg.get(key) or cfg[key].startswith("YOUR_"):
            sys.exit(
                f"[ERROR] Missing or placeholder value for '{key}'. "
                f"Set it in {LOCAL_CONFIG_FILE}."
            )

    return cfg


# ---------------------------------------------------------------------------
# Discord snowflake helpers
# ---------------------------------------------------------------------------
DISCORD_EPOCH_MS = 1420070400000  # 2015-01-01T00:00:00Z in milliseconds


def datetime_to_snowflake(dt: datetime) -> int:
    ms = int(dt.timestamp() * 1000)
    return (ms - DISCORD_EPOCH_MS) << 22


def snowflake_to_datetime(snowflake: int) -> datetime:
    ms = (snowflake >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only – no third-party deps required)
# ---------------------------------------------------------------------------
BASE = "https://discord.com/api/v10"
# Transient HTTP errors that are safe to retry
RETRYABLE_CODES = {429, 500, 502, 503, 504}


def api_get(path: str, token: str, params: dict | None = None) -> list | dict:
    """GET request against the Discord REST API with automatic retry handling."""
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())

    headers = {
        "Authorization": f"Bot {token}",
        "User-Agent": "DiscordActivityChecker/1.0",
    }

    backoff = 5  # seconds to wait on 5xx errors before retrying

    while True:
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                body = json.loads(exc.read())
                retry_after = float(body.get("retry_after", 1))
                print(f"  [rate-limit] waiting {retry_after:.1f}s …", flush=True)
                time.sleep(retry_after + 0.1)
            elif exc.code in (500, 502, 503, 504):
                print(
                    f"  [HTTP {exc.code}] Discord server error – retrying in {backoff}s …",
                    flush=True,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)  # exponential backoff, cap at 60s
            elif exc.code == 403:
                sys.exit(
                    "\n[ERROR] 403 Forbidden – the bot lacks permission to read "
                    "this channel/reactions or guild members. Check the bot's role."
                )
            elif exc.code == 401:
                sys.exit("\n[ERROR] 401 Unauthorized – invalid bot token.")
            else:
                raise


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------
def checkpoint_path(channel_id: str, start: datetime, end: datetime) -> str:
    return f"checkpoint_{channel_id}_{start.date()}_{end.date()}.json"


def load_checkpoint(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_checkpoint(path: str, last_message_id: str, active_ids: set[str]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_message_id": last_message_id, "active_ids": list(active_ids)}, f)
    os.replace(tmp, path)  # atomic on all major platforms


CHECKPOINT_INTERVAL = 50  # save every N messages processed


# ---------------------------------------------------------------------------
# Message + reaction collection
# ---------------------------------------------------------------------------
def fetch_messages_in_range(
    channel_id: str,
    token: str,
    after_snowflake: int,
    before_snowflake: int,
) -> list[dict]:
    messages = []
    seen_ids: set[str] = set()
    cursor = after_snowflake
    page = 0

    print("\nFetching messages …")

    while True:
        page += 1
        batch = api_get(
            f"/channels/{channel_id}/messages",
            token,
            params={"after": str(cursor), "limit": "100"},
        )

        if not batch:
            break

        new_cursor = max(int(m["id"]) for m in batch)
        if new_cursor <= cursor:
            # Cursor didn't advance — stop to avoid an infinite loop.
            break

        in_range = [m for m in batch if int(m["id"]) <= before_snowflake]
        for msg in in_range:
            if msg["id"] not in seen_ids:
                seen_ids.add(msg["id"])
                messages.append(msg)

        print(
            f"  page {page:3d} – {len(in_range):3d} msgs ({len(messages)} total) – "
            f"up to {snowflake_to_datetime(new_cursor).strftime('%Y-%m-%d %H:%M UTC')}",
            flush=True,
        )

        if len(in_range) < len(batch):
            # Some messages were beyond the end date — we're done.
            break

        if len(batch) < 100:
            break

        cursor = new_cursor

    return messages


def fetch_reaction_users(
    channel_id: str,
    message_id: str,
    emoji: str,
    token: str,
) -> list[str]:
    """Return all user IDs who used a specific emoji reaction on a message."""
    user_ids = []
    cursor = None

    while True:
        params: dict = {"limit": "100", "type": "0"}
        if cursor:
            params["after"] = cursor

        emoji_encoded = urllib.parse.quote(emoji, safe=":")
        batch = api_get(
            f"/channels/{channel_id}/messages/{message_id}/reactions/{emoji_encoded}",
            token,
            params,
        )

        if not batch:
            break

        for user in batch:
            user_ids.append(user["id"])

        if len(batch) < 100:
            break

        cursor = batch[-1]["id"]

    return user_ids


def collect_active_user_ids(
    channel_id: str,
    token: str,
    after_sf: int,
    before_sf: int,
    ckpt_path: str,
) -> set[str]:
    messages = fetch_messages_in_range(channel_id, token, after_sf, before_sf)

    if not messages:
        print("No messages found in the specified range.")
        return set()

    total = len(messages)

    # --- resume from checkpoint if available ---
    active: set[str] = set()
    start_index = 0
    ckpt = load_checkpoint(ckpt_path)
    if ckpt:
        active = set(ckpt["active_ids"])
        last_id = ckpt["last_message_id"]
        # find the message after the last processed one
        for i, msg in enumerate(messages):
            if msg["id"] == last_id:
                start_index = i + 1
                break
        if start_index:
            print(
                f"\nResuming from checkpoint — {start_index} message(s) already processed, "
                f"{len(active)} unique user(s) found so far.\n"
            )
        else:
            print("\nCheckpoint found but last message not in range — starting fresh.\n")
            active = set()
    else:
        print(f"\n{total} message(s) found. Collecting authors and reactions …\n")

    for i, msg in enumerate(messages[start_index:], start=start_index + 1):
        active.add(msg["author"]["id"])

        reactions = msg.get("reactions", [])
        for reaction in reactions:
            emoji_data = reaction["emoji"]
            emoji_str = (
                f"{emoji_data['name']}:{emoji_data['id']}"
                if emoji_data.get("id")
                else emoji_data["name"]
            )
            active.update(fetch_reaction_users(channel_id, msg["id"], emoji_str, token))

        if reactions:
            print(
                f"  [{i:4d}/{total}] msg {msg['id']} – "
                f"author collected, {len(reactions)} reaction type(s) fetched",
                flush=True,
            )

        if i % CHECKPOINT_INTERVAL == 0:
            save_checkpoint(ckpt_path, msg["id"], active)

    # run complete — remove checkpoint
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)

    return active


# ---------------------------------------------------------------------------
# Member info enrichment
# ---------------------------------------------------------------------------
def fetch_member_info(guild_id: str, user_id: str, token: str) -> dict:
    try:
        member = api_get(f"/guilds/{guild_id}/members/{user_id}", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "user_id":      user_id,
                "username":     "[left server]",
                "global_name":  "",
                "server_nick":  "",
                "display_name": "[left server]",
            }
        raise

    user = member.get("user", {})
    username    = user.get("username", "")
    global_name = user.get("global_name") or ""
    server_nick = member.get("nick") or ""
    display_name = server_nick or global_name or username

    return {
        "user_id":      user_id,
        "username":     username,
        "global_name":  global_name,
        "server_nick":  server_nick,
        "display_name": display_name,
    }


def enrich_with_member_info(
    user_ids: set[str],
    guild_id: str,
    token: str,
) -> list[dict]:
    total = len(user_ids)
    print(f"\nFetching member info for {total} user(s) …\n")

    results = []
    for i, uid in enumerate(sorted(user_ids), 1):
        info = fetch_member_info(guild_id, uid, token)
        results.append(info)
        print(
            f"  [{i:4d}/{total}] {uid}  {info['username']:<30}  {info['display_name']}",
            flush=True,
        )

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_date(prompt: str) -> datetime:
    while True:
        raw = input(prompt).strip()
        try:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                confirm = input(f"  Interpreted as {dt.strftime('%B %d, %Y')} — correct? [Y/n]: ").strip().lower()
                if confirm in ("", "y", "yes"):
                    return dt
                break  # user said no — ask again
            except ValueError:
                pass
        else:
            print("  Invalid format. Use YYYY-MM-DD or MM/DD/YYYY.")


def main():
    print("=" * 60)
    print("  Discord Channel Activity Checker")
    print("=" * 60)

    cfg = load_config()
    channel_id = cfg["channel_id"]
    guild_id   = cfg["guild_id"]
    token      = cfg["token"]

    print(f"Channel : {channel_id}")
    print(f"Guild   : {guild_id}\n")

    start = parse_date("Start date (inclusive)  [YYYY-MM-DD]: ")
    end   = parse_date("End date   (inclusive)  [YYYY-MM-DD]: ")
    end   = end.replace(hour=23, minute=59, second=59)

    if end < start:
        sys.exit("[ERROR] End date must be after start date.")

    print(f"\nDate range : {start.date()} → {end.date()}")

    after_sf  = datetime_to_snowflake(start)
    before_sf = datetime_to_snowflake(end)
    ckpt_path = checkpoint_path(channel_id, start, end)

    active_ids = collect_active_user_ids(
        channel_id, token, after_sf, before_sf, ckpt_path
    )

    if not active_ids:
        return

    members = enrich_with_member_info(active_ids, guild_id, token)
    members.sort(key=lambda m: m["username"].lower())

    print("\n" + "=" * 60)
    print(f"  {len(members)} unique active user(s) found")
    print("=" * 60)

    header = f"{'User ID':<20}  {'Username':<30}  {'Display Name'}"
    print(f"\n{header}")
    print("-" * len(header))
    for m in members:
        print(f"  {m['user_id']:<20}  {m['username']:<30}  {m['display_name']}")

    out_file = f"active_users_{channel_id}_{start.date()}_{end.date()}.csv"
    with open(out_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["user_id", "username", "global_name", "server_nick", "display_name"],
        )
        writer.writeheader()
        writer.writerows(members)

    print(f"\nResults saved to: {out_file}")
    print("Note: users who left the server since posting appear as [left server].")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[UNEXPECTED ERROR] {exc}")
    finally:
        input("\nPress Enter to close …")
