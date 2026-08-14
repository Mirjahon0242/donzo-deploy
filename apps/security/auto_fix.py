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
import json
import logging
import os
import shutil
import subprocess
import time

from django.utils import timezone

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCHDOG_SCRIPT = os.path.join(BASE_DIR, 'donzo_watchdog.ps1')

# ── AI kod tuzatish: backup + revert ────────────────────────────────────
# Har /togrila kod tuzatishidan oldin asl fayl nusxasi shu papkaga saqlanadi.
# /qaytar buyrug'i oxirgi backup'dan fayllarni tiklaydi.
AI_FIX_BACKUP_DIR = os.path.join(BASE_DIR, 'backups', 'ai_fix')

# Faqat shu ildizlar ichidagi fayllarga yozishga ruxsat (xavfsizlik: /togrila
# hech qachon tashqariga chiqmaydi).
ALLOWED_ROOTS = (
    os.path.join(BASE_DIR, 'apps'),
    os.path.join(BASE_DIR, 'config'),
    os.path.join(BASE_DIR, 'manage.py'),
    os.path.join(BASE_DIR, 'bot.py'),
    os.path.join(BASE_DIR, 'user_client.py'),
    os.path.join(BASE_DIR, 'cloud_launcher.py'),
    os.path.join(BASE_DIR, 'user_client_auth.py'),
    os.path.join(BASE_DIR, 'bot_supervisor.py'),
    os.path.join(BASE_DIR, 'daily_audit_report.py'),
)


def _resolve_abs(rel_path: str):
    """rel_path → abs. Ruxsat etilgan ildizlardan tashqariga chiqishni bloklaydi."""
    if not rel_path or '..' in rel_path.replace('\\', '/'):
        return None
    abs_path = os.path.normpath(os.path.join(BASE_DIR, rel_path))
    for root in ALLOWED_ROOTS:
        if abs_path == root or abs_path.startswith(root + os.sep) or os.path.isfile(root) and abs_path == root:
            return abs_path
    return None


def _backup_file(abs_path: str, ts: str) -> str:
    """Faylni backup papkasiga nusxalaydi. Returns backup fayl yo'li."""
    try:
        rel = os.path.relpath(abs_path, BASE_DIR)
        dst_dir = os.path.join(AI_FIX_BACKUP_DIR, ts)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, rel.replace(os.sep, '__').replace('/', '__'))
        shutil.copy2(abs_path, dst)
        return dst
    except Exception as exc:
        logger.warning('backup xato: %s', exc)
        return ''


def _latest_backup_dir() -> str:
    """Eng so'nggi backup papkasini qaytaradi (timestamp bo'yicha)."""
    try:
        if not os.path.isdir(AI_FIX_BACKUP_DIR):
            return ''
        dirs = [d for d in os.listdir(AI_FIX_BACKUP_DIR)
                if os.path.isdir(os.path.join(AI_FIX_BACKUP_DIR, d))]
        if not dirs:
            return ''
        dirs.sort()  # ISO timestamp lexicographic sort ishlaydi
        return os.path.join(AI_FIX_BACKUP_DIR, dirs[-1])
    except Exception:
        return ''


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


# ── AI KOD TUZATISH (backup + revert bilan) ───────────────────────────────

def _collect_backup_file_names(ts_dir: str) -> list:
    """Backup papkasidagi fayllar ro'yxati (asl rel path lar)."""
    out = []
    try:
        for name in os.listdir(ts_dir):
            rel = name.replace('__', os.sep)
            out.append(rel)
    except Exception:
        pass
    return out


