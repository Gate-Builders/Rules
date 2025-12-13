# Discord Rules

A Discord.py bot that manages a server's rules channel from version-controlled Markdown files.

## Discord setup guide

Here's a quick setup guide for setting up the Discord-authentication and permissions.

### 1) Create the bot application + get the token

1. Go to the Discord Developer Portal and create a **New Application**.
2. In your application, go to **Bot** and create/enable the bot user.
3. Under **Token**, click **Reset Token** (or regenerate) and **copy the token once**.

   * Discord intentionally won't let you copy/view the token again later; if you lose it, you must reset/regenerate it.
4. Save that token somewhere secure (password manager / CI secret). Treat it like a password (don't commit it).

### 2) Invite the bot to your server

1. In the Developer Portal for your application, go to **OAuth2 > URL Generator**.
2. Under **Scopes**, check **bot**.
3. Under **Bot Permissions**, select (minimum for your script):

   * View Channel
   * Read Message History
   * Send Messages
   * Manage Messages
   * Send Messages in Threads (optional)
   * Embed Links (optional)
4. Copy the generated URL, open it in a browser, pick your server, and authorise.

   * You need "Manage Server" permission on that server to add the bot.

## 3) Create/choose a rules channel and grant permissions

* Create a channel like `#rules` (or pick an existing one).
* Ensure the bot can do the minimum set above **in that channel** (either via the bot role permissions or channel overrides).
* Right-click your rules channel > Edit Channel > Permissions
* Grant the bot's role the following permissions: View Channel, Read Message History, Send Messages, Manage Messages.
* "Send Messages in Threads" if the rules channel is a thread.
* "Embed Links" if you want link previews; not required to send normal messages.

## 4) Enable Developer Mode and copy IDs (Guild + Channel)

You'll paste these into `.env` as numbers.

1. Enable **Developer Mode** in Discord:

   * Desktop: User Settings (gear) → **Advanced** → **Developer Mode** ON
2. Copy the **Guild (Server) ID**:

   * Right-click the server icon → **Copy Server ID**
   * This is `DISCORD_GUILD`
3. Copy the **Rules Channel ID**:

   * Right-click the channel → **Copy Channel ID**
   * This is `DISCORD_GUILD_RULES_CHANNEL`

## 5) Create your `.env`

Place `.env` in the repo root **or** `.bot/`:

```env
DISCORD_BOT_TOKEN=...
DISCORD_GUILD=123456789012345678
DISCORD_GUILD_RULES_CHANNEL=123456789012345678

# Optional
BOT_MODE=validate      # validate | dry_run | apply
BOT_MESSAGE_DELAY=1
DISCORD_MENTIONS=no
# BOT_RULES_DIR=.
```

Make sure `.env` is in `.gitignore` (you already have that).

## 6) Run locally (recommended sequence)

From the repo root:

```bash
python -m pip install -r .bot/requirements.txt

# 1) validate: checks config + rule file discovery + markdown sanity, no Discord connect
BOT_MODE=validate python .bot/bot.py

# 2) dry_run: connects, verifies guild/channel/permissions, logs what it would delete/post
BOT_MODE=dry_run python .bot/bot.py

# 3) apply: does the deletions + posts the rules
BOT_MODE=apply python .bot/bot.py
```

## 7) CI setup

In CI, **don't use `.env`**, set secrets as environment variables instead:

* `DISCORD_BOT_TOKEN` (secret)
* `DISCORD_GUILD` (secret or variable)
* `DISCORD_GUILD_RULES_CHANNEL` (secret or variable)

Then run `BOT_MODE=validate` on PRs, and only run `apply` on a manual workflow / protected branch.

If you want, tell me whether you're using GitHub Actions, GitLab CI, or something else, and I'll give you a minimal "validate on PR, apply on manual dispatch" pipeline snippet tailored to it.


## Repository layout

```

repo-root/
Rules.1.intro.md
Rules.2.terms.md
...
.bot/
bot.py
requirements.txt

```

- Rule files live in the repo root by default, the bot assumes the repo root is the parent path to the path where the script is.
- The bot implementation lives in `.bot/`.
- The bot can be run from **any working directory** (including CI); it locates the repo root by resolving the parent of `.bot/`.
- You can override the rules directory with `BOT_RULES_DIR`.

## Rule files

Only files matching this format are considered:

`Rules.<x>.<something>.md`

Where:
- `<x>` is a positive integer (the ordering key)
- `<something>` is a human-readable label (not used by code)
- Files must be sequential with **no gaps** starting at 1 (1,2,3,...)
- Duplicate indices are an error (e.g., `Rules.4.terms.md` and `Rules.4.contact.md`)

Each rule file must:
- be UTF-8
- be <= 2000 characters (Discord message limit)
- pass Markdown sanity checks (code fences, spoilers, link format)

## Configuration

Configuration is loaded from `.env` if present (repo root first, then `.bot/`). Otherwise, it uses process environment variables.

Required:
- `DISCORD_BOT_TOKEN`
- `DISCORD_GUILD` (guild ID)
- `DISCORD_GUILD_RULES_CHANNEL` (channel ID)

Optional:
- `BOT_MODE` (`validate`, `dry_run`, `apply`) ,  defaults to `apply`
- `BOT_MESSAGE_DELAY` (seconds between posts) ,  defaults to `1`
- `DISCORD_MENTIONS` (`yes`/`no`) ,  defaults to `no` (prevents accidental pings)
- `BOT_RULES_DIR` (path) ,  overrides rules directory; defaults to repo root

## Modes

- `validate`: validates config + rules discovery + markdown checks; **does not connect** to Discord.
- `dry_run`: validates everything, connects to Discord, checks guild/channel/permissions, retrieves bot-authored messages, logs what it *would* delete/post; no mutations.
- `apply`: full workflow: validate > connect > retrieve bot-authored messages > delete them > post rules > shutdown.

## Running locally

From anywhere:

```bash
python -m pip install -r /path/to/repo/.bot/requirements.txt
python /path/to/repo/.bot/bot.py
```

Examples:

```bash
BOT_MODE=validate python /path/to/repo/.bot/bot.py
BOT_MODE=dry_run  python /path/to/repo/.bot/bot.py
BOT_MODE=apply    python /path/to/repo/.bot/bot.py
```

## Required Discord permissions

The bot needs (at minimum) in the rules channel:

* View Channel
* Read Message History
* Send Messages
* Manage Messages

## Notes on rate limits and delays

The bot enforces a **5-second delay** between each script-initiated API action (history page, bulk delete call, single delete call). Posting each rule will instead use `BOT_MESSAGE_DELAY` between posts.

discord.py still applies its own internal rate limit handling. If a surfaced 429 is encountered, the bot logs it, sleeps for the indicated retry-after (when available), and retries within reasonable bounds.
