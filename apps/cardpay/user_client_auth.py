"""
DONZO User Client — admin-panel Telegram login (Telethon).

Lets the admin log their PERSONAL Telegram account into the card-payment
user client straight from the admin panel (To'lov nazorati → User Client):

    POST /start/    {phone}      → send_code_request → Telegram sends the code
    POST /verify/   {code}       → sign_in (saves the session file)
    POST /password/ {password}   → 2FA step, if SessionPasswordNeeded
    POST /logout/               → delete the session + stop the worker
    GET  /status/               → authorized? phone? username? worker online?

Security notes:
  • admin-only (permissions enforced in the views);
  • the phone code and the phone_code_hash are kept in-memory only, never
    logged, never stored in the DB;
  • every operation uses a FRESH TelegramClient on its own event loop, so
    concurrent requests can never share/race a client; the phone_code_hash
    ties the code to the original send_code_request;
  • the session file (sessions/donzo_user.session) is a secret — it is the
    equivalent of a logged-in Telegram session and grants full access to
    the account. It is never exposed through any endpoint.
"""
import asyncio
import base64
import logging
import os
import re
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # backend/
SESSION_DIR = os.path.join(BASE_DIR, 'sessions')
SESSION_FILE = os.path.join(SESSION_DIR, 'donzo_user.session')
# Login wizard uses a SEPARATE temp session file. The worker (user_client.py)
# may be running with (or restarting onto) SESSION_FILE at the same time the
# admin is logging in — sharing the file causes "database is locked" or a
# stale (blocked) auth key, surfacing as "kod tekshirilmadi". The wizard
# signs into the temp file, then on success copies it over SESSION_FILE.
LOGIN_SESSION_FILE = os.path.join(SESSION_DIR, 'donzo_user_login.session')

# Login wizard state is stored in the DB (Setting), NOT in memory:
# daphne runs several worker processes in the cloud, each with its own
# memory — a phone/code_hash written by one worker would be invisible to
# the worker that handles the "verify code" request (→ "kod topilmadi").
# DB keys (plaintext, short-lived, never logged):
_KC_PHONE = 'user_client_login_phone'
_KC_HASH = 'user_client_login_code_hash'
_KC_2FA = 'user_client_login_needs_password'
_KC_TS = 'user_client_login_started_at'
_LOGIN_TTL_SECONDS = 10 * 60  # code expires in ~5 min; allow 10 min total

# In-memory login state (never persisted, never logged).
_LOCK = threading.Lock()
_PHONE = ''
_PHONE_CODE_HASH = ''
_NEEDS_PASSWORD = False


def _get_login_state():
    """Read the pending login wizard state (DB-backed, cross-worker).

    Reads STRAIGHT from the DB, never from the Setting TTL cache: daphne
    runs multiple worker processes, each with its OWN in-memory cache. If
    start_phone writes on worker A and verify_code lands on worker B, B's
    cache can still hold the OLD empty value for up to 3s → "kod
tekshirilmadi". A direct .objects query always sees the fresh row.
    """
    from apps.settings_app.models import Setting
    rows = dict(Setting.objects.filter(
        key__in=(_KC_PHONE, _KC_HASH, _KC_2FA, _KC_TS)
    ).values_list('key', 'value'))
    phone = rows.get(_KC_PHONE) or ''
    code_hash = rows.get(_KC_HASH) or ''
    needs_2fa = (rows.get(_KC_2FA) or '').lower() == 'true'
    ts_raw = rows.get(_KC_TS) or ''
    try:
        ts = float(ts_raw)
        if time.time() - ts > _LOGIN_TTL_SECONDS:
            _clear_login_state()
            return '', '', False
    except (TypeError, ValueError):
        pass
    return phone, code_hash, needs_2fa


