"""
Seed data script for TOPUP HUB.
Run: python seed_data.py
"""
import os
import sys
import django

sys.path.append(os.path.dirname(__file__))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.users.models import User, Role
from apps.services.models import Category, Service, Package, ServiceField

print("[SEED] Seeding data...")

# Create/Get Categories
cats_data = [
    ('mobile-games', 'Mobile Games', 'mobile-games', 1),
    ('pc-games', 'PC Games', 'pc-games', 2),
    ('social', 'Social & Messaging', 'social', 3),
    ('streaming', 'Streaming & Entertainment', 'streaming', 4),
]
cats = {}
for key, name, slug, order_index in cats_data:
    cat, created = Category.objects.get_or_create(
        slug=slug,
        defaults={'name': name, 'order_index': order_index}
    )
    cats[key] = cat
    print(f"  {'Created' if created else 'Found'} category: {name}")


def create_service(slug, defaults, fields, packages):
    """Create or update a service with fields and packages."""
    svc, created = Service.objects.get_or_create(slug=slug, defaults=defaults)
    if not created:
        # Update only if this is a fresh installation (no orders exist)
        from apps.orders.models import Order
        if not Order.objects.filter(service=svc).exists():
            for k, v in defaults.items():
                setattr(svc, k, v)
            svc.save()
            print(f"  Updated service details: {svc.name}")
        else:
            print(f"  Skipped update (orders exist): {svc.name}")
    print(f"  {'Created' if created else 'Already exists'} service: {svc.name}")

    # Create/update fields
    for field_data in fields:
        fname = field_data.pop('field_name')
        ServiceField.objects.update_or_create(
            service=svc, field_name=fname,
            defaults=field_data
        )

    # Create packages
    for pkg_name, pkg_label, pkg_price, pkg_order in packages:
        Package.objects.get_or_create(
            service=svc, name=pkg_name,
            defaults={
                'amount_label': pkg_label,
                'price': pkg_price,
                'currency': 'UZS',
                'order_index': pkg_order,
            }
        )

    return svc


# === ALL SERVICES ===

# 1. Mobile Legends
create_service('mobile-legends', {
    'name': 'Mobile Legends', 'category': cats['mobile-games'],
    'description': "Mobile Legends: Bang Bang - eng mashhur MOBA o'yini. Diamond, Weekly Pass va boshqa to'lovlarni amalga oshiring.",
    'instruction_text': "Game ID va Server ID ni o'yin ichidagi profilingizdan topishingiz mumkin.",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/3/3c/Mobile_Legends_Bang_Bang_logo.png',
}, [
    {'field_name': 'game_id', 'field_label': 'Game ID', 'field_type': 'number', 'is_required': True, 'validation_regex': r'^\d{6,10}$', 'order_index': 1},
    {'field_name': 'server_id', 'field_label': 'Server ID', 'field_type': 'number', 'is_required': True, 'validation_regex': r'^\d{4}$', 'order_index': 2},
], [
    ('86 Diamond', '86 Diamond', 15000, 0), ('172 Diamond', '172 Diamond', 29000, 1),
    ('257 Diamond', '257 Diamond', 43000, 2), ('344 Diamond', '344 Diamond', 57000, 3),
    ('429 Diamond', '429 Diamond', 71000, 4), ('514 Diamond', '514 Diamond', 85000, 5),
    ('706 Diamond', '706 Diamond', 113000, 6), ('Twilight Pass', 'Twilight Pass', 120000, 7),
    ('Weekly Pass', 'Weekly Pass', 45000, 8),
])

