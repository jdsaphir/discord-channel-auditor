# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file, interactive Python CLI (`discord_activity_checker.py`) that lists every member who posted a message or added a reaction in one Discord channel between two dates, then writes them to a CSV. It uses only the standard library (`urllib`, no `discord.py`/`requests`) and needs Python 3.10+ (uses `X | None` type hints).

## Running

```bash
python discord_activity_checker.py
```

- Reads `config.json` (`channel_id`, `guild_id`, `token`) from the **current working directory**, so run it from the repo root. Any keys in `config.json.local` override it. Values starting with `YOUR_` are treated as unset.
- `config.json` is a tracked template with placeholders. Real values go in the gitignored `config.json.local`. Never put a real token in `config.json`.
- The script prompts for start/end dates on stdin (`YYYY-MM-DD`, or `MM/DD/YYYY` / `DD/MM/YYYY` with a confirmation prompt), and ends with `input("Press Enter to close …")`. It can't run unattended without piping stdin.
- The bot needs permission to read message history and reactions in the channel, plus guild member lookup (`GET /guilds/{id}/members/{user}`).

There are no tests, linter config, or dependencies to install.

## How it works

Everything goes through `main()`:

1. **Date → snowflake**: dates are converted to Discord snowflakes (`datetime_to_snowflake`) so the range can be passed as `after`/`before` message cursors. End date is widened to 23:59:59 UTC; all dates are UTC.
2. **`fetch_messages_in_range`**: pages forward with `after=<cursor>`, 100 at a time, and stops once a page contains messages past the end snowflake or is short.
3. **`collect_active_user_ids`**: adds each message author, then calls the reactions endpoint for every emoji on every message (`fetch_reaction_users`, which pages as well). Custom emoji are encoded as `name:id`. This is the slow, rate-limited part.
4. **`enrich_with_member_info`**: one member lookup per unique user ID. A 404 means the user left the server and becomes a `[left server]` row. `display_name` is the first non-empty of server nick, global name, and username.
5. Writes `active_users_<channel>_<start>_<end>.csv`.

### Checkpoint/resume

Phase 3 saves `checkpoint_<channel>_<start>_<end>.json` every `CHECKPOINT_INTERVAL` (50) messages, holding the last processed message ID and the active ID set. The write is atomic (`.tmp` then `os.replace`). On a rerun with the same channel and dates, the message list is **re-fetched in full** and processing resumes after that message ID. The checkpoint is deleted when the run completes. Output CSVs and checkpoints are gitignored.

### HTTP handling (`api_get`)

- 429: sleeps for the `retry_after` value in the response body, then retries.
- 5xx: exponential backoff from 5s, capped at 60s.
- 401/403: exits via `sys.exit` with a message.
- Anything else (e.g. 404): re-raises `HTTPError` for the caller to handle.

`RETRYABLE_CODES` is defined but not used. The status codes are hardcoded in the branches.
