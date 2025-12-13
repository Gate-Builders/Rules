#!/usr/bin/env python3
"""
Discord Rules Bot (discord.py)

Repository layout:
- repo root contains Rules.<n>.<name>.md files
- bot implementation lives under .bot/

This script can be run from ANY working directory; it locates the repo root
by walking upward from its own file location to find the ".bot/" directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import discord

try:
    # Optional but recommended; listed in requirements.txt
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore


API_ACTION_DELAY_SECONDS = 5.0
DISCORD_BULK_DELETE_MAX = 100
DISCORD_BULK_DELETE_MIN = 2
DISCORD_BULK_DELETE_CUTOFF_DAYS = 14
DISCORD_MESSAGE_CHAR_LIMIT = 2000


# -----------------------------
# Logging
# -----------------------------

class _MaxLevelFilter(logging.Filter):
    def __init__(self, max_level: int) -> None:
        super().__init__()
        self.max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.max_level


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("discord_rules")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    return logger


log = setup_logging()


# -----------------------------
# Errors
# -----------------------------

class BotError(Exception):
    """Base error for controlled failures."""


class ConfigError(BotError):
    pass


class ValidationError(BotError):
    pass


class DiscordOpError(BotError):
    pass


# -----------------------------
# Configuration
# -----------------------------

@dataclass(frozen=True)
class BotConfig:
    token: Optional[str]
    guild_id: Optional[int]
    rules_channel_id: Optional[int]
    message_delay_seconds: int
    mode: str  # validate | apply | dry_run
    allow_mentions: bool
    repo_root: Path
    bot_dir: Path
    rules_dir: Path


def _read_optional_env_str(name: str) -> Optional[str]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def _read_optional_env_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer.") from e


def _read_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in {"yes", "y", "true", "1"}:
        return True
    if v in {"no", "n", "false", "0"}:
        return False
    raise ConfigError(f"{name} must be yes/no (got a non-yes/no value).")


def _read_env_int(name: str, *, default: Optional[int] = None, min_value: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ConfigError(f"Missing required configuration: {name}")
        return default
    try:
        value = int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer.") from e
    if min_value is not None and value < min_value:
        raise ConfigError(f"{name} must be >= {min_value}.")
    return value


def _require_env_str(name: str) -> str:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        raise ConfigError(f"Missing required configuration: {name}")
    return raw.strip()


def find_repo_root_from_bot_file(bot_file: Path) -> tuple[Path, Path]:
    """
    Find repo root reliably from ANY working directory.

    Strategy:
      - Walk upwards from this script's location.
      - If a parent contains a ".bot" directory, that parent is repo root.
      - Or if we land inside a ".bot" directory, repo root is its parent.
    Returns: (repo_root, bot_dir)
    """
    bot_file = bot_file.resolve()
    for parent in [bot_file.parent, *bot_file.parents]:
        if parent.name == ".bot":
            return parent.parent, parent
        if (parent / ".bot").is_dir():
            return parent, (parent / ".bot")
    raise ConfigError("Could not locate repository root: no '.bot/' directory found above this script.")


def load_env_if_present(repo_root: Path, bot_dir: Path) -> Optional[Path]:
    """
    Load .env if present. If it doesn't exist, rely on environment variables.
    Looks in repo root first, then .bot/.
    """
    candidates = [repo_root / ".env", bot_dir / ".env"]
    for p in candidates:
        if p.is_file():
            if load_dotenv is None:
                raise ConfigError("Found a .env file but python-dotenv is not installed.")
            load_dotenv(dotenv_path=p, override=False)
            return p
    return None


def parse_config(repo_root: Path, bot_dir: Path) -> BotConfig:
    env_path = load_env_if_present(repo_root, bot_dir)
    if env_path:
        log.info(f"Loaded configuration from: {env_path}")
    else:
        log.info("No .env file found; using process environment variables.")

    # IMPORTANT: Determine mode first so validate does NOT require Discord secrets.
    mode = os.getenv("BOT_MODE", "apply").strip().lower()
    if mode not in {"validate", "apply", "dry_run"}:
        raise ConfigError("BOT_MODE must be one of: validate, apply, dry_run")

    message_delay = _read_env_int("BOT_MESSAGE_DELAY", default=1, min_value=0)
    allow_mentions = _read_env_bool("DISCORD_MENTIONS", default=False)

    # Discord config:
    # - validate: optional (CI-friendly)
    # - apply/dry_run: required
    if mode == "validate":
        token = _read_optional_env_str("DISCORD_BOT_TOKEN")
        guild_id = _read_optional_env_int("DISCORD_GUILD")
        rules_channel_id = _read_optional_env_int("DISCORD_GUILD_RULES_CHANNEL")
    else:
        token = _require_env_str("DISCORD_BOT_TOKEN")
        guild_id = _read_env_int("DISCORD_GUILD")
        rules_channel_id = _read_env_int("DISCORD_GUILD_RULES_CHANNEL")

    # Rules dir override
    rules_dir_raw = os.getenv("BOT_RULES_DIR", "").strip()
    if rules_dir_raw:
        p = Path(rules_dir_raw)
        rules_dir = p if p.is_absolute() else (repo_root / p)
    else:
        rules_dir = repo_root

    rules_dir = rules_dir.resolve()
    if not rules_dir.exists() or not rules_dir.is_dir():
        raise ConfigError(f"Rules directory does not exist or is not a directory: {rules_dir}")

    # Never print secret values.
    log.info("Configuration summary (secrets omitted):")
    log.info(f"  DISCORD_BOT_TOKEN: {'set' if token else 'missing'}")
    log.info(f"  DISCORD_GUILD: {guild_id if guild_id is not None else 'missing'}")
    log.info(f"  DISCORD_GUILD_RULES_CHANNEL: {rules_channel_id if rules_channel_id is not None else 'missing'}")
    log.info(f"  BOT_MESSAGE_DELAY: {message_delay}s")
    log.info(f"  BOT_MODE: {mode}")
    log.info(f"  DISCORD_MENTIONS: {'yes' if allow_mentions else 'no'}")
    log.info(f"  BOT_RULES_DIR: {rules_dir}")

    return BotConfig(
        token=token,
        guild_id=guild_id,
        rules_channel_id=rules_channel_id,
        message_delay_seconds=message_delay,
        mode=mode,
        allow_mentions=allow_mentions,
        repo_root=repo_root,
        bot_dir=bot_dir,
        rules_dir=rules_dir,
    )


# -----------------------------
# Rules discovery + validation
# -----------------------------

_RULE_RE = re.compile(r"^Rules\.(\d+)\.(.+)\.md$")


@dataclass(frozen=True)
class RuleFile:
    index: int
    operator_name: str
    path: Path
    content: str


def _validate_markdown_sanity(text: str, *, file_label: str) -> None:
    # Unmatched code fences (```).
    fence_count = len(re.findall(r"(?<!\\)```", text))
    if fence_count % 2 != 0:
        raise ValidationError(f"{file_label}: Unmatched triple-backtick code fence (```); count is odd.")

    # Unmatched spoiler markers (||). Use unescaped only.
    spoiler_count = len(re.findall(r"(?<!\\)\|\|", text))
    if spoiler_count % 2 != 0:
        raise ValidationError(f"{file_label}: Unmatched spoiler markers (||); count is odd.")

    # Link format sanity: check for any '](' that doesn't have a plausible matching '[', ']' and ')'.
    for lineno, line in enumerate(text.splitlines(), start=1):
        pos = 0
        while True:
            j = line.find("](", pos)
            if j == -1:
                break

            i = line.rfind("[", 0, j)
            if i == -1:
                raise ValidationError(f"{file_label}:{lineno}: Found '](' without a preceding '[' on the same line.")

            k = line.find(")", j + 2)
            if k == -1:
                raise ValidationError(f"{file_label}:{lineno}: Found a markdown link that never closes with ')'.")
            if k == j + 2:
                raise ValidationError(f"{file_label}:{lineno}: Found an empty markdown link URL: []().")

            link_text = line[i + 1: j].strip()
            url_text = line[j + 2: k].strip()
            if not link_text:
                raise ValidationError(f"{file_label}:{lineno}: Found an empty markdown link text: [](...).")
            if not url_text:
                raise ValidationError(f"{file_label}:{lineno}: Found an empty markdown link URL: (...).")

            pos = k + 1


def discover_and_validate_rules(rules_dir: Path) -> list[RuleFile]:
    log.info(f"Discovering rules in: {rules_dir}")
    candidates: list[tuple[int, str, Path]] = []
    for p in rules_dir.iterdir():
        if p.is_file():
            m = _RULE_RE.match(p.name)
            if m:
                idx = int(m.group(1))
                operator = m.group(2)
                candidates.append((idx, operator, p))

    if not candidates:
        raise ValidationError(f"No rules files found in {rules_dir} matching Rules.<x>.<something>.md")

    # Detect duplicates by index
    seen: dict[int, list[Path]] = {}
    for idx, _, path in candidates:
        seen.setdefault(idx, []).append(path)

    dup_lines = []
    for idx, paths in sorted(seen.items()):
        if len(paths) > 1:
            files = ", ".join([p.name for p in paths])
            dup_lines.append(f"  Rules.{idx}.* duplicates: {files}")
    if dup_lines:
        raise ValidationError("Duplicate rule indices detected:\n" + "\n".join(dup_lines))

    by_index: dict[int, tuple[str, Path]] = {idx: (op, p) for idx, op, p in candidates}

    # Ensure sequential numbering starting at 1 with no gaps.
    indices = sorted(by_index.keys())
    expected = list(range(1, max(indices) + 1))
    if indices != expected:
        missing = sorted(set(expected) - set(indices))
        extra = sorted(set(indices) - set(expected))
        msg = ["Rule numbering must be sequential starting at 1 with no gaps."]
        if missing:
            msg.append(f"Missing indices: {missing}")
        if extra:
            msg.append(f"Unexpected indices: {extra}")
        raise ValidationError("\n".join(msg))

    # Read + validate each file
    rules: list[RuleFile] = []
    for idx in indices:
        operator, path = by_index[idx]
        label = path.name
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValidationError(f"{label}: File is not valid UTF-8.") from e

        if len(content) > DISCORD_MESSAGE_CHAR_LIMIT:
            raise ValidationError(
                f"{label}: {len(content)} characters exceeds Discord's 2000 character message limit."
            )

        _validate_markdown_sanity(content, file_label=label)

        rules.append(RuleFile(index=idx, operator_name=operator, path=path, content=content))

    log.info(f"Validated {len(rules)} rule file(s).")
    return rules


# -----------------------------
# Discord runner
# -----------------------------

class ActionDelayer:
    """
    Enforces the required 5-second delay BETWEEN bot-initiated API actions.

    The delay is time-based (monotonic): if you have already waited >= 5 seconds
    (e.g., because of BOT_MESSAGE_DELAY or a rate-limit retry-after), it will not
    sleep an additional 5 seconds.
    """
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self._last_action_end: Optional[float] = None

    async def before_action(self) -> None:
        if self._last_action_end is None:
            return
        elapsed = time.monotonic() - self._last_action_end
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    def after_action(self) -> None:
        self._last_action_end = time.monotonic()


def _allowed_mentions(allow: bool) -> discord.AllowedMentions:
    return discord.AllowedMentions.all() if allow else discord.AllowedMentions.none()


def _extract_retry_after_seconds(exc: discord.HTTPException) -> Optional[float]:
    ra = getattr(exc, "retry_after", None)
    if ra is not None:
        try:
            return float(ra)
        except Exception:
            return None

    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers:
        h = headers.get("Retry-After") or headers.get("retry-after")
        if h is not None:
            try:
                return float(h)
            except Exception:
                return None
    return None


async def call_with_retries(
    delayer: ActionDelayer,
    action_name: str,
    coro_factory: Callable[[], "asyncio.Future[Any]"],
    *,
    max_attempts: int = 5,
) -> Any:
    """
    Wrap a Discord API action with retries for surfaced 429s.

    - Relies on discord.py built in rate limit handling by default.
    - If a 429 bubbles up, logs, sleeps for retry-after (when available), retries.
    - Still enforces the inter-action delay between each attempt (as configured by the delayer).
    """
    last_exc: Optional[BaseException] = None

    for attempt in range(1, max_attempts + 1):
        await delayer.before_action()
        try:
            return await coro_factory()
        except discord.HTTPException as e:
            last_exc = e
            if getattr(e, "status", None) == 429:
                wait_s = _extract_retry_after_seconds(e) or float(2 * attempt)
                log.error(
                    f"[rate limit] {action_name}: HTTP 429; sleeping {wait_s:.2f}s then retrying "
                    f"(attempt {attempt}/{max_attempts})"
                )
                await asyncio.sleep(wait_s)
                continue
            raise
        except (discord.Forbidden, discord.NotFound) as e:
            raise DiscordOpError(f"{action_name} failed: {type(e).__name__}") from e
        finally:
            delayer.after_action()

    raise DiscordOpError(f"{action_name} failed after {max_attempts} attempt(s).") from last_exc


async def resolve_guild_and_channel(
    delayer: ActionDelayer,
    client: discord.Client,
    cfg: BotConfig,
) -> tuple[discord.Guild, discord.TextChannel | discord.Thread]:
    # cfg.guild_id / cfg.rules_channel_id are guaranteed non-None for apply/dry_run by main()
    assert cfg.guild_id is not None
    assert cfg.rules_channel_id is not None

    guild = client.get_guild(cfg.guild_id)
    if guild is None:
        log.info(f"Fetching guild {cfg.guild_id}...")
        guild = await call_with_retries(
            delayer,
            "fetch_guild",
            lambda: client.fetch_guild(cfg.guild_id),
        )

    if not isinstance(guild, discord.Guild):
        raise DiscordOpError("Failed to resolve the target guild (bot not in guild or invalid ID).")

    channel = guild.get_channel(cfg.rules_channel_id)
    if channel is None:
        log.info(f"Fetching channel {cfg.rules_channel_id}...")
        channel = await call_with_retries(
            delayer,
            "fetch_channel",
            lambda: guild.fetch_channel(cfg.rules_channel_id),
        )

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise DiscordOpError(f"Rules channel must be a text channel or thread; got {type(channel).__name__}.")

    return guild, channel


async def verify_permissions(
    delayer: ActionDelayer,
    client: discord.Client,
    guild: discord.Guild,
    channel: discord.TextChannel | discord.Thread,
) -> None:
    if client.user is None:
        raise DiscordOpError("Client user is not available.")

    me = guild.get_member(client.user.id)
    if me is None:
        me = await call_with_retries(
            delayer,
            "fetch_member",
            lambda: guild.fetch_member(client.user.id),
        )

    perms = channel.permissions_for(me)
    missing = []
    if not perms.view_channel:
        missing.append("View Channel")
    if not perms.read_message_history:
        missing.append("Read Message History")
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.manage_messages:
        missing.append("Manage Messages")

    if missing:
        raise DiscordOpError("Bot is missing required permissions in the rules channel: " + ", ".join(missing))

    log.info("Permissions check: OK")


async def retrieve_bot_messages(
    delayer: ActionDelayer,
    client: discord.Client,
    channel: discord.TextChannel | discord.Thread,
) -> list[discord.Message]:
    log.info("Retrieving channel history and filtering to bot-authored messages...")
    if client.user is None:
        raise DiscordOpError("Client user is not available.")
    bot_id = client.user.id

    before: Optional[discord.Message] = None
    bot_messages: list[discord.Message] = []
    page = 0

    while True:
        page += 1

        async def _fetch_page() -> list[discord.Message]:
            return [m async for m in channel.history(limit=100, before=before, oldest_first=False)]

        messages: list[discord.Message] = await call_with_retries(
            delayer,
            f"history_page_{page}",
            _fetch_page,
        )
        if not messages:
            break

        bot_messages.extend([m for m in messages if m.author and m.author.id == bot_id])
        before = messages[-1]

    log.info(f"Found {len(bot_messages)} message(s) authored by the bot.")
    return bot_messages


def split_recent_vs_old(messages: list[discord.Message]) -> tuple[list[discord.Message], list[discord.Message]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DISCORD_BULK_DELETE_CUTOFF_DAYS)
    recent: list[discord.Message] = []
    old: list[discord.Message] = []
    for m in messages:
        created = m.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created > cutoff:
            recent.append(m)
        else:
            old.append(m)
    return recent, old


def chunk_messages(msgs: list[discord.Message], size: int) -> list[list[discord.Message]]:
    return [msgs[i: i + size] for i in range(0, len(msgs), size)]


async def delete_messages_apply(
    delayer: ActionDelayer,
    channel: discord.TextChannel | discord.Thread,
    bot_messages: list[discord.Message],
) -> None:
    if not bot_messages:
        log.info("No bot-authored messages to delete.")
        return

    recent, old = split_recent_vs_old(bot_messages)
    log.info(
        f"Deletion plan: {len(recent)} message(s) <14 days old (bulk eligible), "
        f"{len(old)} message(s) >=14 days old (individual)."
    )

    if recent:
        recent_sorted = sorted(recent, key=lambda m: m.created_at, reverse=True)
        for batch in chunk_messages(recent_sorted, DISCORD_BULK_DELETE_MAX):
            if len(batch) >= DISCORD_BULK_DELETE_MIN:
                log.info(f"Bulk deleting {len(batch)} recent message(s)...")
                await call_with_retries(
                    delayer,
                    f"bulk_delete_{len(batch)}",
                    lambda b=batch: channel.delete_messages(b),
                )
            else:
                m = batch[0]
                log.info("Deleting 1 recent message individually (bulk requires >=2)...")
                await call_with_retries(
                    delayer,
                    "delete_message_recent",
                    lambda: m.delete(),
                )

    for m in sorted(old, key=lambda x: x.created_at, reverse=True):
        log.info(f"Deleting old message {m.id} (>=14 days)...")
        await call_with_retries(
            delayer,
            "delete_message_old",
            lambda: m.delete(),
        )

    # Explicit post-deletion wait (required)
    log.info("Post-deletion wait: 5 seconds.")
    await asyncio.sleep(API_ACTION_DELAY_SECONDS)


async def post_rules_apply(
    delayer: ActionDelayer,
    channel: discord.TextChannel | discord.Thread,
    rules: list[RuleFile],
    cfg: BotConfig,
) -> None:
    mentions = _allowed_mentions(cfg.allow_mentions)

    # Do not apply the 5-second API action delay to posting
    post_delayer = ActionDelayer(0.0)

    for i, r in enumerate(rules):
        label = r.path.name
        log.info(f"Posting {label} ({len(r.content)} chars)...")

        async def _send():
            return await channel.send(r.content, allowed_mentions=mentions)

        await call_with_retries(post_delayer, f"send_{label}", _send, max_attempts=3)

        # Keep only BOT_MESSAGE_DELAY between posts
        if i < len(rules) - 1 and cfg.message_delay_seconds > 0:
            log.info(f"Waiting {cfg.message_delay_seconds}s (BOT_MESSAGE_DELAY) before next post...")
            await asyncio.sleep(cfg.message_delay_seconds)


class RulesClient(discord.Client):
    def __init__(self, cfg: BotConfig, rules: list[RuleFile]) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.cfg = cfg
        self.rules = rules
        self.exit_code: int = 1  # pessimistic default

    async def on_ready(self) -> None:
        delayer = ActionDelayer(API_ACTION_DELAY_SECONDS)

        try:
            assert self.user is not None
            log.info(f"Connected to Discord as {self.user} (id={self.user.id}).")

            guild, channel = await resolve_guild_and_channel(delayer, self, self.cfg)
            await verify_permissions(delayer, self, guild, channel)

            if self.cfg.mode == "dry_run":
                bot_messages = await retrieve_bot_messages(delayer, self, channel)
                recent, old = split_recent_vs_old(bot_messages)
                log.info("[dry_run] Would delete:")
                log.info(f"  - {len(recent)} recent message(s) via bulk delete batches")
                log.info(f"  - {len(old)} old message(s) via individual delete calls")
                log.info(f"[dry_run] Would post {len(self.rules)} rule file(s) in order.")
                self.exit_code = 0
                return

            if self.cfg.mode == "apply":
                bot_messages = await retrieve_bot_messages(delayer, self, channel)
                await delete_messages_apply(delayer, channel, bot_messages)
                await post_rules_apply(delayer, channel, self.rules, self.cfg)
                log.info("All rules posted successfully.")
                self.exit_code = 0
                return

            raise DiscordOpError(f"Unexpected mode inside Discord client: {self.cfg.mode}")

        except BotError as e:
            log.error(f"ERROR: {e}")
            self.exit_code = 2
        except Exception as e:
            log.error(f"UNHANDLED ERROR: {e}")
            self.exit_code = 3
        finally:
            try:
                await self.close()
            except Exception:
                pass


# -----------------------------
# Entry point
# -----------------------------

def main() -> int:
    try:
        repo_root, bot_dir = find_repo_root_from_bot_file(Path(__file__))
        cfg = parse_config(repo_root, bot_dir)

        rules = discover_and_validate_rules(cfg.rules_dir)

        if cfg.mode == "validate":
            log.info("Mode=validate: skipping Discord connection. Validation succeeded.")
            return 0

        # For non-validate, these must be present
        if not cfg.token or cfg.guild_id is None or cfg.rules_channel_id is None:
            raise ConfigError("Missing required Discord configuration for apply/dry_run.")

        client = RulesClient(cfg, rules)
        try:
            # discord.py manages loop + cleanup here (prevents aiohttp "Unclosed connector")
            client.run(cfg.token, log_handler=None)
        except discord.LoginFailure:
            log.error("ERROR: Discord login failed. Check DISCORD_BOT_TOKEN.")
            return 2
        except Exception as e:
            log.error(f"UNHANDLED ERROR: {e}")
            return 3
        return client.exit_code

    except BotError as e:
        log.error(f"ERROR: {e}")
        return 2
    except KeyboardInterrupt:
        log.error("Interrupted.")
        return 130
    except Exception as e:
        log.error(f"UNHANDLED ERROR: {e}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