# 2. PUBG Mobile
create_service('pubg-mobile', {
    'name': 'PUBG Mobile', 'category': cats['mobile-games'],
    'description': "PUBG Mobile - Battle Royale o'yini. UC, Royale Pass va boshqa mahsulotlarni sotib oling.",
    'instruction_text': "Game ID ni o'yin ichidagi profilingizdan topishingiz mumkin.",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/2/2f/PUBG_Mobile_logo.svg',
}, [
    {'field_name': 'game_id', 'field_label': 'Game ID', 'field_type': 'number', 'is_required': True, 'order_index': 1},
], [
    ('60 UC', 'Unknown Cash 60', 12000, 0), ('300 UC', 'Unknown Cash 300', 59000, 1),
    ('600 UC', 'Unknown Cash 600', 115000, 2), ('1500 UC', 'Unknown Cash 1500', 285000, 3),
    ('3000 UC', 'Unknown Cash 3000', 569000, 4), ('Royale Pass', 'Royale Pass', 150000, 5),
])

# 3. Free Fire
create_service('free-fire', {
    'name': 'Free Fire', 'category': cats['mobile-games'],
    'description': "Free Fire - dinamik Battle Royale o'yini. Diamond va Elite Pass sotib oling.",
    'instruction_text': "Game ID ni o'yin ichidagi profilingizdan topishingiz mumkin.",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/9/9c/Free_Fire_Battlegrounds_logo.svg',
}, [
    {'field_name': 'game_id', 'field_label': 'Game ID', 'field_type': 'number', 'is_required': True, 'order_index': 1},
], [
    ('70 Diamond', '70 Diamond', 7000, 0), ('140 Diamond', '140 Diamond', 14000, 1),
    ('355 Diamond', '355 Diamond', 35000, 2), ('720 Diamond', '720 Diamond', 70000, 3),
    ('1450 Diamond', '1450 Diamond', 140000, 4), ('Elite Pass', 'Elite Pass', 55000, 5),
])

# 4. Telegram Premium
create_service('telegram-premium', {
    'name': 'Telegram Premium', 'category': cats['social'],
    'description': "Telegram Premium va Telegram Stars. Premium hisobingizni to'ldiring.",
    'instruction_text': "Telegram username (masalan: @username) yoki telefon raqamingizni kiriting.",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/commons/8/82/Telegram_logo.svg',
}, [
    {'field_name': 'username', 'field_label': 'Telegram Username', 'field_type': 'text', 'is_required': True, 'order_index': 1},
], [
    ('1 oy Premium', 'Telegram Premium 1 oy', 45000, 0), ('3 oy Premium', 'Telegram Premium 3 oy', 125000, 1),
    ('6 oy Premium', 'Telegram Premium 6 oy', 245000, 2), ('12 oy Premium', 'Telegram Premium 12 oy', 480000, 3),
    ('50 Stars', 'Telegram Stars 50', 12000, 4), ('100 Stars', 'Telegram Stars 100', 23000, 5),
    ('250 Stars', 'Telegram Stars 250', 55000, 6), ('500 Stars', 'Telegram Stars 500', 110000, 7),
])

# 5. Steam Wallet
create_service('steam-wallet', {
    'name': 'Steam Wallet', 'category': cats['pc-games'],
    'description': "Steam hamyonini to'ldiring. O'yinlar va dasturlar sotib oling.",
    'instruction_text': "Steam ID yoki profilingiz linkini kiriting.",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/commons/8/83/Steam_icon_logo.svg',
}, [
    {'field_name': 'steam_id', 'field_label': 'Steam ID / Profile URL', 'field_type': 'text', 'is_required': True, 'order_index': 1},
], [
    ('10$ Wallet', 'Steam Wallet 10$', 120000, 0), ('20$ Wallet', 'Steam Wallet 20$', 240000, 1),
    ('50$ Wallet', 'Steam Wallet 50$', 590000, 2), ('100$ Wallet', 'Steam Wallet 100$', 1180000, 3),
])

