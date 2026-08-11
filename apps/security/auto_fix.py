# -*- coding: utf-8 -*-
"""
Auto-Fix (DONZO /togrila).

Staff guruhda /togrila komandasi yozilsa — bot tizim holatini tekshiradi va
ishlamayotgan komponentlarni avtomatik qayta ishga tushiradi:

  1. Backend (8000)  — daphne o'lik bo'lsa watchdog ishga tushadi / qayta ishga tushadi
  2. Tunnel          — cloudflared o'lik bo'lsa watchdog qayta ochadi
  3. Bot             — bot o'zi (heartbeat eskirgan bo'lsa supervisor qayta ishga tushiradi)
  4. User Client     — telethon worker port yopiq bo'lsa
  5. Watchdog        — o'zi o'lik bo'lsa

XAVFSIZLIK:
  • Bu modul faqat STAFF (admin/super_admin) tomonidan chaqiriladi — bot.py
    komanda handler'ida rol tekshiruvi bor.
  • Faqat lokaldagi jarayonlarni boshqaradi — hech qanday tashqi ta'sir yo'q.
  • Hech qachon exception tashlamaydi — natija hisobot qilinadi.
"""
import logging
import os
import subprocess
import time

from django.utils import timezone

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCHDOG_SCRIPT = os.path.join(BASE_DIR, 'donzo_watchdog.ps1')


def _run_powershell(script_path: str, args: list = None, timeout: int = 90):
    """PowerShell scriptni ishga tushiradi — natija (rc, output)."""
    cmd = ['powershell', '-ExecutionPolicy', 'Bypass', '-File', script_path] + (args or [])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=BASE_DIR,
        )
        return proc.returncode, (proc.stdout or '') + (proc.stderr or '')
    except subprocess.TimeoutExpired:
        return -1, 'timeout'
    except Exception as exc:
        return -1, str(exc)


def _taskkill_pids(port: int):
    """Port egasi jarayonlarni o'ldiradi (Windows netstat + taskkill)."""
    try:
        out = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=10,
        ).stdout
        pids = set()
        for line in out.splitlines():
            if f':{port} ' in line and 'LISTENING' in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            try:
                subprocess.run(['taskkill', '/F', '/PID', pid],
                               capture_output=True, timeout=10)
            except Exception:
                pass
        return len(pids)
    except Exception:
        return 0


def _is_port_open(port: int) -> bool:
    """Port LISTENING holatda ekanini tekshiradi (netstat)."""
    try:
        out = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if f':{port} ' in line and 'LISTENING' in line and '127.0.0.1:' in line:
                return True
        return False
    except Exception:
        return False


def run_auto_fix(actor_username: str = 'staff') -> dict:
    """
    Avto-tuzatish: holatni tekshiradi, down komponentlarni tiklaydi.

    Returns:
      {'ok': bool, 'actions': [ {component, action, result} ], 'summary': str}
    """
    from . import system_health

    actions = []
    try:
        health = system_health.health_summary()
    except Exception:
        health = {'ok': False, 'down': [{'name': 'Health check', 'detail': 'xato'}]}

    down_names = [c['name'] for c in health.get('down', [])]

    # ── 1) Backend down → portni tozalab watchdog'ni chaqiramiz ──
    if 'Backend' in down_names or 'Tunnel' in down_names:
        if not _is_port_open(8000):
            _taskkill_pids(8000)
            actions.append({'component': 'Backend', 'action': 'port 8000 tozalandi'})
        # Watchdog hammasini ko'taradi (backend + tunnel + bot + user client)
        rc, out = _run_powershell(WATCHDOG_SCRIPT)
        actions.append({
            'component': 'Watchdog',
            'action': 'qayta ishga tushirildi',
            'result': 'OK' if rc == 0 else f'rc={rc}: {out[-150:]}',
        })
        # Ko'tarilishini kutamiz
        time.sleep(12)

    # ── 2) Faqat Tunnel down → watchdog tunnelni qayta ochadi ──
    elif 'Tunnel' in down_names:
        rc, out = _run_powershell(WATCHDOG_SCRIPT)
        actions.append({
            'component': 'Tunnel',
            'action': 'watchdog orqali qayta ochildi',
            'result': 'OK' if rc == 0 else f'rc={rc}: {out[-150:]}',
        })
        time.sleep(10)

    # ── 3) User Client down → portni tozalab, supervisor chaqiramiz ──
    if 'User Client' in down_names:
        _taskkill_pids(18713)
        uc_script = os.path.join(BASE_DIR, 'user_client_supervisor.py')
        if os.path.exists(uc_script):
            rc, out = _run_powershell(
                os.path.join(BASE_DIR, 'start_user_client.ps1')
                if os.path.exists(os.path.join(BASE_DIR, 'start_user_client.ps1'))
                else uc_script,
                timeout=60,
            )
            actions.append({
                'component': 'User Client',
                'action': 'qayta ishga tushirildi',
                'result': 'OK' if rc == 0 else f'rc={rc}',
            })
        else:
            actions.append({'component': 'User Client', 'action': 'supervisor topilmadi',
                            'result': 'SKIP'})

    # ── 4) Bot down (heartbeat eskirgan) → supervisor restart ──
    bot_info = next((c for c in health.get('components', []) if c['name'] == 'Bot'), None)
    if bot_info and bot_info['status'] != 'ok':
        bot_sup = os.path.join(BASE_DIR, 'bot_supervisor.py')
        if os.path.exists(bot_sup):
            rc, out = _run_powershell(os.path.join(BASE_DIR, 'restart_bot.ps1')
                                      if os.path.exists(os.path.join(BASE_DIR, 'restart_bot.ps1'))
                                      else bot_sup, timeout=60)
            actions.append({
                'component': 'Bot',
                'action': 'qayta ishga tushirildi',
                'result': 'OK' if rc == 0 else f'rc={rc}',
            })
        else:
            actions.append({'component': 'Bot', 'action': 'supervisor topilmadi', 'result': 'SKIP'})

    # ── Natijani qayta tekshiramiz ──
    time.sleep(5)
    try:
        after = system_health.health_summary()
        still_down = [c['name'] for c in after.get('down', [])]
    except Exception:
        still_down = ['(tekshirib bo\'lmadi)']

    ok = len(still_down) == 0
    summary = ('✅ Barcha komponentlar tiklandi!'
               if ok else f"⚠️ Hali ishlamayapti: {', '.join(still_down)}")

    # Audit trail
    try:
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            action='auto_fix',
            target_type='system',
            description=f"Auto-fix ({actor_username}): {'; '.join(a['action'] for a in actions)} → {summary}",
        )
    except Exception:
        pass

    return {'ok': ok, 'actions': actions, 'summary': summary}


def format_fix_report(result: dict) -> str:
    """Auto-fix natijasini staff uchun HTML formatda qaytaradi."""
    lines = ["🔧 <b>Avto-tuzatish natijasi</b>\n"]
    if not result.get('actions'):
        lines.append("Hech qanday harakat talab qilinmadi — hammasi ishlayapti.")
    for a in result.get('actions', []):
        icon = '✅' if a.get('result') == 'OK' else ('⚠️' if a.get('result') == 'SKIP' else '❌')
        lines.append(f"{icon} <b>{a['component']}</b> — {a['action']}"
                     + (f" ({a['result']})" if a.get('result') and a['result'] != 'OK' else ''))
    lines.append(f"\n📋 {result.get('summary', '')}")
    return '\n'.join(lines)
