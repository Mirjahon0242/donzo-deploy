"""
Bot statistics helpers for the admin "Bot holati" (Bot Status) panel.

The Telegram bot (bot.py) writes a small JSON stats file:
  .freebuff/bot-stats.json   {started_at, last_activity, restarts,
                              messages_sent, updates_handled, commands{}}

The admin view reads it to answer:
  • Is the bot running?   → last_activity freshness (heartbeat every 30s)
  • Last activity time    → last_activity
  • Messages sent count   → messages_sent / updates_handled
  • Recent log lines      → tail of .freebuff/bot-supervisor.log

This module intentionally avoids Django imports so bot.py can use it
right after django.setup() with zero extra cost.
"""

import json
import os
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_FILE = os.path.join(PROJECT_ROOT, '.freebuff', 'bot-stats.json')
SUPERVISOR_LOG = os.path.join(PROJECT_ROOT, '.freebuff', 'bot-supervisor.log')

DEFAULT_STATS = {
    'started_at': None,
    'last_activity': None,
    'last_heartbeat': None,
    'restarts': 0,
    'messages_sent': 0,
    'updates_handled': 0,
    'commands': {},
    # {checked_at, valid, username, detail} — set by bot.py via getMe
    'token_status': None,
    # [{ts, kind, message}] — getUpdates failures (409/Conflict, NetworkError...)
    'polling_errors': [],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_bot_stats() -> dict:
    """Read the stats JSON (always returns a dict with defaults)."""
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_STATS)
        for key, val in DEFAULT_STATS.items():
            data.setdefault(key, val)
        # Legacy stats files wrote polling_errors as a dict ({}) — the
        # admin panel expects an array. Normalize so the UI never crashes.
        if not isinstance(data.get('polling_errors'), list):
            data['polling_errors'] = []
        return data
    except Exception:
        return dict(DEFAULT_STATS)


def write_bot_stats(stats: dict) -> None:
    """Atomically write the stats JSON."""
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # stats must never crash the bot


def bump(updates: int = 0, messages: int = 0, command: str | None = None) -> None:
    """Increment counters and refresh last_activity."""
    stats = read_bot_stats()
    stats['updates_handled'] = stats.get('updates_handled', 0) + updates
    stats['messages_sent'] = stats.get('messages_sent', 0) + messages
    stats['last_activity'] = now_iso()
    if command:
        stats.setdefault('commands', {})
        stats['commands'][command] = stats['commands'].get(command, 0) + 1
    write_bot_stats(stats)


def mark_started() -> None:
    """Called once at bot startup: set started_at and count a restart."""
    stats = read_bot_stats()
    stats['started_at'] = now_iso()
    stats['restarts'] = stats.get('restarts', 0) + 1
    write_bot_stats(stats)


def heartbeat() -> None:
    """Called every 30s so the admin panel can see the bot is alive."""
    stats = read_bot_stats()
    now = now_iso()
    stats['last_activity'] = now
    stats['last_heartbeat'] = now
    write_bot_stats(stats)


def record_polling_error(kind: str, message: str, max_errors: int = 20) -> None:
    """
    Record a getUpdates failure (409 Conflict, NetworkError, TimedOut, ...).

    The bot passes its polling-loop error kind + message here so the admin
    panel can show live getUpdates health. Keeps only the last max_errors
    entries to bound file size. Never raises.
    """
    try:
        stats = read_bot_stats()
        errors = stats.setdefault('polling_errors', [])
        errors.append({
            'ts': now_iso(),
            'kind': kind or 'unknown',
            'message': (message or '')[:300],
        })
        stats['polling_errors'] = errors[-max_errors:]
        write_bot_stats(stats)
    except Exception:
        pass  # stats must never crash the bot


def set_token_status(valid: bool, username: str = '', detail: str = '') -> None:
    """Record the result of the last bot-token validation (getMe check)."""
    stats = read_bot_stats()
    stats['token_status'] = {
        'checked_at': now_iso(),
        'valid': bool(valid),
        'username': username or '',
        'detail': (detail or '')[:200],
    }
    write_bot_stats(stats)


def is_bot_running(max_age_seconds: int = 120) -> bool:
    """True if the bot wrote a heartbeat within the last max_age_seconds."""
    stats = read_bot_stats()
    last = stats.get('last_activity')
    if not last:
        return False
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age <= max_age_seconds
    except Exception:
        return False


def read_supervisor_log(n: int = 40) -> list:
    """Return the last n lines of the supervisor log (newest last)."""
    try:
        with open(SUPERVISOR_LOG, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return [line.rstrip('\r\n') for line in lines[-n:]]
    except Exception:
        return []