# 6. Valorant
create_service('valorant', {
    'name': 'Valorant', 'category': cats['pc-games'],
    'description': "Valorant - Riot Gamesning strategik shooter o'yini. VP sotib oling.",
    'instruction_text': "Riot ID va Tagline ni kiriting (masalan: Player#NA1).",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/6/6b/Valorant_logo.svg',
}, [
    {'field_name': 'riot_id', 'field_label': 'Riot ID (ism)', 'field_type': 'text', 'is_required': True, 'order_index': 1},
    {'field_name': 'tagline', 'field_label': 'Tagline (#NA1)', 'field_type': 'text', 'is_required': True, 'order_index': 2},
], [
    ('475 VP', 'Valorant Points 475', 45000, 0), ('1000 VP', 'Valorant Points 1000', 90000, 1),
    ('2050 VP', 'Valorant Points 2050', 180000, 2), ('3650 VP', 'Valorant Points 3650', 315000, 3),
    ('5350 VP', 'Valorant Points 5350', 450000, 4), ('11000 VP', 'Valorant Points 11000', 900000, 5),
])

# 7. Roblox
create_service('roblox', {
    'name': 'Roblox', 'category': cats['pc-games'],
    'description': "Roblox - virtual olam. Robux va Premium sotib oling.",
    'instruction_text': "Roblox username yoki ID kiriting.",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/2/2f/Roblox_logo_2022.svg',
}, [
    {'field_name': 'username', 'field_label': 'Roblox Username', 'field_type': 'text', 'is_required': True, 'order_index': 1},
], [
    ('400 Robux', '400 Robux', 40000, 0), ('800 Robux', '800 Robux', 75000, 1),
    ('1700 Robux', '1700 Robux', 150000, 2), ('4500 Robux', '4500 Robux', 370000, 3),
    ('Premium 1 oy', 'Roblox Premium 1 oy', 25000, 4),
])

# 8. Brawl Stars
create_service('brawl-stars', {
    'name': 'Brawl Stars', 'category': cats['mobile-games'],
    'description': "Brawl Stars - Supercellning jangovar o'yini. Gems va Brawl Pass sotib oling.",
    'instruction_text': "Player tag ni kiriting (masalan: #2Y8...)",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/e/ef/Brawl_Stars_logo.svg',
}, [
    {'field_name': 'player_tag', 'field_label': 'Player Tag (#2Y8...)', 'field_type': 'text', 'is_required': True, 'order_index': 1},
], [
    ('30 Gems', '30 Gems', 12000, 0), ('80 Gems', '80 Gems', 29000, 1),
    ('170 Gems', '170 Gems', 55000, 2), ('360 Gems', '360 Gems', 110000, 3),
    ('800 Gems', '800 Gems', 230000, 4), ('Brawl Pass', 'Brawl Pass', 75000, 5),
])

# 9. Clash Royale
create_service('clash-royale', {
    'name': 'Clash Royale', 'category': cats['mobile-games'],
    'description': "Clash Royale - Supercellning real-time strategiya o'yini. Gems va Pass Royale sotib oling.",
    'instruction_text': "Player tag ni kiriting (masalan: #2Y8...)",
    'is_active': True,
    'image_url': 'https://upload.wikimedia.org/wikipedia/en/5/5f/Clash_Royale_logo.svg',
}, [
    {'field_name': 'player_tag', 'field_label': 'Player Tag (#2Y8...)', 'field_type': 'text', 'is_required': True, 'order_index': 1},
], [
    ('500 Gems', '500 Gems', 15000, 0), ('1200 Gems', '1200 Gems', 35000, 1),
    ('2500 Gems', '2500 Gems', 70000, 2), ('6500 Gems', '6500 Gems', 170000, 3),
    ('14000 Gems', '14000 Gems', 350000, 4), ('Pass Royale', 'Pass Royale', 55000, 5),
])

