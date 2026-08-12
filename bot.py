"""
TOPUP HUB Telegram Bot

Runs a polling-based Telegram bot that lets users:
  • Open the Web App (mini app) with a single button
  • Check their balance
  • View their recent orders
  • Get support link

Requires: python-telegram-bot==21.1.0
Run:      python bot.py   (from the backend/ directory, venv active)
          or keep it alive 24/7 via bot_supervisor.py

The bot token / web app URL are read from the database settings
(admin panel → Kalitlar), so they can be changed at runtime.

NOTE: handlers are async (asyncio event loop), so all Django ORM
calls are wrapped with asgiref sync_to_async to avoid
SynchronousOnlyOperation.
"""

import asyncio
import logging
import os
import re
import sys
import threading
import time
from decimal import Decimal

sys.path.append(os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from asgiref.sync import sync_to_async
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import InvalidToken
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

from apps.settings_app.models import Setting
from apps.users.models import User
from apps.orders.models import Order
from bot_stats import (
    bump, heartbeat, mark_started,
    record_polling_error, set_token_status,
)

# ── Heartbeat: keeps .freebuff/bot-stats.json fresh so the admin
#    panel can show the bot is alive even with no user traffic. ──
HEARTBEAT_INTERVAL = 30

# ── Fragment live-price sync: once a day (checked every hour). The bot is
#    the always-on process (supervisor keeps it alive), so it's the natural
#    home for the daily price sync. ──
PRICE_SYNC_CHECK_INTERVAL = 3600  # har soatda tekshiradi

# Roles whose Telegram chats get staff-only bot extras (admin panel button
# + quick stats). Kept in ONE place so the three usages can't drift.
STAFF_ROLES = ('super_admin', 'admin', 'senior_operator', 'operator', 'support')

# Shape-based bot-token matcher (123456789:AA... — 30+ alnum chars). Works
# even without the 'bot' URL prefix, so any secret-shaped string is redacted.
_TOKEN_PATTERN = re.compile(r'\d{5,}:[A-Za-z0-9_-]{30,}')


def _scrub_secrets(text: str) -> str:
    """Replace any bot-token-shaped string with [REDACTED]."""
    try:
        return _TOKEN_PATTERN.sub('[REDACTED]', text or '')
    except Exception:
        # Fail SAFE: never persist a possibly-unscrubbed string.
        return '[REDACTED]'


class PollingErrorHandler(logging.Handler):
    """
    Capture getUpdates polling failures (409 Conflict, NetworkError,
    InvalidToken...) into .freebuff/bot-stats.json so the admin
    'Bot holati' panel can show live getUpdates health.

    Attached to the `telegram` logger, which python-telegram-bot's updater
    uses for the network retry loop (see telegram/ext/_updater.py).
    (NOTE: PTB logs TimedOut at DEBUG, so timeouts never reach this
    ERROR-level handler — only real polling failures are recorded.)
    """
    def emit(self, record: logging.LogRecord) -> None:
        try:
            # Scrub any secret-shaped string (e.g. a bot token inside a
            # NetworkError URL) BEFORE persisting — never store tokens.
            msg = _scrub_secrets(record.getMessage())
            if record.levelno >= logging.ERROR:
                # Map common PTB polling failures to stable kinds the UI
                # understands. 409 Conflict = a second bot instance polling.
                lower = msg.lower()
                if 'conflict' in lower or '409' in msg:
                    kind = 'conflict_409'
                elif 'networkerror' in lower or 'network error' in lower or 'bad gateway' in lower:
                    kind = 'network_error'
                elif 'invalid token' in lower:
                    kind = 'invalid_token'
                else:
                    kind = 'getupdates_error'
                record_polling_error(kind, msg)
                # AI xato tahlili + staff guruhiga xabar (thread'da, throttled)
                if kind in ('conflict_409', 'network_error', 'invalid_token'):
                    _report_bot_error_to_staff(kind, msg)
        except Exception:
            pass  # logging must never crash the bot


def _report_bot_error_to_staff(kind: str, msg: str):
    """Bot polling xatosi → AI tahlil + staff guruhiga xabar (thread, throttled).

    Fire-and-forget: logging hech qachon bloklanmaydi yoki buzilmaydi.
    Throttle: bir xil turdagi xato 10 daqiqada bir marta xabar qilinadi.
    """
    # PollingErrorHandler async kontekstdan chaqiradi — ORM chaqiruvlari
    # (Setting.get_setting, AuditLog) async kontekstda SynchronousOnlyOperation
    # tashlaydi. Shuning uchun hisobot alohida thread'da ishlaydi.
    import threading

    def _run():
        try:
            from apps.security.ai_ops import report_error_to_staff
            report_error_to_staff(
                {
                    'kind': 'bot_polling',
                    'component': f'bot.py (polling {kind})',
                    'error_code': kind,
                    'detail': msg[:300],
                    'extra': {'polling_kind': kind},
                },
                throttle_key=f'bot_{kind}',
                throttle_seconds=600,
            )
        except Exception:
            pass  # hech qachon logging'ni buzmaydi

    threading.Thread(target=_run, daemon=True).start()


def _heartbeat_loop():
    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        heartbeat()


def _price_sync_loop():
    """Kunlik Fragment narx sinxronlash loopi (bot bilan birga yashaydi).

    Har soatda sync_fragment_prices() ni chaqiradi — u O'ZI qaror qiladi:
    o'chirilgan bo'lsa / 24 soat ichida sinxronlangan bo'lsa o'tkazib
    yuboradi. Shunday qilib barcha mantiq bir joyda (fragment_price_sync).
    Xatoliklar logga yoziladi, bot hech qachon yiqilmaydi.
    """
    time.sleep(30)  # bot ishga tushishini kutamiz (DB tayyor bo'lsin)
    while True:
        try:
            from apps.services.fragment_price_sync import sync_fragment_prices
            result = sync_fragment_prices()
            if result.get('synced'):
                print(f"[SYNC] Fragment narxlar yangilandi: {result['result']}", flush=True)
        except Exception:
            import traceback
            traceback.print_exc()

        time.sleep(PRICE_SYNC_CHECK_INTERVAL)


def _get_bot_config():
    """Read bot config from DB settings (admin panel → Kalitlar)."""
    token = Setting.get_setting('telegram_bot_token', '')
    web_app_url = Setting.get_setting('web_app_url', '')
    support = Setting.get_setting('support_telegram', '@topuphub')
    return token, web_app_url, support


def _get_user_by_tg(tg_id: str):
    """Fetch a user by Telegram ID, or None."""
    try:
        return User.objects.get(telegram_id=tg_id)
    except User.DoesNotExist:
        return None


def _get_balance_info(user):
    return Decimal(user.balance), Decimal(user.cashback_balance)


def _get_recent_orders(user):
    # select_related prefetches service/package so the async handler's
    # attribute reads (o.service.name) never trigger lazy DB queries
    # inside the event loop (SynchronousOnlyOperation guard).
    return list(
        Order.objects.select_related('service', 'package')
        .filter(customer=user).order_by('-created_at')[:5]
    )


# ── Sync-wrapped DB access (safe to await from async handlers) ──
db_bot_config = sync_to_async(_get_bot_config)
db_user_by_tg = sync_to_async(_get_user_by_tg)
db_balance_info = sync_to_async(_get_balance_info)
db_recent_orders = sync_to_async(_get_recent_orders)


def _get_staff_quick_stats(user):
    """
    Quick stats for staff (admins/operators).

    Admins see platform-wide numbers. Operators see:
      • 'pending' = UNASSIGNED pending orders waiting to be picked up (this is
        the pool operators actually work from — scoping pending to their own
        assigned orders would always be 0, since pending orders are by
        definition unassigned)
      • 'processing' + today counts = their own assigned orders (mirrors the
        API permission scoping rules)
    """
    from django.db.models import Sum
    from django.utils import timezone
    from apps.orders.models import Order
    from apps.users.models import Role

    today = timezone.now().date()
    is_admin = user.role in (Role.ADMIN, Role.SUPER_ADMIN)
    if is_admin:
        pending = Order.objects.filter(status='pending').count()
        processing = Order.objects.filter(status='processing').count()
        today_orders = Order.objects.filter(created_at__date=today)
    else:
        pending = Order.objects.filter(
            status='pending', assigned_operator__isnull=True
        ).count()
        processing = Order.objects.filter(
            status='processing', assigned_operator=user
        ).count()
        today_orders = Order.objects.filter(
            assigned_operator=user, created_at__date=today
        )

    today_revenue = (
        today_orders.filter(payment_status='paid')
        .aggregate(total=Sum('total_price'))['total']
        or 0
    )
    return pending, processing, today_orders.count(), float(today_revenue)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start — welcome message with a Web App launch button.

    If the user's Telegram account is already linked to the platform, the
    message also shows their current balance. Staff roles (admins/operators)
    additionally get an Admin panel web-app button and a quick-stats button.

    The Web App URL is read from the DB setting 'web_app_url' and MUST be
    HTTPS (Telegram requires https:// for WebAppInfo). If it is empty or
    invalid, the user gets a clear error message and the server logs it.
    """
    bump(updates=1, messages=1, command='start')
    _, web_app_url, support = await db_bot_config()

    user_name = update.effective_user.first_name or 'do\'stim'
    message = (
        f"🎮 Assalomu alaykum, <b>{user_name}</b>!\n\n"
        f"<b>DONZO</b> — o\'yinlar va raqamli xizmatlar uchun "
        f"tez va ishonchli donat platformasi.\n\n"
        f"🔥 O\'yinlar: PUBG, Mobile Legends, Free Fire, Steam, "
        f"Telegram Premium va boshqalar!\n"
        f"⚡ Tez yetkazib berish, qulay to\'lov, 24/7 qo\'llab-quvvatlash.\n\n"
        f"👇 Quyidagi tugmani bosib o\'yinga donat qilishni boshlang!"
    )

    # ── Linked user? Show balance + staff extras in /start ──
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is not None:
        balance_val, cashback = await db_balance_info(user)
        message += (
            f"\n\n💰 <b>Balansingiz:</b> {balance_val:,.0f} so'm"
            f"\n🎁 Keshbek: {cashback:,.0f} so'm"
        )

    buttons = []
    # SECURITY/UX: Telegram only accepts HTTPS Web App URLs. If the DB value is
    # empty or not HTTPS, DO NOT show a broken button — tell the user clearly
    # and log the problem server-side.
    if web_app_url and str(web_app_url).startswith('https://'):
        buttons.append([
            InlineKeyboardButton(
                "🚀 Web App'ni ochish",
                web_app=WebAppInfo(url=str(web_app_url)),
            )
        ])
    elif web_app_url and not str(web_app_url).startswith('https://'):
        print(f"[BOT] XATO: web_app_url HTTPS emas: {web_app_url!r}")
        await update.effective_message.reply_html(
            "⚠️ <b>Web App hozircha sozlanmagan.</b>\n\n"
            "Admin panel → <b>Kalitlar</b> sahifasida <code>web_app_url</code> "
            "https:// bilan boshlanadigan manzil bo'lishi kerak.\n\n"
            "Iltimos, birozdan so'ng qayta urinib ko'ring."
        )
        return
    else:
        print("[BOT] XATO: web_app_url bo'sh — bot Web App tugmasini ko'rsata olmaydi")
        await update.effective_message.reply_html(
            "⚠️ <b>Web App hozircha sozlanmagan.</b>\n\n"
            "Admin panel → <b>Kalitlar</b> sahifasida <code>web_app_url</code> "
            "kiritilmagan. Administrator bilan bog'laning.\n\n"
            "Iltimos, birozdan so'ng qayta urinib ko'ring."
        )
        return

    buttons.append([
        InlineKeyboardButton("🔑 Kod olish", callback_data='login'),
        InlineKeyboardButton("💰 Balansim", callback_data='balance'),
    ])
    buttons.append([
        InlineKeyboardButton("📦 Buyurtmalarim", callback_data='orders'),
        InlineKeyboardButton("💳 To\'lov ma'lumotlari", callback_data='payment_info'),
    ])

    # ── Staff extras: Admin panel web-app button + quick stats ──
    if user is not None and user.role in STAFF_ROLES:
        staff_row = []
        if web_app_url and str(web_app_url).startswith('https://'):
            panel_path = {
                'super_admin': '/admin',
                'admin': '/admin',
                'senior_operator': '/operator',
                'operator': '/operator',
                'support': '/support',
            }.get(user.role, '/admin')
            staff_row.append(
                InlineKeyboardButton(
                    "🛠️ Admin panel",
                    web_app=WebAppInfo(url=f"{str(web_app_url).rstrip('/')}{panel_path}"),
                )
            )
        staff_row.append(InlineKeyboardButton("📊 Tezkor statistika", callback_data='staff_stats'))
        if staff_row:
            buttons.append(staff_row)

    if support:
        buttons.append([
            InlineKeyboardButton("🆘 Yordam", url=f'https://t.me/{support.lstrip("@")}')
        ])

    await update.effective_message.reply_html(
        message,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/balance — show the user's current balance."""
    bump(updates=1, messages=1, command='balance')
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is None:
        await update.effective_message.reply_html(
            "❌ Sizning hisobingiz botga ulanmagan.\n\n"
            "Web app orqali Telegram orqali kirish tugmasi bilan "
            "kirganingizda hisob avtomatik ulanadi."
        )
        return

    balance_val, cashback = await db_balance_info(user)
    await update.effective_message.reply_html(
        f"💰 <b>Hisobingiz</b>\n\n"
        f"• Asosiy balans: <b>{balance_val:,.0f}</b> so\'m\n"
        f"• Keshbek: <b>{cashback:,.0f}</b> so\'m\n\n"
        f"Balansni to\'ldirish uchun web app\'ni oching 👇"
    )


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/orders — show the user's last 5 orders."""
    bump(updates=1, messages=1, command='orders')
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is None:
        await update.effective_message.reply_html(
            "❌ Hisobingiz botga ulanmagan. Avval web app orqali kiring."
        )
        return

    recent = await db_recent_orders(user)
    if not recent:
        await update.effective_message.reply_html(
            "📦 Buyurtmalaringiz hozircha yo\'q.\n\n"
            "O\'yin donatlarini boshlash uchun web app\'ni oching!"
        )
        return

    status_emoji = {
        'pending': '🕐 Kutilmoqda',
        'processing': '⚙️ Bajarilmoqda',
        'completed': '✅ Tugallangan',
        'cancelled': '❌ Bekor qilingan',
    }
    lines = ["📦 <b>Oxirgi buyurtmalaringiz</b>\n"]
    for o in recent:
        lines.append(
            f"#{o.order_number} — <b>{o.service.name if o.service else 'Xizmat'}</b>\n"
            f"   {status_emoji.get(o.status, o.status)} • "
            f"{Decimal(o.total_price):,.0f} so\'m"
        )
    lines.append("\nBatafsil ma\'lumot: web app → Buyurtmalar")
    await update.effective_message.reply_html('\n'.join(lines))


def _create_login_code(tg_id, tg_username, first_name, last_name, language_code):
    """Create a fresh one-time login code for a Telegram user (sync, ORM-safe).
    Delegates to the shared helper so bot.py and the web app send-code
    endpoint can never drift apart."""
    from apps.users.code_utils import create_login_code
    return create_login_code(tg_id, tg_username, first_name, last_name, language_code)


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /login — send a one-time login code to the user's chat.

    The code is valid for 5 minutes and can be used ONCE in the web app
    ('Kod orqali kirish') — including from a regular desktop/mobile browser,
    not only from inside Telegram. This is the fallback login method when
    the WebApp auto-login is unavailable (e.g. browser outside Telegram).
    """
    bump(updates=1, messages=1, command='login')
    user_info = update.effective_user
    tg_id = str(user_info.id)
    tg_username = user_info.username or ''
    first_name = user_info.first_name or ''
    last_name = user_info.last_name or ''
    language_code = user_info.language_code or ''

    code_obj = await sync_to_async(_create_login_code)(
        tg_id, tg_username, first_name, last_name, language_code,
    )
    code = code_obj.plain_code

    await update.effective_message.reply_html(
        f"🔑 <b>Kirish kodingiz</b>\n\n"
        f"<code>{code}</code>\n\n"
        f"🕐 Kod <b>5 daqiqa</b> yaroqli va faqat <b>bir marta</b> ishlatiladi.\n\n"
        f"Endi web app'da (yoki istalgan brauzerda) <b>Kod orqali kirish</b> "
        f"bo'limiga o'tib, shu kodni kiriting — hisobingiz avtomatik ulanadi."
    )


async def payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    💳 To'lov ma'lumotlari — explain how payments work on the platform
    (balance top-up → admin approval → pay from balance). Also shows the
    user's current balance when linked.
    """
    bump(updates=1, messages=1, command='button:payment_info')
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)

    lines = [
        "💳 <b>To\'lov tizimi</b>\n",
        "DONZO'da to\'lovlar <b>balans orqali</b> amalga oshiriladi:",
        "",
        "1️⃣ Web app → <b>Giftlar</b> bo'limiga o'ting",
        "2️⃣ Miqdorni tanlang → <b>Balansni to'ldirish</b>",
        "3️⃣ Operatorga pul o'tkazing va tasdiqni kuting",
        "4️⃣ Operator tasdiqlagach balans hisobingizga tushadi",
        "",
        "⚡ Keyin istalgan o'yinga donat qilishda to'lov to'g'ridan-to'g'ri "
        "balansingizdan yechiladi.",
    ]
    if user is not None:
        balance_val, cashback = await db_balance_info(user)
        lines += [
            "",
            f"💰 <b>Joriy balans:</b> {balance_val:,.0f} so'm",
            f"🎁 Keshbek: {cashback:,.0f} so'm",
        ]
    lines.append("")
    lines.append("Balansni to'ldirish uchun web app'ni oching 👇")
    await update.effective_message.reply_html('\n'.join(lines))


async def staff_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """📊 Tezkor statistika — quick staff-only stats from the live database.

    Admins see platform-wide numbers; operators see only their own assigned
    orders (mirrors the API permission scoping). Non-staff users get a
    friendly "no access" reply.
    """
    bump(updates=1, messages=1, command='button:staff_stats')
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is None or user.role not in STAFF_ROLES:
        await update.effective_message.reply_html(
            "❌ Bu bo'lim faqat xodimlar uchun.\n\n"
            "Foydalanuvchi sifatida <b>Balansim</b> yoki <b>Buyurtmalarim</b> "
            "tugmalaridan foydalaning."
        )
        return

    pending, processing, today_orders, today_revenue = await sync_to_async(
        _get_staff_quick_stats
    )(user)

    scope = 'platforma bo\'ylab' if user.role in ('super_admin', 'admin') else 'sizning buyurtmalaringiz'
    await update.effective_message.reply_html(
        f"📊 <b>Tezkor statistika</b> ({scope})\n\n"
        f"🕐 Kutilayotgan (qabul qilinmagan): <b>{pending}</b>\n"
        f"⚙️ Bajarilmoqda: <b>{processing}</b>\n"
        f"📅 Bugungi buyurtmalar: <b>{today_orders}</b>\n"
        f"💰 Bugungi tushum: <b>{today_revenue:,.0f} so'm</b>\n\n"
        f"Batafsil panel: 🛠️ Admin panel tugmasi orqali."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — list available commands."""
    bump(updates=1, messages=1, command='help')
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    is_staff = user is not None and user.role in STAFF_ROLES
    lines = [
        "🤖 <b>DONZO bot — buyruqlar</b>\n",
        "/start — bosh sahifa",
        "/balance — balansni ko'rish",
        "/orders — buyurtmalarim",
        "/login — kirish kodi olish (brauzerda kirish)",
        "/help — yordam",
    ]
    if is_staff:
        lines += [
            "",
            "🛠️ <b>Xodimlar uchun:</b>",
            "/status — tizim holati (backend, tunnel, bot, DB)",
            "/xato — oxirgi xatolar ro'yxati",
            "/tahlil — AI xatolarni tahlil qiladi (qayerdan + tuzatish)",
            "/tunnel — joriy tunnel URL",
        ]
        if user.role in ('super_admin', 'admin'):
            lines += [
                "/togrila — AVTO-TUZATISH (ishlamayotgan komponentlarni tiklaydi)",
                "/restart backend|tunnel|bot|userclient|watchdog — komponent restart",
            ]
    lines += ["", "🚀 Eng asosiy: <b>Donat qilishni boshlash</b> tugmasi orqali web app'ga o'ting!"]
    await update.effective_message.reply_html('\n'.join(lines))


# ── STAFF COMMANDS ────────────────────────────────────────────────────────
# These commands are ONLY for staff (admin/super_admin) Telegram chats.
# They are the /togrila auto-fix loop: system health → AI diagnosis → fix.

async def _require_staff(update: Update) -> bool:
    """Staff tekshiruvi. Returns True if the user may use staff commands."""
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is None or user.role not in STAFF_ROLES:
        await update.effective_message.reply_html(
            "❌ Bu buyruq faqat xodimlar uchun."
        )
        return False
    return True


async def _require_admin(update: Update) -> bool:
    """Admin tekshiruvi (auto-fix / restart faqat adminlar uchun)."""
    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is None or user.role not in ('super_admin', 'admin'):
        await update.effective_message.reply_html(
            "❌ Bu buyruq faqat adminlar uchun."
        )
        return False
    return True


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/status — tizim holati (backend, tunnel, bot, user client, DB)."""
    bump(updates=1, messages=1, command='status')
    if not await _require_staff(update):
        return
    from apps.security.system_health import format_health_report
    msg = await sync_to_async(format_health_report)()
    await update.effective_message.reply_html(msg)


async def xato_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/xato — oxirgi xatolar (AuditLog'dan)."""
    bump(updates=1, messages=1, command='xato')
    if not await _require_staff(update):
        return
    from apps.security.system_health import recent_errors
    errors = await sync_to_async(recent_errors)(5)
    if not errors:
        await update.effective_message.reply_html(
            "📋 <b>Oxirgi xatolar</b>\n\n✅ Xatolar topilmadi. Hammasi joyida!"
        )
        return
    lines = ["📋 <b>Oxirgi xatolar</b>\n"]
    for e in errors:
        lines.append(
            f"🕐 {e['time'].strftime('%d.%m %H:%M')} — <b>{e['action']}</b>\n"
            f"   {e['description']}"
        )
    lines.append("\n🧠 AI tahlil: /tahlil · Avto-tuzatish: /togrila")
    await update.effective_message.reply_html('\n'.join(lines))


async def tahlil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tahlil — AI oxirgi xatolarni tahlil qiladi (qayerdan + qanday tuzatish)."""
    bump(updates=1, messages=1, command='tahlil')
    if not await _require_staff(update):
        return
    from apps.security.system_health import recent_errors
    from apps.security.ai_ops import analyze_error
    errors = await sync_to_async(recent_errors)(3)
    if not errors:
        await update.effective_message.reply_html(
            "🧠 <b>AI tahlil</b>\n\n✅ Xatolar yo'q — tahlil qilishga hech narsa yo'q."
        )
        return
    await update.effective_message.reply_html(
        "🧠 <b>AI tahlil boshlandi...</b>\n\nOxirgi xatolar Gemini'ga yuborilmoqda. Bu 10-25 soniya oladi."
    )
    context = {
        'kind': 'audit_errors',
        'component': 'AuditLog',
        'detail': '; '.join(e['description'] for e in errors)[:700],
        'extra': {'errors': [{'action': e['action'], 'time': str(e['time'])} for e in errors]},
    }
    result = await sync_to_async(analyze_error)(context)
    if not result.get('ok'):
        await update.effective_message.reply_html(
            f"❌ AI tahlil qila olmadi ({result.get('error', 'xato')}).\n\n"
            f"Boshqa yo'l: /status (holat) · /togrila (avto-tuzatish)"
        )
        return
    sev_icon = {'CRITICAL': '🔴', 'HIGH': '🟠', 'MEDIUM': '🟡', 'LOW': '🔵'}.get(result['severity'], '⚪')
    lines = [
        f"🧠 <b>AI tahlil natijasi</b>\n",
        f"{sev_icon} <b>Qayerdan:</b> {result['root_cause']}\n",
        f"🛠️ <b>Qanday tuzatish:</b>",
    ]
    for i, step in enumerate(result['fix_steps'], 1):
        lines.append(f"  {i}. {step}")
    if result['auto_fixable']:
        lines.append("\n✅ Avto-tuzatish mumkin → /togrila")
    else:
        lines.append("\n⚠️ Qo'lda tuzatish kerak.")
    await update.effective_message.reply_html('\n'.join(lines))


async def togrila_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /togrila — AVTO-TUZATISH: tizim holatini tekshiradi va ishlamayotgan
    komponentlarni (backend, tunnel, bot, user client) avtomatik tiklaydi.
    Faqat admin/super_admin uchun.
    """
    bump(updates=1, messages=1, command='togrila')
    if not await _require_admin(update):
        return
    await update.effective_message.reply_html(
        "🔧 <b>Avto-tuzatish boshlandi...</b>\n\n"
        "Tizim holati tekshirilmoqda va ishlamayotgan komponentlar "
        "qayta ishga tushirilmoqda. Bu 20-60 soniya oladi."
    )
    from apps.security.auto_fix import run_auto_fix, format_fix_report
    username = update.effective_user.username or str(update.effective_user.id)
    result = await sync_to_async(run_auto_fix)(username)
    await update.effective_message.reply_html(format_fix_report(result))


async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/restart <komponent> — komponentni qayta ishga tushiradi.

    Komponentlar: backend · tunnel · bot · userclient · watchdog (hammasi).
    Faqat admin/super_admin uchun.
    """
    bump(updates=1, messages=1, command='restart')
    if not await _require_admin(update):
        return
    args = (context.args or [])
    target = (args[0] if args else 'hammasi').lower()
    valid = ('backend', 'tunnel', 'bot', 'userclient', 'watchdog', 'hammasi')
    if target not in valid:
        await update.effective_message.reply_html(
            "ℹ️ <b>/restart</b> — komponentni qayta ishga tushiradi\n\n"
            "Ishlatish: <code>/restart backend</code>\n\n"
            "Komponentlar: <b>backend · tunnel · bot · userclient · watchdog · hammasi</b>"
        )
        return
    await update.effective_message.reply_html(
        f"🔄 <b>{target}</b> qayta ishga tushirilmoqda..."
    )

    import subprocess as sp
    import os
    script = None
    if target in ('backend',):
        script = 'restart_backend.ps1'
    elif target in ('tunnel',):
        script = 'restart_tunnels.ps1'
    elif target in ('bot',):
        script = 'restart_bot.ps1'
    elif target in ('userclient',):
        script = 'restart_user_client.ps1' if os.path.exists('restart_user_client.ps1') else None
    elif target in ('watchdog', 'hammasi'):
        script = 'donzo_watchdog.ps1'

    if not script or not os.path.exists(script):
        await update.effective_message.reply_html(
            f"❌ {target} uchun script topilmadi. Boshqa yo'l: /togrila (to'liq avto-tuzatish)"
        )
        return

    try:
        proc = await asyncio.to_thread(
            sp.run, ['powershell', '-ExecutionPolicy', 'Bypass', '-File', script],
            capture_output=True, text=True, timeout=120,
        )
        ok = proc.returncode == 0
        detail = (proc.stdout or '').strip()[-200:] or (proc.stderr or '').strip()[-200:]
        await update.effective_message.reply_html(
            f"{'✅' if ok else '❌'} <b>{target}</b> qayta ishga tushirildi"
            + (f"\n<code>{detail}</code>" if detail and not ok else "")
        )
    except Exception as exc:
        await update.effective_message.reply_html(
            f"❌ {target} restartda xatolik: {str(exc)[:150]}"
        )


async def tunnel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tunnel — joriy tunnel URL va holati."""
    bump(updates=1, messages=1, command='tunnel')
    if not await _require_staff(update):
        return
    from apps.security.system_health import check_tunnel, get_tunnel_url
    info = await sync_to_async(check_tunnel)()
    url = await sync_to_async(get_tunnel_url)()
    lines = ["🌐 <b>Tunnel holati</b>\n"]
    if info['status'] == 'ok':
        lines.append(f"✅ Tunnel ishlayapti\n\nURL: <code>{url}</code>")
    else:
        lines.append(f"🔴 Tunnel ishlamayapti ({info['detail']})\n\nTuzatish: /togrila")
    lines.append("\n⚠️ Eslatma: URL o'zgarsa Vercel avtomatik qayta deploy qilinadi.")
    await update.effective_message.reply_html('\n'.join(lines))


async def _security_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Security Center inline buttons: sec:<incident_id>:<action>.

    Only admin/super_admin Telegram accounts may act. Actions are executed
    through services.resolve_incident() — the DECISION ENFORCER. The bot
    never invents a decision; it only relays a human's click.
    """
    query = update.callback_query
    parts = query.data.split(':')
    if len(parts) != 3 or parts[0] != 'sec':
        await query.answer('Noma\'lum tugma')
        return
    _, incident_id, action = parts

    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    if user is None or user.role not in ('super_admin', 'admin'):
        await query.answer('❌ Bu harakat faqat adminlar uchun')
        return

    from apps.security import services as sec_services
    if action == 'ack':
        await sync_to_async(alerts_ack)(incident_id, user.username)
        await query.answer('✅ Incident qabul qilindi')
        return
    if action == 'case':
        from apps.security.models import SecurityCase, SecurityIncident
        try:
            inc = await sync_to_async(SecurityIncident.objects.get)(pk=int(incident_id))
            case = await sync_to_async(SecurityCase.objects.create)(
                severity='HIGH', assigned_admin=user,
                admin_notes='Telegram tugmasi orqali ochildi')
            await sync_to_async(case.incidents.add)(inc)
            if inc.user:
                await sync_to_async(case.users.add)(inc.user)
            await sync_to_async(inc.add_timeline)('case_created', case.case_id)
            await query.answer(f'📁 Case {case.case_id} ochildi')
        except Exception:
            await query.answer('Case ochishda xatolik')
        return

    if action not in ('approve', 'reject', 'block', 'keep'):
        await query.answer('Noma\'lum harakat')
        return

    result = await sync_to_async(sec_services.resolve_incident)(
        int(incident_id), user, action, note=f"Telegram tugmasi ({user.username})")
    if result.get('ok'):
        label = {'approve': '✅ Tasdiqlandi, balansga tushdi',
                 'reject': '❌ Rad etildi',
                 'block': '🚫 Foydalanuvchi bloklandi',
                 'keep': '⏸ Hold saqlandi'}.get(action, 'OK')
        await query.answer(label)
        try:
            await query.edit_message_text(
                (query.message.text or '') + f"\n\n✅ <b>{label}</b> — {user.username}",
                parse_mode='HTML')
        except Exception:
            pass
    else:
        await query.answer(result.get('detail', 'Xatolik'))


def alerts_ack(incident_id, actor):
    from apps.security.alerts import acknowledge_incident
    acknowledge_incident(int(incident_id), actor)


async def _suspicious_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle suspicious-payment inline buttons: sp:<suspicious_id>:<action>.

    Sent by notify_staff_suspicious_payment() directly to staff chats.
    Only staff Telegram accounts (admin/operator/support) may act. The
    balance only moves through the cardpay decision enforcer — the bot
    never invents a decision, it relays a human's click.
    """
    query = update.callback_query
    parts = query.data.split(':')
    if len(parts) != 3 or parts[0] != 'sp':
        await query.answer('Noma\'lum tugma')
        return
    _, sp_id, action = parts

    tg_id = str(update.effective_user.id)
    user = await db_user_by_tg(tg_id)
    # Every staff member RECEIVES the alert, but only admins may MOVE MONEY
    # (the admin-panel approve endpoint is admin-only — keep parity).
    if user is None or user.role not in ('super_admin', 'admin'):
        await query.answer('❌ Bu harakat faqat adminlar uchun')
        return

    from apps.cardpay import services as cardpay_services
    note = f"Telegram tugmasi ({user.username})"
    # decided_by is a User FK — pass the model instance, not a string.
    if action == 'approve':
        result = await sync_to_async(cardpay_services.approve_suspicious)(int(sp_id), user)
        label = '✅ Tasdiqlandi, balansga tushdi' if result.get('ok') else result.get('detail', 'Xatolik')
        await query.answer(label)
    elif action == 'reject':
        result = await sync_to_async(cardpay_services.reject_suspicious)(int(sp_id), user, note)
        label = '❌ Rad etildi' if result.get('ok') else result.get('detail', 'Xatolik')
        await query.answer(label)
    else:
        await query.answer('Noma\'lum harakat')
        return

    try:
        await query.edit_message_text(
            (query.message.text or '') + f"\n\n✅ <b>{label}</b> — {user.username}",
            parse_mode='HTML')
    except Exception:
        pass


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button clicks (balance / orders / security / suspicious)."""
    query = update.callback_query
    bump(updates=1, messages=1, command=f'button:{query.data}')
    await query.answer()

    if query.data.startswith('sec:'):
        await _security_callback(update, context)
    elif query.data.startswith('sp:'):
        await _suspicious_callback(update, context)
    elif query.data == 'staff:togrila':
        # AI xato xabari ichidagi "🔧 Avto-tuzatish" tugmasi
        await togrila_command(update, context)
    elif query.data == 'login':
        await login(update, context)
    elif query.data == 'balance':
        await balance(update, context)
    elif query.data == 'orders':
        await orders(update, context)
    elif query.data == 'payment_info':
        await payment_info(update, context)
    elif query.data == 'staff_stats':
        await staff_stats(update, context)


def main():
    # Python 3.12+ no longer auto-creates an event loop in the main thread.
    # python-telegram-bot's run_polling() calls asyncio.get_event_loop(), so
    # we must create and set one explicitly first.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    # Capture getUpdates errors (409/NetworkError/...) into bot-stats.json so
    # the admin panel can show live polling health.
    logging.getLogger('telegram').addHandler(PollingErrorHandler())

    token, web_app_url, support = _get_bot_config()
    if not token:
        print("[BOT] XATO: Telegram bot token sozlanmagan!")
        print("[BOT] Admin panel → Kalitlar sahifasida 'telegram_bot_token' ni kiriting.")
        set_token_status(False, detail='Token sozlanmagan (bo\'sh)')
        sys.exit(1)

    print(f"[BOT] DONZO Telegram bot ishga tushmoqda...")
    print(f"[BOT] Token: {token[:12]}...")
    print(f"[BOT] Web App: {web_app_url or '(sozlanmagan)'}")
    print(f"[BOT] Support: {support}")
    print("[BOT] Kutilmoqda... (Ctrl+C bilan to'xtatiladi)")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('balance', balance))
    application.add_handler(CommandHandler('orders', orders))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('login', login))
    # ── Staff commands (AI ops) ──
    application.add_handler(CommandHandler('status', status_command))
    application.add_handler(CommandHandler('xato', xato_command))
    application.add_handler(CommandHandler('tahlil', tahlil_command))
    application.add_handler(CommandHandler('togrila', togrila_command))
    application.add_handler(CommandHandler('restart', restart_command))
    application.add_handler(CommandHandler('tunnel', tunnel_command))
    application.add_handler(CallbackQueryHandler(callback_handler))

    # ── Token validation (getMe) — records valid/invalid so the admin
    #    panel can show live token status instead of only a prefix. ──
    async def _validate_token():
        try:
            me = await application.bot.get_me()
            set_token_status(
                True,
                username=me.username or '',
                detail=f"@{me.username} · {me.first_name}",
            )
            print(f"[BOT] Token OK: @{me.username}")
        except Exception as exc:
            # Never persist the raw exception: a low-level NetworkError could
            # embed the request URL containing the bot token. Scrub it.
            safe_detail = _scrub_secrets(str(exc))[:200]
            set_token_status(False, detail=safe_detail)
            print(f"[BOT] XATO: token tekshiruvi muvaffaqiyatsiz: {safe_detail}")

    try:
        asyncio.get_event_loop().run_until_complete(_validate_token())
    except Exception:
        pass  # non-fatal — run_polling will surface a real InvalidToken

    # Record startup time + restart count, then start the heartbeat thread.
    mark_started()
    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    print("[BOT] Stats: .freebuff/bot-stats.json (heartbeat har 30s)")

    # Fragment live-price sync (kuniga bir marta) — bot bilan parallel ishlaydi.
    threading.Thread(target=_price_sync_loop, daemon=True).start()
    print("[BOT] Fragment narx sinxronlash: har 24 soatda (bot orqali)")

    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        # DB'dagi token noto'g'ri (placeholder bo'lib qolgan). Aniq xabar
        # beramiz va maxsus exit code (2) bilan chiqamiz — supervisor buni
        # ko'rib, tez-tez restart qilmaydi.
        print("=" * 60)
        print("[BOT] XATO: Telegram bot token NOTO'G'RI yoki rad etildi!")
        print("[BOT] Hozirgi token: " + (token[:12] + '...' if token else "(bo'sh)"))
        print("[BOT] Buni tuzatish uchun:")
        print("[BOT]   1) @BotFather -> /mybots -> sizning bot -> API Token")
        print("[BOT]   2) Tokenni Admin panel -> Kalitlar -> 'telegram_bot_token'")
        print("[BOT]      ga yozib saqlang.")
        print("[BOT]   3) Bot 1 daqiqa ichida avtomatik qayta ishga tushadi.")
        print("=" * 60)
        sys.exit(2)


if __name__ == '__main__':
    main()
