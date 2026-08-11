"""
DONZO User Client — bir martalik sozlash (login + session).

Ishga tushirish:
    python setup_user_client.py

Nima qiladi:
  1. telegram_api_id / telegram_api_hash ni o'qiydi
     (.env: TELEGRAM_API_ID / TELEGRAM_API_HASH yoki admin panel → Kalitlar)
  2. Telefon raqamingizni so'raydi
  3. Telegram'dan kelgan kodni so'raydi (login kodi / 2FA paroli)
  4. Sessionni saqlaydi: backend/sessions/donzo_user.session
  5. Keyingi safar user_client.py shu session bilan avtomatik kiradi

api_id / api_hash olish: https://my.telegram.org → API development tools
"""
import asyncio
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django  # noqa: E402
django.setup()

from telethon import TelegramClient  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_DIR = os.path.join(BASE_DIR, 'sessions')
SESSION_FILE = os.path.join(SESSION_DIR, 'donzo_user.session')


def get_credentials():
    from apps.settings_app.models import Setting
    api_id = (Setting.get_setting('telegram_api_id', '') or '').strip() \
        or (os.getenv('TELEGRAM_API_ID', '') or '').strip()
    api_hash = (Setting.get_setting('telegram_api_hash', '') or '').strip() \
        or (os.getenv('TELEGRAM_API_HASH', '') or '').strip()
    return api_id, api_hash


async def main():
    api_id, api_hash = get_credentials()
    if not api_id or not api_hash:
        print("\n❌ telegram_api_id / telegram_api_hash topilmadi.")
        print("   1) https://my.telegram.org → API development tools dan oling.")
        print("   2) Admin panel → Kalitlar → telegram_api_id / telegram_api_hash\n")
        sys.exit(3)

    os.makedirs(SESSION_DIR, exist_ok=True)
    client = TelegramClient(SESSION_FILE, int(api_id), api_hash)

    print("\n🔐 DONZO User Client — login")
    print("──────────────────────────────────────────")
    phone = input("📱 Telefon raqamingiz (xalqaro format, +998...): ").strip()
    if not phone:
        print("Raqam kiritilmadi.")
        sys.exit(1)

    try:
        await client.start(phone=phone)
    except Exception as exc:
        print(f"\n❌ Login xatosi: {type(exc).__name__}: {exc}")
        sys.exit(1)

    me = await client.get_me()
    print(f"\n✅ Kirish muvaffaqiyatli! @{me.username or me.first_name} (id={me.id})")
    print(f"💾 Session saqlandi: {SESSION_FILE}")
    print("\nEndi 'python user_client_supervisor.py' bilan 24/7 ishga tushiring.\n")
    await client.disconnect()


if __name__ == '__main__':
    asyncio.run(main())
