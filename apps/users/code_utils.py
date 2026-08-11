# -*- coding: utf-8 -*-
"""
Shared login-code helpers used by BOTH the Telegram bot (bot.py /login)
and the web app's "Kod orqali kirish" auto-send endpoint
(/api/v1/auth/telegram/send-code/).

Kept in one module so the two entry points can never drift apart:
the same random, single-use, 5-minute code is created either way.
"""
import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone

from .models import TelegramLoginCode


def hash_code(code: str) -> str:
    """SHA-256 hex digest of a login code.

    Only this hash is persisted — the plaintext code is NEVER stored in the
    database, so a DB leak can never be used to log in.
    """
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def create_login_code(tg_id, tg_username='', first_name='', last_name='', language_code=''):
    """Create a fresh one-time login code for a Telegram user (sync, ORM-safe).

    Returns the model instance with the PLAINTEXT code attached as
    ``obj.plain_code`` (for sending to the user's chat / returning to the
    verified client). Only the SHA-256 hash is persisted in the DB.
    """
    # Invalidate any previous unused codes for this user — only the LATEST
    # code is ever valid, so a leaked old code cannot be replayed.
    TelegramLoginCode.objects.filter(telegram_id=tg_id, used=False).update(used=True)

    # secrets, not random: the code is an authentication credential and must
    # be cryptographically unpredictable (random is not suitable for that).
    code_value = f'{secrets.randbelow(1_000_000):06d}'
    obj = TelegramLoginCode.objects.create(
        code=hash_code(code_value),
        telegram_id=tg_id,
        telegram_username=tg_username,
        first_name=first_name,
        last_name=last_name,
        language_code=language_code,
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    # Transient plaintext for the immediate caller — never saved back.
    obj.plain_code = code_value
    return obj


def send_code_to_chat(bot_token, chat_id, code):
    """Best-effort: push the login code to the user's Telegram chat.

    Returns True if the Telegram API accepted the message, False otherwise
    (e.g. the user never started the bot). The web app auto-login flow does
    NOT depend on this — the code is returned to the verified client anyway,
    so a failed push never blocks login.
    """
    try:
        import requests
        resp = requests.post(
            f'https://api.telegram.org/bot{bot_token}/sendMessage',
            json={
                'chat_id': chat_id,
                'text': (
                    f"\U0001F511 <b>Kirish kodingiz</b>\n\n"
                    f"<code>{code}</code>\n\n"
                    f"\U0001F550 Kod <b>5 daqiqa</b> yaroqli va faqat "
                    f"<b>bir marta</b> ishlatiladi."
                ),
                'parse_mode': 'HTML',
            },
            timeout=10,
        )
        return bool(resp.ok)
    except Exception:
        # Never log the code or the token; a network hiccup must not break login.
        return False
