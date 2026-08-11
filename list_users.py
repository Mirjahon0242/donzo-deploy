# -*- coding: utf-8 -*-
"""Dev helper: list users + recent login codes (live-test planning)."""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.users.models import User, TelegramLoginCode  # noqa: E402

print('=== Users (test candidates) ===')
for u in User.objects.all().order_by('id')[:15]:
    print(f"  id={u.id} role={u.role} tg={u.telegram_id or '-'} "
          f"uname={u.telegram_username or '-'} first={u.first_name or '-'} bal={u.balance}")
print('=== Recent login codes ===')
for c in TelegramLoginCode.objects.order_by('-created_at')[:5]:
    print(f"  tg={c.telegram_id} used={c.used} expires={c.expires_at}")