# 10. Standoff 2
create_service('standoff-2', {
    'name': 'Standoff 2', 'category': cats['mobile-games'],
    'description': "Standoff 2 - aksiyon FPS o'yini. Diamant (💎) bilan donat qiling.",
    'instruction_text': "Game ID ni o'yin ichidagi profilingizdan topishingiz mumkin.",
    'is_active': True,
    'image_url': 'https://play-lh.googleusercontent.com/BzFzyK022sdG6grfJqkwj3KoNFAxp0aQ7kYFzZwwfbHZvaMkViEQDco68Xt_tk4us6XrCG6ST3CJT32W3KutDQ=w256',
}, [
    {'field_name': 'game_id', 'field_label': 'Game ID', 'field_type': 'number', 'is_required': True, 'order_index': 1},
], [
    ('100 Diamond', '100💎', 10990, 0), ('500 Diamond', '500💎', 54990, 1),
    ('1000 Diamond', '1000💎', 109990, 2), ('1500 Diamond', '1500💎', 169990, 3),
    ('2000 Diamond', '2000💎', 219990, 4), ('3000 Diamond', '3000💎', 329990, 5),
])

# Force-update image URLs even for services with existing orders
# Real game logos hosted on reliable CDNs (jsdelivr simple-icons + Google Play store CDN)
image_urls = {
    'mobile-legends': 'https://play-lh.googleusercontent.com/D8r13ijO9c-0_1N-CP4d63mR1w6YhDuR2mBQUl27ELJAx0sKdaKtM5vCUnSLODKBVzUx7rZ9cW4Ir9jYiufsSQ=w256',
    'pubg-mobile': 'https://cdn.jsdelivr.net/npm/simple-icons/icons/pubg.svg',
    'free-fire': 'https://play-lh.googleusercontent.com/JT88XmsHoGDio7FxONwh382DhuTxuccfMmWFDtRBFjilySzNqWOCxUhqm8IhBKzQSwVrW2HWp_XvSgKFwi3ETA=w256',
    'telegram-premium': 'https://cdn.jsdelivr.net/npm/simple-icons/icons/telegram.svg',
    'steam-wallet': 'https://cdn.jsdelivr.net/npm/simple-icons/icons/steam.svg',
    'valorant': 'https://cdn.jsdelivr.net/npm/simple-icons/icons/valorant.svg',
    'roblox': 'https://cdn.jsdelivr.net/npm/simple-icons/icons/roblox.svg',
    'brawl-stars': 'https://play-lh.googleusercontent.com/c0hXyphuxh-gpnhSJGZV1I0IpWbq9IdEc1pautS7SmHlXNBrCff7bMqK-u63pJdfP3KJoxamG7W1dRMKr7ZzWKs=w256',
    'clash-royale': 'https://play-lh.googleusercontent.com/z0rspJKftanEI7MA4WOdypbaaHfeKy4UjoawRGKf4Ys3v6LrrcleZWOfms7XK-J33Oqyfm3DlFd4Z_eKWafVFg=w256',
    'standoff-2': 'https://play-lh.googleusercontent.com/BzFzyK022sdG6grfJqkwj3KoNFAxp0aQ7kYFzZwwfbHZvaMkViEQDco68Xt_tk4us6XrCG6ST3CJT32W3KutDQ=w256',
}
for slug, url in image_urls.items():
    updated = Service.objects.filter(slug=slug).update(image_url=url)
    if updated:
        print(f"  Updated image_url for: {slug}")
    else:
        print(f"  Service not found: {slug}")

# Make sure admin is super admin
try:
    user = User.objects.get(username='admin')
    user.role = Role.SUPER_ADMIN
    user.save()
    print(f"  Admin user '{user.username}' updated to {user.role}")
except User.DoesNotExist:
    print("  Admin user not found, skipping")

# Seed payment provider settings
from apps.settings_app.models import Setting

