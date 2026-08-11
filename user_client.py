"""
DONZO Telegram User Client (card payment verification).

A Telethon client logged in with YOUR personal Telegram account that:
  1. Watches the configured payment-monitor chat (usually a bank-card
     notification chat/group) for incoming-transfer messages.
  2. Parses the amount and matches it against active pending top-up
     requests (unique amounts). On a match the user's balance is credited
     automatically (atomic, row-locked) and a report is sent to the
     report group.
  3. Transfers ABOVE the suspicious limit are NOT credited — they land in
     the admin panel's "Shubhali" list for manual approve/reject.
  4. In Saved Messages, typing `status` replies with the daily payment
     status report.
  5. Periodically expires pending requests older than the timeout window
     (10 min default) — those transfers are cancelled.

SETUP (one time):  python setup_user_client.py   (logs in, saves session)
RUN (24/7):         python user_client_supervisor.py   (watchdog)

Credentials (api_id/api_hash) come from my.telegram.org → settings:
  .env: TELEGRAM_API_ID / TELEGRAM_API_HASH
  or admin panel → Kalitlar → telegram_api_id / telegram_api_hash
Session: backend/sessions/donzo_user.session

SECURITY:
  • The session file and api_hash are secrets — never commit them.
  • Every bank message is consumed at most once (unique chat+message).
  • Never logs raw message text or amounts to the console.
"""
import asyncio
import logging
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402
django.setup()

from asgiref.sync import sync_to_async  # noqa: E402
from telethon import TelegramClient, events  # noqa: E402

import user_client_stats  # noqa: E402

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 30      # seconds
SWEEP_INTERVAL = 60          # seconds — expire stale requests
MONITOR_REFRESH_INTERVAL = 300  # re-resolve monitor entity every 5 min
STATUS_REPORT_INTERVAL = 900  # seconds — system health report to report group (15 min)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, 'sessions')
SESSION_FILE = os.path.join(SESSION_DIR, 'donzo_user.session')

# Exit codes understood by the supervisor
EXIT_NO_CREDENTIALS = 3
EXIT_NOT_AUTHORIZED = 4


def _get_credentials():
    from apps.settings_app.models import Setting
    api_id = (Setting.get_setting('telegram_api_id', '') or '').strip() \
        or (os.getenv('TELEGRAM_API_ID', '') or '').strip()
    api_hash = (Setting.get_setting('telegram_api_hash', '') or '').strip() \
        or (os.getenv('TELEGRAM_API_HASH', '') or '').strip()
    return api_id, api_hash


