# -*- coding: utf-8 -*-
"""
System Health — DONZO tizim holatini yig'ish (staff bot komandalari uchun).

Tekshiradi:
  • Backend (http://localhost:8000/health/)
  • Tunnel (cloudflared — joriy URL + /health/ orqali)
  • Bot (bot-stats.json heartbeat + token status)
  • User Client (port 18713)
  • Watchdog (lock port)
  • SQLite DB (so'rov orqali)

Hech qachon exception tashlamaydi — har bir komponent alohida try/except
ichida, xato bo'lsa "down" deb belgilanadi. Bu modul faqat STAFF komandalari
uchun (bot /status, /xato, /togrila) va admin panel uchun ishlatiladi.
"""
import json
import logging
import os
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# CLOUD MODE: Render'da RENDER_EXTERNAL_URL/PORT env bor — backend URL
# dinamik. Lokalda esa localhost:8000 (daphne) ishlatiladi.
IS_CLOUD = bool(os.getenv('RENDER') or os.getenv('RENDER_EXTERNAL_URL'))
CLOUD_URL = (os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
LOCAL_BACKEND_URL = f"http://localhost:{os.getenv('PORT', '8000')}"
BACKEND_BASE = CLOUD_URL or LOCAL_BACKEND_URL
BACKEND_HEALTH_URL = BACKEND_BASE + '/health/'
TUNNEL_LOG = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.freebuff', 'tunnel.log')
BOT_STATS = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.freebuff', 'bot-stats.json')
USER_CLIENT_STATS = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.freebuff', 'user-client-stats.json')
USER_CLIENT_PORT = 18713
WATCHDOG_PORT = 18717


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect'larni follow qilmaydi — 301/302 ni xuddi o'zi qaytaradi
    (SECURE_SSL_REDIRECT tufayli http→https 301 keladi — bu backend jonli
    ekanini bildiradi, https'ga follow qilish lokal sertifikatsiz yiqiladi)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_status(url: str, timeout: float = 4.0):
    """GET so'rov — (code, ok) qaytaradi. 3xx redirect ham OK (HTTPS majburiy)."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        req = urllib.request.Request(url, method='GET')
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, True
    except urllib.error.HTTPError as e:
        # 3xx = redirect (SECURE_SSL_REDIRECT) — backend jonli
        return e.code, e.code < 400
    except Exception:
        return None, False


def _port_open(port: int, host: str = '127.0.0.1', timeout: float = 1.5) -> bool:
    """Port LISTENING holatda ekanini tekshiradi.

    Eslatma: supervisor'lar lock-socket'ini hech qachon ACCEPT qilmaydi —
    shuning uchun create_connection hang bo'lishi mumkin. Ishonchli usul:
    netstat'da 'LISTENING' holatini qidirish (Windows).
    """
    try:
        out = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=timeout + 2,
        ).stdout
        needle = f':{port} '
        for line in out.splitlines():
            if needle in line and 'LISTENING' in line and f'{host}:' in line:
                return True
        return False
    except Exception:
        # Fallback: to'g'ridan-to'g'ri connect (macOS/Linux)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            return False


def _read_json(path: str):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def get_tunnel_url() -> str:
    """Joriy tunnel URL (trycloudflare) — .freebuff fayllardan topadi.

    Ustuvorlik:
      1) current_tunnel_url.txt / tunnel-urls.txt — watchdog yozgan joriy URL
      2) web_app_url Setting (agar trycloudflare bo'lsa)
      3) Log'lar (eng OXIRGI topilgan — eng yangi)
    """
    import re as _re
    from apps.settings_app.models import Setting
    freebuff = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.freebuff')

    # 1) Joriy URL fayllari (watchdog yozadi)
    for name in ('current_tunnel_url.txt', 'tunnel-urls.txt'):
        path = os.path.join(freebuff, name)
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        m = _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                        if m:
                            return m.group(0)
        except Exception:
            continue

    # 2) Setting
    web_app_url = Setting.get_setting('web_app_url', '') or ''
    if web_app_url and 'trycloudflare.com' in str(web_app_url):
        return str(web_app_url).rstrip('/')

    # 3) Log'lar (oxirgi = eng yangi)
    found = ''
    for log_name in ('watchdog.log', 'cloudflared-backend.log', 'tunnel.log'):
        path = os.path.join(freebuff, log_name)
        try:
            if not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    m = _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                    if m:
                        found = m.group(0)
        except Exception:
            continue
    return found or web_app_url or ''


def check_backend() -> dict:
    code, ok = _http_status(BACKEND_HEALTH_URL)
    return {'name': 'Backend', 'port': os.getenv('PORT', '8000'),
            'status': 'ok' if ok else 'down',
            'detail': f'HTTP {code}' if code else 'aloqa yo\'q'}


def check_tunnel() -> dict:
    # CLOUD: tunnel yo'q — Render'ning doimiy URL'i tekshiriladi.
    if IS_CLOUD and CLOUD_URL:
        code, ok = _http_status(f'{CLOUD_URL}/health/', timeout=5.0)
        return {'name': 'Public URL', 'port': '-', 'status': 'ok' if ok else 'down',
                'detail': CLOUD_URL if ok else f'HTTP {code}'}
    url = get_tunnel_url()
    if not url:
        return {'name': 'Tunnel', 'port': '-', 'status': 'down', 'detail': 'URL topilmadi'}
    code, ok = _http_status(f'{url}/health/', timeout=5.0)
    return {'name': 'Tunnel', 'port': '-', 'status': 'ok' if ok else 'down',
            'detail': url if ok else f'HTTP {code}'}


def _ts_to_epoch(ts) -> float:
    """ISO timestamp yoki epoch sonni epoch'ga aylantiradi (float)."""
    if not ts:
        return 0.0
    try:
        return float(ts)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp()
    except Exception:
        return 0.0