def apply_ai_patch(patch: dict, actor_username: str = 'staff') -> dict:
    """AI taklif qilgan patch'ni backup bilan qo'llaydi.

    patch: {'file': 'apps/security/staff_ai.py', 'old': '...', 'new': '...'}
    (yoki 'replacements': [ {file, old, new}, ... ] ko'p fayl uchun)

    Har bir fayl o'zgartirilishidan OLDIN asl nusxasi backups/ai_fix/<ts>/ ga
    saqlanadi — /qaytar buyrug'i shu nusxadan faylni tiklaydi.

    Returns: {'ok': bool, 'applied': [str], 'backup_dir': str, 'error': str}
    """
    patches = patch.get('replacements') or [patch]
    if not patches or not isinstance(patches, list):
        return {'ok': False, 'error': 'patch bo\'sh'}

    ts = timezone.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(AI_FIX_BACKUP_DIR, ts)
    applied = []
    errors = []

    for p in patches:
        rel = (p.get('file') or '').strip()
        old = p.get('old')
        new = p.get('new', '')
        if not rel or not old or old == new:
            continue
        abs_path = _resolve_abs(rel)
        if abs_path is None:
            errors.append(f"{rel}: ruxsat etilmagan fayl")
            continue
        if not os.path.isfile(abs_path):
            errors.append(f"{rel}: fayl topilmadi")
            continue
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()
            if old not in content:
                errors.append(f"{rel}: eski matn topilmadi (o'zgarish qo'llanmadi)")
                continue
            # Backup — faqat birinchi marta (bir ts ichida qayta yozilmasin)
            dst = _backup_file(abs_path, ts)
            new_content = content.replace(old, new, 1)
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            applied.append(rel)
        except Exception as exc:
            errors.append(f"{rel}: {type(exc).__name__}: {str(exc)[:120]}")

    # Audit trail
    try:
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            action='ai_code_fix',
            target_type='code',
            description=f"AI kod tuzatish ({actor_username}): {', '.join(applied) or '—'} → backup: {ts}",
        )
    except Exception:
        pass

    return {
        'ok': not errors and bool(applied),
        'applied': applied,
        'errors': errors,
        'backup_dir': ts,
    }


def revert_last_fix(actor_username: str = 'staff') -> dict:
    """Oxirgi AI kod tuzatishini asl holatiga qaytaradi (backup'dan tiklaydi)."""
    latest = _latest_backup_dir()
    if not latest:
        return {'ok': False, 'error': 'Qaytariladigan backup topilmadi — hali AI tuzatish bo\'lmagan.'}
    restored = []
    errors = []
    for name in os.listdir(latest):
        rel = name.replace('__', os.sep)
        abs_path = _resolve_abs(rel)
        if abs_path is None:
            errors.append(f"{rel}: ruxsat etilmagan — tashlab ketildi")
            continue
        try:
            shutil.copy2(os.path.join(latest, name), abs_path)
            restored.append(rel)
        except Exception as exc:
            errors.append(f"{rel}: {type(exc).__name__}: {str(exc)[:120]}")

    # Audit trail
    try:
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            action='ai_code_revert',
            target_type='code',
            description=f"AI kod tuzatish qaytarildi ({actor_username}): {', '.join(restored) or '—'}",
        )
    except Exception:
        pass

    return {'ok': not errors and bool(restored), 'restored': restored, 'errors': errors,
            'backup_dir': os.path.basename(latest)}


def format_patch_report(result: dict) -> str:
    """AI patch / revert natijasini staff uchun HTML formatda."""
    if result.get('error'):
        return f"⚠️ {result['error']}"
    lines = []
    if result.get('applied'):
        lines.append("✅ <b>Kod yangilandi</b> (backup saqlandi):")
        for f in result['applied']:
            lines.append(f"  • <code>{f}</code>")
        lines.append(f"\n💾 Backup: <code>backups/ai_fix/{result.get('backup_dir', '')}/</code>")
        lines.append("\nYoqmasa — <b>/qaytar</b> yozing, asl holatga qaytadi.")
    elif result.get('restored'):
        lines.append("↩️ <b>Asl holatga qaytarildi</b>:")
        for f in result['restored']:
            lines.append(f"  • <code>{f}</code>")
    if result.get('errors'):
        lines.append("\n⚠️ Xatolar:")
        for e in result['errors']:
            lines.append(f"  • {e}")
    return '\n'.join(lines) or 'Hech narsa o\'zgarmadi.'