def _log(msg: str):
    line = f"[{user_client_stats.now_iso()}] {msg}"
    # Windows consoles default to cp1252 which cannot encode non-Latin-1
    # characters (→, arrows, emoji) — reconfigure or the log crashes.
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    print(line, flush=True)
    try:
        with open(user_client_stats.SUPERVISOR_LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


async def main():
    from apps.cardpay import services as cardpay_services

    # DB read in async context → must go through sync_to_async
    api_id, api_hash = await sync_to_async(_get_credentials)()
    if not api_id or not api_hash:
        _log("XATO: TELEGRAM_API_ID / TELEGRAM_API_HASH sozlanmagan. "
             "Admin panel → Kalitlar → telegram_api_id/telegram_api_hash ni to'ldiring, "
             "yoki .env ga yozing, so'ng setup_user_client.py ishga tushiring.")
        sys.exit(EXIT_NO_CREDENTIALS)

    os.makedirs(SESSION_DIR, exist_ok=True)
    client = TelegramClient(SESSION_FILE, int(api_id), api_hash)

    try:
        await client.connect()
    except Exception as exc:
        _log(f"XATO: Telegramga ulanishda xatolik: {type(exc).__name__}")
        sys.exit(EXIT_NOT_AUTHORIZED)

    if not await client.is_user_authorized():
        _log("XATO: Session yo'q yoki ro'yxatdan o'tmagan. "
             "Admin panel → To'lov nazorati → User Client orqali kirishni bajaring "
             "(telefon raqam → kod → 2FA parol).")
        await client.disconnect()
        sys.exit(EXIT_NOT_AUTHORIZED)

    await client.start()
    me = await client.get_me()
    if me is None:
        _log("XATO: Session topilmadi yoki ro'yxatdan o'tmagan. "
             "Avval setup_user_client.py bilan kirishni bajaring.")
        sys.exit(EXIT_NOT_AUTHORIZED)

    _log(f"User client ishga tushdi: @{me.username or me.first_name} (id={me.id})")
    user_client_stats.mark_started({
        'username': getattr(me, 'username', None) or '',
        'first_name': getattr(me, 'first_name', None) or '',
        'user_id': getattr(me, 'id', None),
        'phone': getattr(me, 'phone', None) or '',
    })

    monitor_entity = None
    last_refresh = 0.0

    async def refresh_monitor():
        """Resolve the configured monitor chat (id or username) to a peer."""
        nonlocal monitor_entity, last_refresh
        s = await sync_to_async(cardpay_services.get_settings)()
        raw = s['monitor_chat_id']
        if not raw:
            monitor_entity = None
            return
        try:
            ent = await client.get_entity(raw)
            monitor_entity = ent
            _log(f"Monitor chat aniqlandi: {getattr(ent, 'title', None) or raw} (id={ent.id})")
        except Exception as exc:
            _log(f"Monitor chat topilmadi ({raw}): {type(exc).__name__} — "
                 f"sozlamalarni tekshiring")
            monitor_entity = None

    await refresh_monitor()

    # ── Heartbeat + expiry sweeper + security escalation ──
    async def heartbeat_loop():
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            user_client_stats.heartbeat()
            try:
                await sync_to_async(cardpay_services.expire_stale_requests)()
            except Exception:
                logger.exception('sweep failed')

    async def escalation_loop():
        # Security incident escalation: un-ACKed CRITICAL/HIGH alerts get
        # re-sent after the configured timeout (level 1→2→3).
        while True:
            await asyncio.sleep(60)
            try:
                from apps.security.alerts import escalate_open_incidents
                await sync_to_async(escalate_open_incidents)()
            except Exception:
                logger.exception('escalation sweep failed')

    async def status_report_loop():
        # Send the system health report to the report group every 15 minutes:
        # what is down, or "hammasi ishlayapti" when everything is fine.
        # First report goes out ~30s after start so it is visible right away.
        await asyncio.sleep(30)
        while True:
            try:
                ok = await sync_to_async(cardpay_services.send_health_report)()
                _log(f"Holat hisoboti: {'yuborildi' if ok else 'yuborilmadi (chat/token tekshiring)'}")
            except Exception:
                logger.exception('status report loop failed')
            await asyncio.sleep(STATUS_REPORT_INTERVAL)

    # ── Saved Messages command center ──
    async def handle_saved_command(event, cmd):
        from apps.security import alerts as sec_alerts
        from apps.security.models import SecurityIncident

        if cmd in ('status', '/status', 'статус'):
            report = await sync_to_async(cardpay_services.build_status_report)()
            sec = await sync_to_async(sec_alerts.ack_summary_text)()
            await event.reply(report + "\n\n" + sec)
        elif cmd in ('incidents', 'incident'):
            incs = await sync_to_async(list)(
                SecurityIncident.objects.filter(status__in=['OPEN', 'ACKED', 'INVESTIGATING'])
                .order_by('-created_at')[:5])
            if not incs:
                await event.reply("🔐 Ochiq incidentlar yo'q ✓")
                return
            lines = ["🚨 <b>Ochiq incidentlar</b>\n"]
            for i in incs:
                lines.append(f"#{i.id} <b>{i.severity}</b> {i.risk_score}/100 — "
                             f"{i.payment_amount:,.0f} so'm ({i.status})")
            await event.reply("\n".join(lines))
        elif cmd in ('critical', 'crit'):
            incs = await sync_to_async(list)(
                SecurityIncident.objects.filter(severity='CRITICAL',
                                                status__in=['OPEN', 'ACKED', 'INVESTIGATING'])
                .order_by('-created_at')[:5])
            if not incs:
                await event.reply("🔴 Kritik incidentlar yo'q ✓")
                return
            lines = ["🔴 <b>Kritik incidentlar</b>\n"]
            for i in incs:
                lines.append(f"#{i.id} — {i.payment_amount:,.0f} so'm ({i.status}, esc:{i.escalation_level})")
            await event.reply("\n".join(lines))
        elif cmd in ('pending', 'hold'):
            from apps.cardpay.models import CardTopupRequest
            cnt = await sync_to_async(CardTopupRequest.objects.filter(status='pending').count)()
            await event.reply(f"🕐 Kutilayotgan to'lovlar: <b>{cnt}</b>")
        elif cmd in ('ai status', 'ai'):
            from apps.security.gemini_ai import health_check
            h = await sync_to_async(health_check)()
            await event.reply(
                f"🤖 <b>AI status</b>\n\n"
                f"Configured: {'✅' if h['configured'] else '❌'}\n"
                f"Reachable: {'✅' if h['reachable'] else '❌'}\n"
                f"Detail: {h.get('detail', '')}")
        else:
            await event.reply(
                "📋 <b>Buyruqlar:</b> status · incidents · critical · pending · ai status")

    # ── New message handler: monitored chat + Saved Messages ──
    # Saved Messages (O'zimga yuborilgan) is BOTH the command center AND the
    # bank-notification inbox: known commands are answered, everything else
    # with text is treated as a bank payment message.
    COMMAND_WORDS = ('status', '/status', 'статус', 'incidents', 'incident',
                     'critical', 'crit', 'pending', 'hold', 'ai status', 'ai',
                     'buyruqlar', 'help', '/help')

    async def _consume(event, text):
        """Send one message through the card-payment pipeline (idempotent)."""
        chat_id = event.chat_id
        sender = getattr(event.sender, 'id', None)
        result = await sync_to_async(cardpay_services.consume_payment_message)(
            str(chat_id), event.message.id, text, sender_id=str(sender) if sender else None,
        )
        outcome = result.get('outcome', '?')
        if outcome in ('matched', 'suspicious', 'held'):
            user_client_stats.record_event(outcome if outcome != 'held' else 'suspicious')
            _log(f"To'lov qayta ishlandi: {outcome} (msg {event.message.id})")
        elif outcome == 'no_match':
            _log(f"Xabar qabul qilindi, mos so'rov topilmadi (msg {event.message.id})")

    @client.on(events.NewMessage())
    async def on_new(event):
        nonlocal last_refresh
        try:
            now = asyncio.get_event_loop().time()
            if now - last_refresh > MONITOR_REFRESH_INTERVAL:
                last_refresh = now
                await refresh_monitor()

            text = (event.raw_text or '')
            is_saved = event.is_private and event.chat_id == me.id

            # 1) Saved Messages: command center + bank payment inbox
            if is_saved:
                low = text.strip().lower()
                if low and low.split()[0] in COMMAND_WORDS:
                    await handle_saved_command(event, low)
                    _log(f"Saved Messages buyruq: {low}")
                    return
                if text.strip():
                    user_client_stats.record_event('message')
                    await _consume(event, text)
                return

            # 2) Monitored chat: bank notifications
            if monitor_entity is None:
                return
            if event.chat_id != getattr(monitor_entity, 'id', None):
                return
            user_client_stats.record_event('message')
            await _consume(event, text)
        except Exception:
            logger.exception('message handler error')

    asyncio.create_task(heartbeat_loop())
    asyncio.create_task(escalation_loop())
    asyncio.create_task(status_report_loop())

    _log("User client tinglashni boshladi. Saved Messages'ga 'status' yozing.")
    await client.run_until_disconnected()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("User client to'xtatildi (Ctrl+C).")