def check_bot() -> dict:
    stats = _read_json(BOT_STATS)
    if not stats:
        return {'name': 'Bot', 'port': '-', 'status': 'down', 'detail': 'statistika yo\'q'}
    ts = stats.get('last_heartbeat') or stats.get('started_at') or 0
    age_s = time.time() - _ts_to_epoch(ts)
    ok = age_s < 180  # 3 daqiqa ichida heartbeat bo'lsa jonli
    token_status = stats.get('token_status')
    token_txt = 'token OK' if token_status else 'token xato!'
    return {'name': 'Bot', 'port': '-', 'status': 'ok' if ok else 'down',
            'detail': f'heartbeat {int(age_s)}s avval · {token_txt}'}


def check_user_client() -> dict:
    # CLOUD: lock-port yo'q — user-client-stats.json heartbeat'ga qaraymiz
    # (user_client_stats.mark_started/heartbeat har 30s yozadi).
    stats = _read_json(USER_CLIENT_STATS)
    if stats:
        ts = stats.get('last_heartbeat') or stats.get('started_at') or 0
        age_s = time.time() - _ts_to_epoch(ts)
        ok = age_s < 180
        detail = f'heartbeat {int(age_s)}s avval' if ok else 'heartbeat eskirgan'
        return {'name': 'User Client', 'port': '-', 'status': 'ok' if ok else 'down',
                'detail': detail}
    # CLOUD: stats fayli bo'lmasa (fresh container) — Neon DB'dagi sessiya
    # va login holatiga qaraymiz. Sessiya bor + login kutilmayotgan bo'lsa
    # worker qayta boshlanishi mumkin; sessiya yo'q yoki login_pending
    # bo'lsa — qayta kirish kerakligini aniq ko'rsatamiz.
    if IS_CLOUD:
        try:
            from apps.settings_app.models import Setting
            b64 = Setting.get_setting('user_client_session_b64', '') or ''
            pending = bool(Setting.get_setting('user_client_login_phone', '') or '')
            detail = 'sessiya Neon DB\'da'
            if not b64:
                return {'name': 'User Client', 'port': '-', 'status': 'down',
                        'detail': 'sessiya yo\'q — User Client panelida qayta kirish kerak'}
            if pending:
                return {'name': 'User Client', 'port': '-', 'status': 'down',
                        'detail': 'login jarayonda — kod kiritilishini kutyapti'}
            # Sessiya bor, lekin worker heartbeat'i yo'q — bloklangan yoki
            # worker hali ishga tushmagan bo'lishi mumkin.
            return {'name': 'User Client', 'port': '-', 'status': 'down',
                    'detail': 'sessiya bor, worker heartbeat yo\'q (bloklangan bo\'lishi mumkin)'}
        except Exception:
            pass
    ok = _port_open(USER_CLIENT_PORT)
    return {'name': 'User Client', 'port': USER_CLIENT_PORT,
            'status': 'ok' if ok else 'down',
            'detail': f'port {USER_CLIENT_PORT}' + (' ochiq' if ok else ' yopiq')}


