# Discord Channel Activity Checker

Lists everyone who sent a message or added a reaction in a Discord channel between two dates, and saves the list to a CSV.

It's a single Python script with no dependencies beyond the standard library.

## Requirements

- Python 3.10 or newer
- A Discord bot in the server, with access to the channel you want to check. Its role needs **View Channel** and **Read Message History** there.

## Setup

1. Create `config.json.local` next to the script with your bot token and the IDs of the channel and server:

   ```json
   {
       "channel_id": "123456789012345678",
       "guild_id":   "123456789012345678",
       "token":      "your-bot-token"
   }
   ```

   `config.json.local` is gitignored. `config.json` is only a template with placeholder values, so don't put real values there. Any key in `config.json.local` overrides the same key in `config.json`.

2. To get channel and server IDs, turn on **Developer Mode** in Discord (User Settings → Advanced), then right-click the channel or server and choose **Copy ID**.

## Usage

Run it from the folder that contains the config files:

```bash
python discord_activity_checker.py
```

It asks for a start and end date. Both dates are inclusive and interpreted as UTC. Enter them as `YYYY-MM-DD`. `MM/DD/YYYY` and `DD/MM/YYYY` also work, and the script asks you to confirm how it read them.

The script then:

1. Fetches every message in the channel within the date range.
2. Records each message's author and every user who reacted to it.
3. Looks up each user's username, global display name and server nickname.
4. Prints the results and saves them to `active_users_<channel_id>_<start>_<end>.csv`.

### Output columns

| Column | Meaning |
| --- | --- |
| `user_id` | Discord user ID |
| `username` | Account username |
| `global_name` | Global display name, if set |
| `server_nick` | Nickname in this server, if set |
| `display_name` | Server nickname, else global name, else username |

Users who have since left the server are listed as `[left server]`.

## Resuming an interrupted run

Checking reactions takes the most time on busy channels. While it runs, the script saves progress every 50 messages to `checkpoint_<channel_id>_<start>_<end>.json`. If the run stops partway, run it again with the same dates. It re-fetches the message list and continues where it left off. The checkpoint is deleted when a run finishes.

When Discord rate-limits the script, it waits and retries automatically, and it retries Discord server errors with increasing delays.

## Troubleshooting

- **`401 Unauthorized`**: the bot token is wrong.
- **`403 Forbidden`**: the bot can't read the channel, its message history, its reactions, or server members. Check the bot's role permissions for that channel.
- **`Missing or placeholder value for '…'`**: that key isn't set in `config.json.local`.