def _set_login_state(phone, code_hash='', needs_2fa=False):
    """Persist the login wizard state so any daphne worker can continue."""
    from apps.settings_app.models import Setting
    Setting.set_setting(_KC_PHONE, phone or '')
    Setting.set_setting(_KC_HASH, code_hash or '')
    Setting.set_setting(_KC_2FA, 'true' if needs_2fa else 'false')
    Setting.set_setting(_KC_TS, str(time.time()))


def _clear_login_state():
    from apps.settings_app.models import Setting
    for key in (_KC_PHONE, _KC_HASH, _KC_2FA, _KC_TS):
        Setting.set_setting(key, '')


def _sync_session_to_db(session_file=None):
    """Muvaffaqiyatli kirishdan keyin sessiyani Neon DB'ga saqlaydi.

    Cloud deploy'da launcher sessiyani Neon'dan tiklaydi — yangi yozilgan
    sessiya DB'ga qaytarilmasa keyingi restart eski (bloklangan) sessiyaga
    qaytadi. Bu funksiya har muvaffaqiyatli login'da shu muammoni yopadi.
    """
    try:
        path = session_file or SESSION_FILE
        if not os.path.exists(path):
            return
        with open(path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
        from apps.settings_app.models import Setting
        Setting.set_setting('user_client_session_b64', b64)
        logger.info('Sessiya Neon DB\'ga sinxronlandi (%s belgi)', len(b64))
    except Exception:
        pass


def _promote_login_session():
    """Login wizard muvaffaqiyatli bo'lgach temp sessiyani asosiy faylga
    ko'chiradi (va Neon DB'ga yozadi). Worker keyingi restartda yangi
    sessiyani oladi.
    """
    try:
        if not os.path.exists(LOGIN_SESSION_FILE):
            return
        tmp = SESSION_FILE + '.new'
        with open(LOGIN_SESSION_FILE, 'rb') as f:
            data = f.read()
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, SESSION_FILE)
        _sync_session_to_db(SESSION_FILE)
        logger.info('Login sessiyasi asosiy sessiya fayliga ko\'chirildi')
    except Exception as exc:
        logger.warning('sessiyani ko\'chirishda xato: %s', exc)
    finally:
        try:
            if os.path.exists(LOGIN_SESSION_FILE):
                os.remove(LOGIN_SESSION_FILE)
        except Exception:
            pass

# Bound every network operation — a stuck Telegram connection must never
# hold an admin API request for minutes.
OP_TIMEOUT = 30


def _kill_worker_crossplatform() -> None:
    """Kill running user_client.py worker processes (any OS).

    Windows → PowerShell CIM; Linux/macOS → pkill. The cloud launcher
    auto-restarts the worker after a successful login.
    """
    if os.name == 'nt':
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'user_client\\.py' -and "
            "$_.CommandLine -notmatch 'supervisor' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
            "Write-Output $_.ProcessId }"
        )
        try:
            out = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps],
                capture_output=True, text=True, timeout=15,
            ).stdout or ''
        except Exception as exc:
            logger.warning('kill worker (powershell) failed: %s', exc)
            return
        for line in out.splitlines():
            line = line.strip()
            if line.isdigit():
                logger.info('user_client worker killed (pid %s)', line)
    else:
        try:
            subprocess.run(
                ['pkill', '-f', 'user_client\.py'],
                capture_output=True, text=True, timeout=15,
            )
            logger.info('user_client worker pkill yuborildi (cloud)')
        except Exception as exc:
            logger.warning('kill worker (pkill) failed: %s', exc)


def _get_credentials():
    """api_id / api_hash from DB Settings, falling back to .env."""
    from apps.settings_app.models import Setting
    api_id = (Setting.get_setting('telegram_api_id', '') or '').strip() \
        or (os.getenv('TELEGRAM_API_ID', '') or '').strip()
    api_hash = (Setting.get_setting('telegram_api_hash', '') or '').strip() \
        or (os.getenv('TELEGRAM_API_HASH', '') or '').strip()
    return api_id, api_hash