def check_watchdog() -> dict:
    # CLOUD: cloud_launcher o'zi watchdog vazifasini bajaradi — launcher
    # jonli bo'lsa (bu kod o'sha konteynerda ishlayapti) watchdog OK.
    if IS_CLOUD:
        return {'name': 'Watchdog', 'port': '-', 'status': 'ok', 'detail': 'cloud_launcher (Render)'}
    ok = _port_open(WATCHDOG_PORT)
    return {'name': 'Watchdog', 'port': WATCHDOG_PORT,
            'status': 'ok' if ok else 'down',
            'detail': f'port {WATCHDOG_PORT}' + (' ochiq' if ok else ' yopiq')}


def check_database() -> dict:
    try:
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('SELECT 1')
            cur.fetchone()
        engine = connection.vendor  # 'sqlite' / 'postgresql'
        return {'name': 'Ma\'lumotlar bazasi', 'port': '-', 'status': 'ok',
                'detail': f'{engine} OK'}
    except Exception as exc:
        return {'name': 'Ma\'lumotlar bazasi', 'port': '-', 'status': 'down',
                'detail': str(exc)[:80]}


def collect_health() -> list:
    """Barcha komponentlar holatini yig'adi — (name, status, detail) ro'yxati."""
    return [
        check_backend(),
        check_tunnel(),
        check_bot(),
        check_user_client(),
        check_watchdog(),
        check_database(),
    ]


def health_summary() -> dict:
    """Kompakt xulosa — avto-tuzatish uchun (qaysi komponent down)."""
    parts = collect_health()
    down = [p for p in parts if p['status'] != 'ok']
    return {
        'ok': len(down) == 0,
        'down': down,
        'components': parts,
        'checked_at': timezone.now().isoformat(),
    }


def format_health_report() -> str:
    """Staff uchun HTML formatdagi holat hisoboti."""
    parts = collect_health()
    lines = ["🖥️ <b>DONZO tizim holati</b>\n"]
    for p in parts:
        icon = '🟢' if p['status'] == 'ok' else '🔴'
        lines.append(f"{icon} <b>{p['name']}</b> — {p['detail']}")
    down = [p for p in parts if p['status'] != 'ok']
    if down:
        lines.append(f"\n⚠️ <b>{len(down)} ta komponent ishlamayapti.</b>")
        lines.append("Tuzatish uchun: /togrila")
    else:
        lines.append("\n✅ <b>Hammasi ishlayapti.</b>")
    return '\n'.join(lines)


def recent_errors(limit: int = 5) -> list:
    """Oxirgi AuditLog xatolarini qaytaradi (staff /xato uchun)."""
    try:
        from apps.audit_log.models import AuditLog
        qs = AuditLog.objects.exclude(
            description__isnull=True,
        ).exclude(description='').order_by('-created_at')
        errors = []
        for log in qs[:limit * 3]:  # xato bo'lmaganlarni filtrlaymiz
            desc = log.description or ''
            if any(k in desc.lower() for k in ('xato', 'error', 'failed', 'muvaffaqiyatsiz', 'exception')):
                errors.append({
                    'time': log.created_at,
                    'action': log.action,
                    'description': desc[:200],
                })
            if len(errors) >= limit:
                break
        return errors
    except Exception:
        logger.exception('AuditLog xatolarini olishda xato')
        return []
