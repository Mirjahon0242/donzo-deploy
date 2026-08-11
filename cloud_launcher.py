#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DONZO cloud launcher — Render (yoki boshqa container) uchun.

Bitta konteynerda hamma narsani ishga tushiradi va nazorat qiladi:
  1. daphne        — Django API + WebSocket (web, $PORT da)
  2. bot.py        — Telegram bot (polling)
  3. user_client.py — karta monitori (Telethon)

Qo'shimcha:
  • Render free web service uxlab qolmasligi uchun har 5 daqiqada
    RENDER_EXTERNAL_URL/health/ ga ping yuboradi.
  • SESSION_B64 env'idan user_client sessiyasini tiklaydi (agar mavjud).
  • Kunlik audit hisobotini AUDIT_REPORT_HOUR (UTC, default 9) da yuboradi.
  • Har bir jarayon yiqilsa backoff bilan avtomatik qayta ishga tushadi.

Ishlatish:  python cloud_launcher.py
"""
import base64
import datetime as dt
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = os.getenv('PORT', '8000')
PING_URL = (os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
PING_INTERVAL = int(os.getenv('PING_INTERVAL', '300'))
AUDIT_HOUR = int(os.getenv('AUDIT_REPORT_HOUR', '9'))

_stop = threading.Event()


def _log(tag: str, msg: str):
    ts = dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] [{tag}] {msg}', flush=True)


def _session_bootstrap():
    """Sessiyani tiklaydi: SESSION_B64 env yoki Neon DB'dagi
    'user_client_session_b64' sozlamasidan (cloud deploy uchun)."""
    b64 = os.getenv('SESSION_B64', '')
    if not b64:
        try:
            import django
            os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
            django.setup()
            from apps.settings_app.models import Setting
            b64 = Setting.get_setting('user_client_session_b64', '') or ''
            _log('SESSION', "sessiya Neon DB'dan o'qildi")
        except Exception as exc:
            _log('SESSION', f"DB sessiya o'qilmadi: {type(exc).__name__}: {str(exc)[:120]}")
            return
    if not b64:
        _log('SESSION', "sessiya topilmadi (env ham, DB ham bo'sh) — user_client ishlamaydi")
        return
    try:
        data = base64.b64decode(b64)
    except Exception as exc:
        _log('SESSION', f"SESSION_B64 dekodlash xatosi: {exc}")
        return
    sess_dir = os.path.join(BASE_DIR, 'sessions')
    sess_file = os.path.join(sess_dir, 'donzo_user.session')
    os.makedirs(sess_dir, exist_ok=True)
    tmp = sess_file + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, sess_file)
    _log('SESSION', f"Sessiya tiklandi ({len(data)} bayt) → sessions/donzo_user.session")


def _relay(name: str, proc: subprocess.Popen):
    """Child stdout/stderr ni prefiks bilan terminalga uzatadi."""
    def _pump(stream):
        try:
            for line in iter(stream.readline, b''):
                text = line.decode('utf-8', errors='replace').rstrip('\n')
                if text:
                    _log(name, text)
        except Exception:
            pass
    threading.Thread(target=_pump, args=(proc.stdout,), daemon=True).start()
    threading.Thread(target=_pump, args=(proc.stderr,), daemon=True).start()


def _spawn(cmd, name, cwd=BASE_DIR):
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _relay(name, proc)
        return proc
    except Exception as exc:
        _log(name, f"ishga tushirilmadi: {exc}")
        return None


def _supervise(name, cmd):
    """Jarayonni backoff bilan abadiy nazorat qiladi."""
    backoff = 5
    while not _stop.is_set():
        proc = _spawn(cmd, name)
        if proc is None:
            _stop.wait(backoff)
            backoff = min(backoff * 2, 60)
            continue
        _t0 = time.time()
        _log(name, f"started (pid={proc.pid})")
        rc = proc.wait()
        if _stop.is_set():
            _log(name, f"stopped (rc={rc}) — launcher yakunlanmoqda")
            return
        lived = time.time() - _t0
        if rc == 0:
            _log(name, f"chiqdi (rc=0) — {backoff}s keyin qayta ishga tushadi")
        else:
            _log(name, f"YIQILDI (rc={rc}) — {backoff}s keyin qayta ishga tushadi")
        _stop.wait(backoff)
        backoff = 5 if lived > 300 else min(backoff * 2, 60)


def _pinger():
    """Free web service'ni uyquga ketishdan saqlaydi (5 daqiqada ping)."""
    if not PING_URL:
        _log('PING', "RENDER_EXTERNAL_URL yo'q — ping o'chirilgan (lokal rejim)")
        return
    url = PING_URL + '/health/'
    while not _stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                _log('PING', f"{r.status} ← {url}")
        except Exception as exc:
            _log('PING', f"xato: {type(exc).__name__}: {str(exc)[:120]}")
        _stop.wait(PING_INTERVAL)


def _daily_audit():
    """Kunlik audit hisobotini AUDIT_HOUR (UTC) da yuboradi."""
    while not _stop.is_set():
        now = dt.datetime.utcnow()
        target = now.replace(hour=AUDIT_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        secs = (target - now).total_seconds()
        _log('AUDIT', f"keyingi hisobot: {target.isoformat()}Z ({int(secs)}s dan keyin)")
        if _stop.wait(secs):
            return
        try:
            _log('AUDIT', "hisobot yuborilmoqda...")
            subprocess.run(
                [sys.executable, 'daily_audit_report.py', '--force'],
                cwd=BASE_DIR, timeout=120,
            )
            _log('AUDIT', 'hisobot yuborildi')
        except Exception as exc:
            _log('AUDIT', f"hisobot xatosi: {type(exc).__name__}: {str(exc)[:150]}")


def main():
    _log('MAIN', f"DONZO cloud launcher — port {PORT}")
    _session_bootstrap()

    procs = [
        ('DAPHNE', [sys.executable, '-m', 'daphne', '-b', '0.0.0.0',
                    '-p', PORT, 'config.asgi:application']),
        ('BOT', [sys.executable, 'bot.py']),
        ('USERCLIENT', [sys.executable, 'user_client.py']),
    ]
    threads = [threading.Thread(target=_supervise, args=(n, c), daemon=True)
               for n, c in procs]
    threads.append(threading.Thread(target=_pinger, daemon=True))
    threads.append(threading.Thread(target=_daily_audit, daemon=True))
    for t in threads:
        t.start()

    def _shutdown(signum, frame):
        _log('MAIN', f"signal {signum} — yakunlanmoqda")
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not _stop.is_set():
            _stop.wait(1)
    except KeyboardInterrupt:
        _stop.set()
    _log('MAIN', "barcha jarayonlar to'xtatilmoqda")
    time.sleep(2)
    _log('MAIN', "chiqish")


if __name__ == '__main__':
    main()