def _run(coro, timeout=OP_TIMEOUT):
    """Run one asyncio coroutine in its own fresh event loop.

    Works both from a sync context (manage.py runserver) and from an async
    server (daphne): if there is already a running event loop, the coroutine
    runs in a dedicated thread with its own loop — asyncio.run() would raise
    "cannot be called from a running event loop" inside daphne.
    """
    async def _guarded():
        return await asyncio.wait_for(coro, timeout=timeout)

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        return asyncio.run(_guarded())

    result = {}

    def _worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result['value'] = loop.run_until_complete(_guarded())
        except BaseException as exc:
            result['error'] = exc
        finally:
            loop.close()

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_worker)
        try:
            future.result(timeout=timeout + 5)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f'user client operation timed out ({timeout}s)')
    if 'error' in result:
        raise result['error']
    return result['value']


async def _make_client(session_file=None):
    """Build a fresh TelegramClient. Async because credentials live in the
    DB (Settings) — reading them inside a running event loop must go
    through sync_to_async, otherwise Django raises SynchronousOnlyOperation.

    session_file=None → the worker session (SESSION_FILE). Pass
    LOGIN_SESSION_FILE during the login wizard so a concurrent worker can
    never lock the file the wizard is writing.
    """
    from asgiref.sync import sync_to_async
    from telethon import TelegramClient
    # thread_sensitive=False: under daphne the sync view runs in a worker
    # thread where asgiref's CurrentThreadExecutor cannot submit to itself.
    api_id, api_hash = await sync_to_async(_get_credentials, thread_sensitive=False)()
    if not api_id or not api_hash:
        return None, None
    try:
        api_id_int = int(api_id)
    except (TypeError, ValueError):
        logger.error("telegram_api_id raqam emas — Kalitlar bo'limida tekshiring")
        return None, None
    os.makedirs(SESSION_DIR, exist_ok=True)
    client = TelegramClient(session_file or SESSION_FILE, api_id_int, api_hash)
    return client, api_id


# ── status ────────────────────────────────────────────────────────────────

