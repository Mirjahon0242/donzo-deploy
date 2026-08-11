"""
Bind owner telegram_id to the legacy admin account (holds 180,000 so'm balance)
and clean up test users created during e2e testing.
"""
import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from apps.users.models import User

OWNER_TG = '2007554600'

# 1. Remove the fresh owner account created by e2e test (id=16, empty balance)
fresh = User.objects.filter(telegram_id=OWNER_TG, username='topuphub_owner').first()
if fresh:
    fresh.delete()
    print(f"Deleted fresh owner account (id={fresh.id} placeholder) — it held 0 balance")

# 2. Bind telegram_id to the legacy admin account (id=1, super_admin, 180,000 balance)
legacy = User.objects.filter(username='admin', role='super_admin').first()
if legacy:
    legacy.telegram_id = OWNER_TG
    legacy.telegram_username = 'topuphub_owner'
    legacy.is_staff = True
    legacy.is_superuser = True
    legacy.save(update_fields=['telegram_id', 'telegram_username', 'is_staff', 'is_superuser'])
    print(f"Bound telegram_id {OWNER_TG} -> legacy admin (id={legacy.id}, balance={legacy.balance})")
else:
    print("WARN: legacy admin not found")

# 3. Clean up e2e test user (operator_candidate / telegram 987654321)
tc = User.objects.filter(telegram_id='987654321').first()
if tc:
    tc.delete()
    print(f"Deleted e2e test user (id={tc.id})")

# 4. Verify no duplicate telegram_id remains
dupes = User.objects.filter(telegram_id=OWNER_TG)
print(f"Users with owner telegram_id now: {list(dupes.values_list('id', 'username', 'balance'))}")

# 5. Verify all staff holders still valid
print("Staff holders:", list(User.objects.filter(role__in=['super_admin', 'admin', 'senior_operator', 'operator', 'support']).values_list('username', 'role', 'telegram_id')))
