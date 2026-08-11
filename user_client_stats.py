"""
User-client statistics for the admin "To'lov nazorati" (cardpay) panel.

The Telethon user client (user_client.py) writes a small JSON stats file:
  .freebuff/user-client-stats.json
    {started_at, last_activity, last_heartbeat, restarts, messages_seen,
     payments_matched, suspicious, last_error, last_error_ts}

No Django imports here — safe to use from the plain asyncio process.
"""

import json
import os
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATS_FILE = os.path.join(PROJECT_ROOT, '.freebuff', 'user-client-stats.json')
SUPERVISOR_LOG = os.path.join(PROJECT_ROOT, '.freebuff', 'user-client-supervisor.log')

DEFAULT_STATS = {
    'started_at': None,
    'last_activity': None,
    'last_heartbeat': None,
    'restarts': 0,
    'messages_seen': 0,
    'payments_matched': 0,
    'suspicious': 0,
    'last_error': '',
    'last_error_ts': None,
    'authorized': False,
    'account': {},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_stats() -> dict:
    try:
        with open(STATS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return dict(DEFAULT_STATS)
        for key, val in DEFAULT_STATS.items():
            data.setdefault(key, val)
        return data
    except Exception:
        return dict(DEFAULT_STATS)


def write_stats(stats: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATS_FILE), exist_ok=True)
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def mark_started(account: dict | None = None) -> None:
    stats = read_stats()
    stats['started_at'] = now_iso()
    stats['restarts'] = stats.get('restarts', 0) + 1
    # The worker calls this only AFTER a successful Telegram auth, so it
    # doubles as the authorized flag the admin panel reads — without the
    # panel ever opening a competing Telethon connection against the
    # session file the running worker holds.
    if account:
        stats['authorized'] = True
        stats['account'] = account
    write_stats(stats)


def heartbeat() -> None:
    stats = read_stats()
    now = now_iso()
    stats['last_activity'] = now
    stats['last_heartbeat'] = now
    write_stats(stats)


def record_event(kind: str, amount: str = '') -> None:
    """kind: message|matched|suspicious — bump counters."""
    stats = read_stats()
    stats['last_activity'] = now_iso()
    if kind == 'message':
        stats['messages_seen'] = stats.get('messages_seen', 0) + 1
    elif kind == 'matched':
        stats['payments_matched'] = stats.get('payments_matched', 0) + 1
    elif kind == 'suspicious':
        stats['suspicious'] = stats.get('suspicious', 0) + 1
    write_stats(stats)


def record_error(message: str) -> None:
    stats = read_stats()
    stats['last_error'] = (message or '')[:400]
    stats['last_error_ts'] = now_iso()
    stats['last_activity'] = now_iso()
    write_stats(stats)


def is_online(max_age_seconds: int = 180) -> bool:
    stats = read_stats()
    last = stats.get('last_heartbeat') or stats.get('last_activity')
    if not last:
        return False
    try:
        dt = datetime.fromisoformat(last)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_seconds
    except Exception:
        return False


def read_supervisor_log(n: int = 40) -> list:
    try:
        with open(SUPERVISOR_LOG, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return [line.rstrip('\r\n') for line in lines[-n:]]
    except Exception:
        return []