def get_status() -> dict:
    """Authorized? phone/username? worker online? (no secrets)."""
    import json
    from datetime import datetime, timezone
    from pathlib import Path

    stats = {}
    try:
        stats_path = Path(BASE_DIR).parents[0] / '.freebuff' / 'user-client-stats.json'
        if stats_path.exists():
            stats = json.loads(stats_path.read_text(encoding='utf-8'))
    except Exception:
        pass

    online = False
    try:
        hb = stats.get('last_heartbeat')
        if hb:
            dt = datetime.fromisoformat(hb)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            online = (datetime.now(timezone.utc) - dt).total_seconds() < 180
    except Exception:
        pass

    session_exists = os.path.exists(SESSION_FILE)

    # CRITICAL: while the worker is ONLINE it holds the Telethon session
    # file. Opening a second client here (same file) deadlocks on SQLite
    # ("database is locked") — which previously surfaced as a bogus
    # "KIRILMAGAN" status. So: worker online → trust the auth state the
    # worker itself wrote to stats (it only runs after a successful login).
    db_phone, _db_hash, _db_2fa = _get_login_state()
    if online:
        account = stats.get('account') or {}
        return {
            'authorized': bool(stats.get('authorized')),
            'credentials': True,
            'session_exists': session_exists,
            'worker_online': True,
            'last_heartbeat': stats.get('last_heartbeat'),
            'restarts': stats.get('restarts', 0),
            'last_error': stats.get('last_error', ''),
            'last_error_ts': stats.get('last_error_ts'),
            'phone': account.get('phone') or db_phone or _PHONE,
            'username': account.get('username') or '',
            'first_name': account.get('first_name') or '',
            'user_id': account.get('user_id'),
            'login_pending': bool(db_phone),
        }

    # Worker offline → DO NOT open a Telethon client against the session
    # file here. During the login wizard verify_code/sign_in is writing the
    # very same SQLite session file — a concurrent open from get_status
    # (frontend polls every 30s) deadlocks it ("database is locked") and
    # surfaces as "kod tekshirilmadi". Report stats + pending login state.
    #
    # Agar Neon DB'da valid sessiya bo'lsa (login wizard yozgan) — foydalanuvchi
    # KIRGAN, worker esa offline (self-heal jarayonida). "KIRILMAGAN" deyish
    # noto'g'ri bo'lardi: authorized=True + worker_online=False ko'rsatamiz.
    db_authorized = False
    db_username = stats.get('account', {}).get('username') or ''
    try:
        from apps.settings_app.models import Setting
        _b64 = Setting.get_setting('user_client_session_b64', '') or ''
        if _b64:
            import base64 as _b64mod
            _data = _b64mod.b64decode(_b64)
            import sqlite3 as _sqlite3
            import tempfile as _tempfile
            _fd, _path = _tempfile.mkstemp(suffix='.session')
            try:
                import os as _os
                with _os.fdopen(_fd, 'wb') as _f:
                    _f.write(_data)
                _con = _sqlite3.connect(_path)
                try:
                    _row = _con.execute(
                        'SELECT auth_key FROM sessions LIMIT 1'
                    ).fetchone()
                finally:
                    _con.close()
                db_authorized = bool(_row and _row[0] and any(_row[0]))
            finally:
                try:
                    _os.remove(_path)
                except Exception:
                    pass
    except Exception:
        pass

    info = {
        'authorized': db_authorized,
        'credentials': True,
        'session_exists': session_exists,
        'worker_online': False,
        'last_heartbeat': stats.get('last_heartbeat'),
        'restarts': stats.get('restarts', 0),
        'last_error': stats.get('last_error', ''),
        'last_error_ts': stats.get('last_error_ts'),
        'phone': db_phone or _PHONE,
        'username': db_username,
        'first_name': stats.get('account', {}).get('first_name') or '',
        'user_id': stats.get('account', {}).get('user_id'),
        'login_pending': bool(db_phone),
        'session_source': 'neon' if db_authorized else 'none',
    }
    return info


# ── login flow ────────────────────────────────────────────────────────────

def start_phone(phone: str) -> dict:
    """Send the Telegram login code to the given phone. Returns ok / detail."""
    phone = (phone or '').strip()
    if not re.match(r'^\+?[0-9]{7,15}$', phone):
        return {'ok': False, 'detail': "Telefon raqam noto'g'ri formatda (masalan +998901234567)"}

    # Yangi login boshlanmoqda — eski temp sessiyani tozalaymiz (agar
    # avvalgi urinish chala qolgan bo'lsa).
    try:
        if os.path.exists(LOGIN_SESSION_FILE):
            os.remove(LOGIN_SESSION_FILE)
    except Exception:
        pass

    async def _start():
        client, api_id = await _make_client(LOGIN_SESSION_FILE)
        if client is None:
            return {'ok': False, 'detail': 'telegram_api_id / telegram_api_hash sozlanmagan (Kalitlar)'}
        await client.connect()
        try:
            if await client.is_user_authorized():
                return {'ok': False, 'already_authorized': True,
                        'detail': 'Bu session allaqachon kiritilgan. Avval "Chiqish" tugmasini bosing.'}
            sent = await client.send_code_request(phone)
            return {'ok': True, 'phone_code_hash': getattr(sent, 'phone_code_hash', '') or ''}
        finally:
            await client.disconnect()

    try:
        result = _run(_start())
    except Exception as exc:
        logger.warning('send_code_request failed: %s', type(exc).__name__)
        err = type(exc).__name__.lower()
        if 'phone' in err and ('invalid' in err or 'occupied' in err):
            return {'ok': False, 'detail': "Raqam noto'g'ri yoki bu akkaunt allaqachon band (PhoneNumberInvalid/Occupied)"}
        if 'flood' in err:
            return {'ok': False, 'detail': 'Telegram vaqtincha chekladi (flood). Birozdan so‘ng qayta urinib ko‘ring.'}
        return {'ok': False, 'detail': f"Kod yuborilmadi ({type(exc).__name__})"}

    if not result.get('ok'):
        return result

    _set_login_state(phone, result.get('phone_code_hash', ''), False)
    return {'ok': True, 'detail': 'Tasdiqlash kodi Telegram/SMS orqali yuborildi. Kodni kiriting.'}


