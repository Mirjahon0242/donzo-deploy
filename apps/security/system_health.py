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

BACKEND_HEALTH_URL = 'http://localhost:8000/health/'
TUNNEL_LOG = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.freebuff', 'tunnel.log')
BOT_STATS = os.path.join(os.path.dirname(__file__), '..', '..', '..', '.freebuff', 'bot-stats.json')
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
    return {'name': 'Backend', 'port': 8000, 'status': 'ok' if ok else 'down',
            'detail': f'HTTP {code}' if code else 'aloqa yo\'q'}


def check_tunnel() -> dict:
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
    ok = _port_open(USER_CLIENT_PORT)
    return {'name': 'User Client', 'port': USER_CLIENT_PORT,
            'status': 'ok' if ok else 'down',
            'detail': f'port {USER_CLIENT_PORT}' + (' ochiq' if ok else ' yopiq')}


def check_watchdog() -> dict:
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
        return {'name': 'Ma\'lumotlar bazasi', 'port': '-', 'status': 'ok', 'detail': 'SQLite OK'}
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