def _gemini_patch(problem: str, health_text: str = '') -> dict:
    """Gemini'dan kod tuzatish patch'ini so'raydi. Returns {'ok', 'patch', 'analysis'}."""
    try:
        from apps.settings_app.models import Setting
        key = Setting.get_setting('gemini_api_key', '') or ''
        model = Setting.get_setting('gemini_model', 'gemini-1.5-flash') or 'gemini-1.5-flash'
        if not key:
            return {'ok': False, 'error': 'gemini_api_key sozlanmagan'}
        import urllib.request
        import urllib.parse
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
        prompt = (
            "Sen DONZO platformasining egasiga yordam beradigan aqlli dasturchi yordamchisan.\n"
            "Quyida tizim holati va foydalanuvchi tasvirlagan muammo berilgan.\n"
            "Vazifa: muammoni tahlil qil va agar kodda tuzatish kerak bo'lsa, "
            "patch taklif qil. Patch JSON formatda bo'lsin:\n"
            "{\"replacements\": [{\"file\": \"relative/path.py\", \"old\": \"eski aniq matn\", \"new\": \"yangi matn\"}]}\n"
            "Qoidalar:\n"
            "- file backend/ ildiziga nisbatan (masalan apps/security/staff_ai.py).\n"
            "- old matn fayldagi ANIQ, noyob qism bo'lsin (1 ta joyda uchrasin).\n"
            "- old va new so'zma-so'z bir-biriga mos bo'lsin — boshqa hech narsa o'zgarmasin.\n"
            "- Agar kod o'zgarishi shart bo'lmasa: {\"replacements\": []}\n"
            "- JAVOBDA FAQAT JSON qaytar — boshqa hech narsa yozma.\n\n"
            f"== TIZIM HOLATI ==\n{health_text[:1500]}\n\n"
            f"== MUAMMO ==\n{problem[:800]}"
        )
        body = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 1500},
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST',
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode('utf-8')
        data = json.loads(raw)
        text = data['candidates'][0]['content']['parts'][0]['text']
        # JSON blokni ajratib olamiz (Gemini ba'zan ```json ... ``` qaytaradi)
        cleaned = text.strip()
        if '```' in cleaned:
            import re as _re
            m = _re.search(r'```(?:json)?\s*(.*?)\s*```', cleaned, _re.DOTALL)
            if m:
                cleaned = m.group(1).strip()
        patch = json.loads(cleaned)
        return {'ok': True, 'patch': patch, 'analysis': text[:400]}
    except Exception as exc:
        return {'ok': False, 'error': f"Gemini patch xato: {type(exc).__name__}: {str(exc)[:150]}"}


def ai_code_fix(problem: str, actor_username: str = 'staff', health_text: str = '') -> dict:
    """AI kod tuzatish oqimi: Gemini tahlil → patch → backup bilan qo'llash.

    Returns: {'ok', 'applied', 'errors', 'backup_dir', 'analysis', 'error'}
    """
    if not (problem or '').strip():
        return {'ok': False, 'error': 'Muammo tavsifi bo\'sh.'}
    g = _gemini_patch(problem, health_text or '')
    if not g.get('ok'):
        return {'ok': False, 'error': g.get('error', 'AI tahlil qila olmadi')}
    patch = g.get('patch') or {}
    reps = patch.get('replacements') or []
    if not reps:
        return {'ok': True, 'applied': [], 'errors': [], 'backup_dir': '',
                'analysis': 'AI tahlil: kod o\'zgarishi shart emas.', 'note': 'no_change'}
    res = apply_ai_patch(patch, actor_username)
    res['analysis'] = g.get('analysis', '')
    return res