def verify_code(code: str) -> dict:
    """Sign in with the code. Returns ok / needs_password / error."""
    code = (code or '').strip()

    phone, phone_code_hash, _needs_2fa = _get_login_state()

    if not phone:
        return {'ok': False, 'detail': 'Avval telefon raqamni kiriting va "Kod olish"ni bosing.'}
    if not code:
        return {'ok': False, 'detail': 'Kod kiritilmadi.'}

    async def _verify():
        client, _ = await _make_client(LOGIN_SESSION_FILE)
        if client is None:
            return {'ok': False, 'detail': 'Kalitlar sozlanmagan'}
        await client.connect()
        try:
            await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash or None)
            me = await client.get_me()
            return {'ok': True, 'username': getattr(me, 'username', '') or '',
                    'first_name': getattr(me, 'first_name', '') or '',
                    'user_id': getattr(me, 'id', None)}
        finally:
            await client.disconnect()

    try:
        result = _run(_verify())
    except Exception as exc:
        from telethon.errors import SessionPasswordNeededError
        if isinstance(exc, SessionPasswordNeededError):
            _set_login_state(phone, phone_code_hash, True)
            return {'ok': False, 'needs_password': True,
                    'detail': 'Akkauntda ikki bosqichli himoya (2FA) yoqilgan — parolni kiriting.'}
        err = type(exc).__name__.lower()
        if 'invalid' in err or 'code' in err and 'expired' in err:
            return {'ok': False, 'detail': "Kod noto'g'ri yoki muddati o'tgan. Qayta urinib ko'ring yoki qayta kod oling."}
        if 'flood' in err:
            return {'ok': False, 'detail': 'Telegram vaqtincha chekladi. Birozdan so‘ng qayta urinib ko‘ring.'}
        logger.warning('sign_in failed: %s', type(exc).__name__)
        return {'ok': False, 'detail': f"Kirish amalga oshmadi ({type(exc).__name__})"}

    if result.get('ok'):
        _clear_login_state()
        _promote_login_session()
        _restart_worker()
    return result


def verify_password(password: str) -> dict:
    """Second step for 2FA-enabled accounts."""
    password = password or ''

    phone, phone_code_hash, needs_2fa = _get_login_state()
    if not needs_2fa:
        return {'ok': False, 'detail': 'Parol so‘ralmagan. Avval kod bilan kirishni boshlang.'}

    async def _do():
        client, _ = await _make_client(LOGIN_SESSION_FILE)
        if client is None:
            return {'ok': False, 'detail': 'Kalitlar sozlanmagan'}
        await client.connect()
        try:
            await client.sign_in(password=password)
            me = await client.get_me()
            return {'ok': True, 'username': getattr(me, 'username', '') or '',
                    'first_name': getattr(me, 'first_name', '') or '',
                    'user_id': getattr(me, 'id', None)}
        finally:
            await client.disconnect()

    try:
        result = _run(_do())
    except Exception as exc:
        err = type(exc).__name__.lower()
        if 'password' in err and 'invalid' in err:
            return {'ok': False, 'detail': 'Parol noto‘g‘ri. Qayta urinib ko‘ring.'}
        logger.warning('2FA sign_in failed: %s', type(exc).__name__)
        return {'ok': False, 'detail': f"Parol qabul qilinmadi ({type(exc).__name__})"}

    if result.get('ok'):
        _clear_login_state()
        _promote_login_session()
        _restart_worker()
    return result