# Fragment API (Telegram Stars & Premium auto-fulfillment) — defaults only;
# real values set via Admin panel → Kalitlar.
fragment_settings = {
    'fragment_api_base_url': 'https://fragment-api.uz/api/v1',
    'fragment_api_key': '',
    'fragment_usd_uzs_rate': '12800',
    'fragment_price_margin_percent': '15',
    'fragment_price_sync_enabled': 'True',
    'fragment_last_price_sync': '',
    'fragment_last_sync_result': '',
}
for key, value in fragment_settings.items():
    s, created = Setting.objects.get_or_create(
        key=key,
        defaults={'value': value, 'description': f'Fragment API setting: {key}'}
    )
    if created:
        print(f"  Created setting: {key}")

payment_settings = {
    'click_merchant_id': 'YOUR_CLICK_MERCHANT_ID',
    'click_secret_key': 'YOUR_CLICK_SECRET_KEY',
    'click_service_id': 'YOUR_CLICK_SERVICE_ID',
    'payme_merchant_id': 'YOUR_PAYME_MERCHANT_ID',
    'payme_secret_key': 'YOUR_PAYME_SECRET_KEY',
    'uzum_merchant_id': 'YOUR_UZUM_MERCHANT_ID',
    'uzum_secret_key': 'YOUR_UZUM_SECRET_KEY',
}

for key, value in payment_settings.items():
    s, created = Setting.objects.get_or_create(
        key=key,
        defaults={'value': value, 'description': f'Payment provider setting: {key}'}
    )
    if created:
        print(f"  Created setting: {key}")

# Seed notification templates
from apps.notifications.models import Notification

notification_templates = [
    ('order_completed', 'telegram', "Salom {customer_name}! #{order_number} - {service_name} ({package_name}) buyurtmangiz bajarildi! ✅"),
    ('order_processing', 'telegram', "Salom {customer_name}! #{order_number} - {service_name} buyurtmangiz bajarilmoqda... 🔵"),
    ('order_pending', 'telegram', "Salom {customer_name}! #{order_number} - {service_name} buyurtmangiz qabul qilindi va tekshirilmoqda. 🟡"),
    ('order_cancelled', 'telegram', "Salom {customer_name}! #{order_number} - {service_name} buyurtmangiz bekor qilindi. Sabab: operator bilan bog'lanishingiz mumkin. 🔴"),
    ('payment_received', 'telegram', "Salom {customer_name}! #{order_number} - {service_name} buyurtmangiz uchun to'lov qabul qilindi. ✅"),
    ('order_completed', 'sms', "{service_name} buyurtmangiz bajarildi! #{order_number}"),
    ('order_pending', 'sms', "Buyurtmangiz qabul qilindi: #{order_number}"),
]

for event_type, channel, template_text in notification_templates:
    n, created = Notification.objects.get_or_create(
        event_type=event_type,
        channel=channel,
        defaults={'template_text': template_text, 'is_active': True}
    )
    if created:
        print(f"  Created notification: {event_type} ({channel})")

print("\n[SEED] SUCCESS!")
print(f"  Categories: {Category.objects.count()}")
print(f"  Services: {Service.objects.count()}")
print(f"  Packages: {Package.objects.count()}")
print(f"  Fields: {ServiceField.objects.count()}")
print(f"  Settings: {Setting.objects.count()}")
print(f"  Notifications: {Notification.objects.count()}")

print("\n--- Test Buyurtma ---")
print("Buyurtma berish uchun:")
print('  curl -X POST http://localhost:8000/api/v1/orders/ \\')
print('    -H "Content-Type: application/json" \\')
print('    -d \'{"service": 1, "package": 1, "field_values": {"game_id": "123456", "server_id": "1234"}, "customer_name": "Test User", "customer_telegram": "@testuser"}\'')
print()
print("Tolov usullarini korish:")
print('  curl http://localhost:8000/api/v1/payments/providers/')
print()
print("Payment init (buyurtma yaratilgandan keyin):")
print('  curl -X POST http://localhost:8000/api/v1/payments/init/ \\')
print('    -H "Content-Type: application/json" \\')
print('    -d \'{"order_id": 1, "provider": "balance"}\'')
