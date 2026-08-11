#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kunlik audit hisoboti — TelegramWebAppSession jadvalidan kunlik
muvaffaqiyat/muvaffaqiyatsiz statistikasini tuzib, staff (admin/operator)
Telegram chatlariga yuboradi.

Ishlatish:
  python daily_audit_report.py              # kecha (UTC kun) hisoboti, yuboradi
  python daily_audit_report.py --dry-run    # faqat chop etadi, yubormaydi
  python daily_audit_report.py --hours 24   # oxirgi 24 soat
  python daily_audit_report.py --force      # kunlik himoyani chetlab o'tish
  python daily_audit_report.py --install    # Windows Task Scheduler'ga kunlik 09:00 vazifa

Kunlik himoya: har bir sana uchun bir marta yuboradi (Setting'da marker) —
Task Scheduler ikki marta ishga tushirsa ham dublikat yubormaydi.
"""
import argparse
import os
import sys
import datetime
from pathlib import Path

# Windows konsoli cp1251/cp866 bo'lishi mumkin — emoji/UTF-8 matn
# print() da UnicodeEncodeError tashlamasligi uchun stdout'ni UTF-8 ga
# o'tkazamiz (bot_supervisor'dagi kabi).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, str(Path(__file__).parent))
import django
django.setup()

from django.db.models import Count
from django.utils import timezone
from collections import Counter

from apps.settings_app.models import Setting
from apps.users.models import User, TelegramWebAppSession
from apps.users.telegram_notify import _send_message, NOTIFY_ROLES

SCRIPT = Path(__file__).resolve()
PYTHON = Path(sys.executable).resolve()


def compute_stats(start, end):
    """Compute audit stats for [start, end) — all times UTC."""
    qs = TelegramWebAppSession.objects.filter(opened_at__gte=start, opened_at__lt=end)
    total = qs.count()
    ok = qs.filter(is_authenticated=True).count()
    fail = total - ok

    # Error code breakdown (failed attempts)
    err_codes = dict(
        qs.filter(is_authenticated=False).values_list('error_code')
        .annotate(n=Count('id')).order_by('-n')
    )

    # Users / IPs
    distinct_tg = qs.filter(is_authenticated=True, telegram_id__isnull=False)\
        .values('telegram_id').distinct().count()
    new_users = User.objects.filter(created_at__gte=start, created_at__lt=end).count()

    ip_counts = Counter(
        qs.exclude(ip_address__isnull=True).exclude(ip_address='')
        .values_list('ip_address', flat=True)
    )
    distinct_ips = len(ip_counts)
    suspicious_ips = {ip: n for ip, n in ip_counts.items() if n >= 10}

    # Top failing IPs (failed attempts only)
    fail_ip_counts = Counter(
        qs.filter(is_authenticated=False).exclude(ip_address__isnull=True)
        .exclude(ip_address='').values_list('ip_address', flat=True)
    )
    top_fail_ips = fail_ip_counts.most_common(3)

    last = qs.order_by('-opened_at').first()

    return {
        'total': total, 'ok': ok, 'fail': fail,
        'err_codes': err_codes,
        'distinct_tg': distinct_tg, 'new_users': new_users,
        'distinct_ips': distinct_ips, 'suspicious_ips': suspicious_ips,
        'top_fail_ips': top_fail_ips,
        'last_ts': last.opened_at if last else None,
    }


def build_message(label, s):
    lines = [f"📊 <b>Kunlik audit hisoboti — {label}</b>\n"]
    lines.append("🔑 <b>Telegram WebApp loginlar:</b>")
    lines.append(f"• Jami: <b>{s['total']}</b>")
    lines.append(f"• ✅ Muvaffaqiyatli: <b>{s['ok']}</b>")
    lines.append(f"• ❌ Muvaffaqiyatsiz: <b>{s['fail']}</b>")

    if s['err_codes']:
        lines.append("\n❌ <b>Muvaffaqiyatsiz sabablar:</b>")
        for code, n in s['err_codes'].items():
            lines.append(f"• {code or '?'}: {n}")

    lines.append("\n👥 <b>Foydalanuvchilar:</b>")
    lines.append(f"• Turli telegram akkaunt: {s['distinct_tg']}")
    lines.append(f"• Yangi userlar: {s['new_users']}")

    lines.append("\n🌐 <b>IP statistikasi:</b>")
    lines.append(f"• Turli IP: {s['distinct_ips']}")
    for ip, n in s['top_fail_ips']:
        lines.append(f"• Muvaffaqiyatsiz: {ip} ({n} marta)")
    if s['suspicious_ips']:
        lines.append("\n⚠️ <b>Shubhali (10+ urinish bir IP'dan):</b>")
        for ip, n in list(s['suspicious_ips'].items())[:5]:
            lines.append(f"• {ip}: {n} urinish")
    else:
        lines.append("\n✅ Shubhali IP yo'q")

    if s['last_ts']:
        lines.append(f"\n🕐 Oxirgi urinish: {s['last_ts'].strftime('%d.%m %H:%M:%S')} UTC")

    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description='Kunlik audit hisoboti')
    ap.add_argument('--dry-run', action='store_true', help='faqat chop etish, yubormaslik')
    ap.add_argument('--hours', type=int, default=0,
                    help='oxirgi N soat (berilmasa: kecha, to\'liq UTC kun)')
    ap.add_argument('--force', action='store_true', help='kunlik himoyani chetlab o\'tish')
    ap.add_argument('--install', action='store_true',
                    help='Windows Task Scheduler\'ga kunlik 09:00 vazifa qo\'shish')
    args = ap.parse_args()

    if args.install:
        task = "DONZO_DailyAuditReport"
        cmd = (f'schtasks /create /tn "{task}" /tr '
               f'"\\"{PYTHON}\\" \\"{SCRIPT}\\" --force" '
               f'/sc daily /st 09:00 /f')
        print(f'Vazifa yaratilmoqda: {task}')
        print(f'  Buyruq: {cmd}')
        rc = os.system(cmd)
        print(f'  Natija kodi: {rc}')
        if rc == 0:
            print('✅ Task Scheduler\'ga qo\'shildi — har kuni 09:00 da ishlaydi.')
            print(f'  O\'chirish: schtasks /delete /tn "{task}" /f')
        return rc

    now = timezone.now()
    if args.hours:
        start = now - datetime.timedelta(hours=args.hours)
        end = now
        label = f"oxirgi {args.hours} soat"
    else:
        # Kecha — to'liq UTC kun
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start - datetime.timedelta(days=1)
        end = day_start
        label = start.strftime('%d.%m.%Y')

    # Kunlik himoya (yuborilgan sanani belgilash)
    marker = f'daily_audit_report_{start.date().isoformat()}'
    if not args.force and Setting.get_setting(marker, ''):
        print(f'⚠️  {start.date()} uchun hisobot allaqachon yuborilgan '
              f'(--force bilan chetlab o\'tish mumkin).')
        return 0

    stats = compute_stats(start, end)
    text = build_message(label, stats)

    print(text)
    print('\n' + '=' * 40)

    if stats['total'] == 0:
        print('⚠️  Davrda hech qanday login bo\'lmagan — hisobot yuborilmaydi.')
        return 0

    if args.dry_run:
        print('--dry-run: xabar yuborilmadi.')
        return 0

    # Staff chatlariga yuborish — asosiy bot tokeni, ishlamasa eski (alt) bot
    bot_token = Setting.get_setting('telegram_bot_token', '')
    alt_token = Setting.get_setting('telegram_bot_token_alt', '')
    if not bot_token:
        print('❌ Bot token sozlanmagan — yuborib bo\'lmadi.')
        return 1

    chat_ids = _staff_ids()
    if not chat_ids:
        print('❌ Telegram bog\'langan staff topilmadi.')
        return 1

    sent = 0
    for chat_id in chat_ids:
        ok = False
        # 1) Asosiy bot (@DONZOROBOT) orqali
        try:
            ok = _send_message(bot_token, chat_id, text)
        except Exception as exc:
            print(f'  {chat_id} (asosiy bot): {exc}')
        # 2) Eski bot (@TopTupUzbot) orqali — u bilan chat boshlaganlar uchun
        if not ok and alt_token:
            try:
                ok = _send_message(alt_token, chat_id, text)
                if ok:
                    print(f'  {chat_id}: eski bot orqali yuborildi')
            except Exception as exc:
                print(f'  {chat_id} (eski bot): {exc}')
        if ok:
            sent += 1

    if sent:
        Setting.set_setting(marker, str(now.isoformat()))
        print(f'✅ {sent}/{len(chat_ids)} chatga yuborildi. Marker saqlandi.')
    else:
        print('❌ Hech bir chatga yuborilmadi.')
    return 0 if sent else 1


def _staff_ids():
    """Staff telegram chat ids (super_admin/admin/operator/support)."""
    return set(
        User.objects.filter(
            role__in=NOTIFY_ROLES,
            telegram_id__isnull=False,
        ).exclude(telegram_id='').values_list('telegram_id', flat=True)
    )


if __name__ == '__main__':
    sys.exit(main())