def resolve_monitor_chat() -> dict:
    """Try to resolve the configured monitor chat with the logged-in session.

    Returns {ok, raw, resolved_id, name} on success or {ok: False, detail}
    with the Telegram error — lets the admin debug "Monitor chat topilmadi"
    straight from the panel.
    """
    from apps.cardpay import services as cardpay_services

    async def _check():
        from asgiref.sync import sync_to_async
        client, _ = await _make_client()
        if client is None:
            return {'ok': False, 'detail': 'telegram_api_id / telegram_api_hash sozlanmagan (Kalitlar)'}
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return {'ok': False, 'detail': 'Akkaunt kiritilmagan — avval kirishni bajaring'}
            s = await sync_to_async(cardpay_services.get_settings, thread_sensitive=False)()
            raw = s.get('monitor_chat_id') or ''
            if not raw:
                return {'ok': False, 'detail': "Monitor chat sozlanmagan (Sozlamalar bo'limida kiriting)"}
            try:
                ent = await client.get_entity(raw)
            except Exception as exc:
                return {
                    'ok': False,
                    'raw': raw,
                    'detail': f'Topilmadi: {type(exc).__name__} — chat username/ID noto\'g\'ri yoki akkaunt a\'zo emas',
                }
            return {
                'ok': True,
                'raw': raw,
                'resolved_id': getattr(ent, 'id', None),
                'name': (getattr(ent, 'title', None) or getattr(ent, 'username', None)
                         or getattr(ent, 'first_name', None) or str(raw)),
            }
        finally:
            await client.disconnect()

    try:
        return _run(_check())
    except Exception as exc:
        logger.warning('monitor chat check failed: %s', type(exc).__name__)
        return {'ok': False, 'detail': f'Tekshirish amalga oshmadi ({type(exc).__name__})'}


def read_supervisor_log(n: int = 40) -> list:
    """Last N lines of the user-client supervisor log (no secrets)."""
    try:
        from pathlib import Path
        log_path = Path(BASE_DIR).parent / '.freebuff' / 'user-client-supervisor.log'
        if not log_path.exists():
            return []
        lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
        return lines[-n:]
    except Exception:
        return []


def logout() -> dict:
    """Delete the session file + stop the running worker (supervisor restarts)."""
    removed = False
    try:
        for base in (SESSION_FILE, LOGIN_SESSION_FILE):
            for suffix in ('', '.session'):
                p = base + ('' if suffix == '' else suffix)
                if os.path.exists(p):
                    os.remove(p)
                    removed = True
    except Exception as exc:
        logger.warning('session delete failed: %s', exc)
    _clear_login_state()
    _kill_worker_crossplatform()
    return {'ok': True, 'removed': removed,
            'detail': 'Session o‘chirildi. Endi boshqa akkaunt bilan kirishingiz mumkin.'}


# ── worker (supervisor) integration ───────────────────────────────────────

def _kill_worker() -> None:
    """Cross-platform kill of user_client.py worker processes."""
    _kill_worker_crossplatform()


def _restart_worker() -> None:
    """After a successful login, restart the worker so it picks up the new
    session immediately (the supervisor auto-restarts it within ~5s)."""
    _kill_worker_crossplatform()
    # Cloud (Linux): cloud_launcher rc=5 holatida 300s kutar edi. Flag fayl
    # yaratamiz — launcher buni ko'rib darhol qayta ishga tushiradi.
    try:
        flag = os.path.join(BASE_DIR, 'sessions', '.restart_requested')
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, 'w') as f:
            f.write(str(time.time()))
        logger.info('worker restart flag yaratildi (cloud launcher uchun)')
    except Exception as exc:
        logger.warning('restart flag yozilmadi: %s', exc)
