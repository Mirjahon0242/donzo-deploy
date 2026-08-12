"""
DONZO auth views — DEMO MODE.

Login tizimi butunlay olib tashlandi: Telegram initData verifikatsiyasi
(HMAC), kod-login, email/password login — hech biri yo'q. Ilova DEMO
rejimda ishlaydi: frontend sahifa yo'nalishiga qarab mos demo-foydalanuvchi
bilan avtomatik kiradi.

  POST /api/v1/auth/demo-login/   {role: customer|admin|operator|support}
      → {access, refresh, user} — get_or_create, idempotent.

Profil, Fragment sync va logout endpointlari saqlanib qolgan
(admin panel va user qismi o'zgarmaydi).
"""
import logging
import time
from datetime import timedelta

from django.utils import timezone
from rest_framework import status, generics, permissions
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.throttling import ScopedRateThrottle


class LoginCodeThrottle(ScopedRateThrottle):
    """Kod so'rash — 10/min/IP (brute-force guard)."""
    scope = 'login_code'


class LoginCodeVerifyThrottle(ScopedRateThrottle):
    """Kodni tekshirish — 20/min/IP."""
    scope = 'login_code_verify'
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, Role
from .serializers import UserSerializer, ProfileUpdateSerializer
from .fragment_profile import sync_fragment_profile
from apps.settings_app.models import Setting

logger = logging.getLogger(__name__)

# Telegram ID of the owner — automatically granted Super Admin on login.
# Configurable via Settings (key: super_admin_telegram_id).
DEFAULT_SUPER_ADMIN_TELEGRAM_ID = '2007554600'


def get_super_admin_telegram_id():
    """Return the telegram_id (as str) that should auto-become super admin."""
    val = Setting.get_setting('super_admin_telegram_id', DEFAULT_SUPER_ADMIN_TELEGRAM_ID)
    if not val:
        return DEFAULT_SUPER_ADMIN_TELEGRAM_ID
    return str(val)  # always compare as string (Setting may hold a JSON number)


def get_tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {
        'refresh': str(refresh),
        'access': str(refresh.access_token),
    }


# ── DEMO MODE ─────────────────────────────────────────────────────────────
# Login o'chirilgan: har bir rol uchun bitta demo-foydalanuvchi. Frontend
# sahifa yo'nalishiga qarab mos rol bilan kiradi (user sahifalari → mijoz,
# /admin → admin, /operator → operator, /support → support).
DEMO_USERS = {
    'customer': {
        'username': 'demo_customer',
        'email': 'demo_customer@donzo.local',
        'role': 'customer',
        'first_name': 'Demo',
        'last_name': 'Mijoz',
    },
    'admin': {
        'username': 'demo_admin',
        'email': 'demo_admin@donzo.local',
        # XAVFSIZLIK: demo-login HECH QACHON super_admin bermaydi — faqat
        # customer. (Avval super_admin edi — tunnel orqali {role:'admin'}
        # yuborib har kim egasi bo'lishi mumkin edi.)
        'role': 'customer',
        'first_name': 'Demo',
        'last_name': 'Admin',
    },
    'operator': {
        'username': 'demo_operator',
        'email': 'demo_operator@donzo.local',
        'role': 'operator',
        'first_name': 'Demo',
        'last_name': 'Operator',
    },
    'support': {
        'username': 'demo_support',
        'email': 'demo_support@donzo.local',
        'role': 'support',
        'first_name': 'Demo',
        'last_name': 'Support',
    },
}


def _get_demo_user(role: str):
    spec = DEMO_USERS.get(role) or DEMO_USERS['customer']
    user, created = User.objects.get_or_create(
        email=spec['email'],
        defaults={
            'username': spec['username'],
            'role': spec['role'],
            'first_name': spec['first_name'],
            'last_name': spec['last_name'],
            'is_active': True,
        },
    )
    if created:
        user.set_unusable_password()
        user.save()
    # Eski DB'da rol boshqacha bo'lib qolgan bo'lsa — demo rolini tiklaymiz.
    if user.role != spec['role']:
        user.role = spec['role']
        user.save(update_fields=['role'])
    return user


def _report_login_error(kind: str, error_code: str, username: str = ''):
    """Login xatosi → AI tahlil + staff guruhiga xabar (thread, throttled).

    Fire-and-forget: login oqimini hech qachon sekinlatmaydi/buzmaydi.
    Throttle: bir xil turdagi xato 10 daqiqada bir marta xabar qilinadi.
    Username maxfiy emas — faqat diagnostika uchun yuboriladi.
    """
    try:
        import threading
        def _send():
            try:
                from apps.security.ai_ops import report_error_to_staff
                report_error_to_staff(
                    {
                        'kind': 'login',
                        'component': f'views.py ({kind})',
                        'error_code': error_code,
                        'detail': f'Login muvaffaqiyatsiz ({kind})',
                        'extra': {'error_code': error_code, 'username': username[:60]},
                    },
                    throttle_key=f'login_{kind}_{error_code}',
                    throttle_seconds=600,
                )
            except Exception:
                pass
        threading.Thread(target=_send, daemon=True).start()
    except Exception:
        pass


def _client_ip(request) -> str:
    """Client IP (XFF chap tomoni) — faqat sessiya metadata uchun."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()[:45]
    return request.META.get('REMOTE_ADDR', '')[:45]


def _capture_login_meta(user, request):
    """Anti-fraud: kirishda IP + joylashuv + vaqtni user'ga yozadi.

    IP-geolokatsiya best-effort (24h kesh) — hech qachon login'ni sekinlatmaydi
    yoki buzmaz. Session metadata ham TelegramWebAppSession'ga yoziladi.

    Maydonlar:
      last_ip         — oxirgi IP manzil
      last_ip_location — IP bo'yicha joylashuv ("Toshkent, UZ · ISP")
      last_location   — TO'LIQ manzil (GPS reverse-geocode; bo'lmasa IP label)
      geo_lat/geo_lng — koordinata (GPS yoki IP taxminiy)
    """
    try:
        from .geoip import geolocate, location_label
        ip = _client_ip(request)
        geo = geolocate(ip) if ip else None
        label = location_label(geo) if geo else ''
        user.last_ip = ip or user.last_ip
        if label:
            user.last_ip_location = label
            # GPS to'liq manzili bo'lmasa — IP label'ini umumiy joylashuvga yozamiz
            if not user.last_location:
                user.last_location = label
        # IP bo'yicha TAXMINIY koordinata — GPS ruxsat bermagan bo'lsa ham
        # xaritada joylashuv ko'rinishi uchun (aniq emas, IP darajasida).
        # Aniq GPS kelgach ustidan yoziladi va geo_source='gps' bo'ladi.
        if geo and geo.get('lat') is not None and geo.get('lon') is not None and user.geo_lat is None:
            user.geo_lat = geo['lat']
            user.geo_lng = geo['lon']
            user.geo_source = 'ip'
        user.last_user_agent = user.last_user_agent or (request.META.get('HTTP_USER_AGENT', '') or '')[:500]
        user.last_seen_at = timezone.now()
        user.save(update_fields=['last_ip', 'last_ip_location', 'last_location', 'last_user_agent', 'last_seen_at', 'geo_lat', 'geo_lng', 'geo_source'])
        return label
    except Exception:
        logger.exception('Login meta yozishda xato (ahamiyatsiz)')
        return ''


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def demo_login(request):
    """
    POST /api/v1/auth/demo-login/

    FAQAT DEBUG (lokal ishlab chiqish) uchun: rol bo'yicha demo
    foydalanuvchini qaytaradi. Production'da (DEBUG=False) bu endpoint
    404 qaytaradi — himoyasiz super_admin JWT beradigan teshik bo'lmasligi
    uchun (har kim {role:'admin'} yuborib super_admin bo'lishi mumkin edi).
    """
    from django.conf import settings
    if not settings.DEBUG:
        return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)

    role = str(request.data.get('role') or 'customer').lower()
    if role not in DEMO_USERS:
        role = 'customer'

    user = _get_demo_user(role)
    tokens = get_tokens_for_user(user)

    # "Jonli sessiyalar" admin ekrani jonli qolishi uchun demo-sessiya
    # yozamiz (faqat metadata — user, vaqt, IP; hech qanday maxfiy yo'q).
    try:
        from .models import TelegramWebAppSession
        TelegramWebAppSession.objects.create(
            user=user,
            telegram_id=user.telegram_id or '',
            is_authenticated=True,
            launch_source='demo',
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:250],
            ip_address=_client_ip(request),
        )
        TelegramWebAppSession.prune_old()
    except Exception:
        logger.exception('Demo sessiya yozishda xato (ahamiyatsiz)')

    logger.info('[Demo] login: role=%s user=%s', role, user.username)
    return Response({
        'refresh': tokens['refresh'],
        'access': tokens['access'],
        'user': UserSerializer(user).data,
    })


# ── FRAGMENT LOGIN ────────────────────────────────────────────────────────
# Web app ochilganda foydalanuvchi Telegram username'ini kiritadi → backend
# Fragment API (getInfo) orqali uning ma'lumotlarini (ism, rasm, premium)
# oladi → shu ma'lumot LOGIN sifatida qabul qilinadi. Ma'lumotlar User
# yozuviga (user id) biriktiriladi va JWT qaytariladi — keyin foydalanuvchi
# user id orqali aniqlanadi.
#
# Xavfsizlik: getInfo muvaffaqiyatsiz bo'lsa login HAM muvaffaqiyatsiz —
# Fragment ma'lumoti login identifikatorining o'zi.


def _normalize_username(raw) -> str:
    """@ belgisi va bo'sh joylarni tozalaydi, kichik harfga o'tkazadi."""
    return (raw or '').strip().lstrip('@').strip().lower()


def _get_info_with_retry(username: str):
    """getInfo'ni bir necha marta urinadi (API vaqti-vaqti bilan timeout
    beradi). Muvaffaqiyatli dict yoki {error: ...} qaytaradi.

    Qisqa timeout (10s): auto-login ekrani uzoq osilib qolmasligi uchun —
    muvaffaqiyatsizlikda frontend kod oqimiga tez o'tadi."""
    from apps.services import fragment_api
    info = None
    last_err = ''
    for attempt in range(2):
        info = fragment_api.get_info(username, timeout=10)
        if isinstance(info, dict) and not info.get('error'):
            return info, None
        err = (info or {}).get('error') or {}
        last_err = err.get('code') if isinstance(err, dict) else ''
        if attempt < 1:
            time.sleep(1)
    return info, last_err or 'FRAGMENT_ERROR'


def _get_or_create_user_by_username(username: str, info: dict):
    """Username bo'yicha foydalanuvchini topadi/yozadi va profil ma'lumotini
    biriktiradi (user id = identifikator).

    info: Fragment getInfo natijasi ({name, photo, is_premium}) — kod-login
    kabi Fragment'siz yo'llarda bo'sh dict beriladi (profil saqlangan holda
    qoladi). Rol: admin usernames ro'yxati → super_admin; yangi → customer;
    mavjud — saqlanadi. (user, created) qaytaradi.
    """
    user = (
        User.objects.filter(telegram_username__iexact=username).first()
        or User.objects.filter(username__iexact=username).first()
    )
    created = user is None
    if created:
        user = User(
            username=username,
            email=f"{username}@fragment.user",
            role=Role.CUSTOMER,
            is_active=True,
        )
        user.set_unusable_password()

    # ── Fragment ma'lumotlarini user id ga biriktiramiz ──
    user.telegram_username = username
    name = (info.get('name') or '').strip()
    if name:
        user.first_name = name
    photo = (info.get('photo') or '').strip()
    if photo:
        user.avatar_url = photo
    user.is_telegram_premium = bool(info.get('is_premium'))
    user.fragment_synced_at = timezone.now()

    # ── Rol: admin usernames ro'yxati → super_admin; yangi → customer; ──
    #    mavjud user — roli saqlanadi (admin panel orqali boshqariladi).
    admin_usernames = (Setting.get_setting('fragment_admin_usernames', '') or '').lower()
    admin_list = [u.strip() for u in admin_usernames.split(',') if u.strip()]
    if username in admin_list:
        user.role = Role.SUPER_ADMIN
    elif created:
        user.role = Role.CUSTOMER

    user.save()
    return user, created


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
def fragment_login(request):
    """
    POST /api/v1/auth/fragment-login/

    FRAGMENT LOGIN: foydalanuvchi Telegram username'ini yuboradi, backend
    Fragment API (POST /getInfo) orqali jonli ma'lumotni oladi:

        {username, name, photo, is_premium}

    Ma'lumotlar User yozuviga (user id) biriktiriladi — first_name, avatar,
    premium holati, telegram_username. Foydalanuvchi mavjud bo'lsa topiladi
    (telegram_username bo'yicha), aks holda yangi mijoz yaratiladi. JWT
    qaytariladi; keyingi so'rovlarda user id orqali aniqlanadi.

    Admin: `fragment_admin_usernames` Setting (vergul bilan) dagi username
    bilan kirganlar avtomatik super_admin bo'ladi.
    """
    username = _normalize_username(request.data.get('username'))
    if not username:
        return Response(
            {'detail': 'Telegram username kiriting'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Telegram akkaunt mosligi tekshiruvi ──
    # Frontend Telegram WebApp initDataUnsafe'dan JORIY akkaunt username'ini
    # yuboradi (telegram_username). Agar yuborilgan bo'lsa va kiritilgan
    # username'ga mos kelmasa — login rad etiladi: foydalanuvchi faqat O'Z
    # Telegram username'i bilan kirishi mumkin (boshqa birovning username'ini
    # kiritib kirish yo'q). Frontend asosiy darvoza, bu esa ikkinchi himoya
    # qatlami.
    telegram_username = _normalize_username(request.data.get('telegram_username') or '')
    if telegram_username and telegram_username != username:
        logger.info('[FragmentLogin] username mos emas: typed=%s tg=%s', username, telegram_username)
        return Response(
            {'detail': f"@{username} Telegram akkauntingizga mos emas. Siz @{telegram_username} akkauntidasiz — faqat o'z username'ingiz bilan kirishingiz mumkin."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # ── Foydalanuvchini avval BAZADAN topamiz (user id = identifikator) ──
    # Bitta so'rovda ikki marta qidirmaslik uchun natijani qayta ishlatamiz.
    user = (
        User.objects.filter(telegram_username__iexact=username).first()
        or User.objects.filter(username__iexact=username).first()
    )
    created = user is None

    if user is not None:
        # ── MAVJUD user: tez yo'l ──
        # getInfo BIR marta sinanadi — yangi profil ma'lumotini olish uchun.
        # Xato bo'lsa (Fragment API user topolmasa / rate-limit) saqlangan
        # profil bilan kirishga ruxsat beramiz: uz_ultra kabi real mijozlar
        # Fragment API'da topilmasa ham bloklanmaydi. Retry kerak emas,
        # chunki fallback mavjud — login tez bo'ladi.
        from apps.services import fragment_api
        info = fragment_api.get_info(username, timeout=12)
        verified = isinstance(info, dict) and not info.get('error')
        if not verified:
            logger.info('[FragmentLogin] mavjud user fallback (getInfo xato): username=%s', username)
            info = {}  # saqlangan profil ma'lumotlari ishlatiladi
    else:
        # ── YANGI user: Fragment tekshiruvi MAJBURIY ──
        # Soxta username bilan akkaunt ochilmasin — getInfo muvaffaqiyatsiz
        # bo'lsa login ham muvaffaqiyatsiz (3 urinish, flaky API uchun).
        info, err = _get_info_with_retry(username)
        verified = info is not None and isinstance(info, dict) and not info.get('error')
        if not verified:
            logger.info('[FragmentLogin] getInfo muvaffaqiyatsiz, yangi user rad etildi username=%s err=%s', username, err)
            # AI xato tahlili → staff guruhiga (throttled: 10 daqiqada bir marta)
            _report_login_error('fragment_verify', err, username)
            return Response(
                {'detail': f"Fragment orqali tasdiqlanmadi ({err}). To'g'ri Telegram username kiriting."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

    user, created = _get_or_create_user_by_username(username, info)
    logger.info('[FragmentLogin] %s -> user#%s (created=%s, role=%s)',
                username, user.pk, created, user.role)

    # Anti-fraud: IP + joylashuv + vaqt user'ga va sessiyaga yoziladi
    loc_label = _capture_login_meta(user, request)

    tokens = get_tokens_for_user(user)

    # "Jonli sessiyalar" admin ekrani uchun metadata (hech qanday maxfiy yo'q)
    try:
        from .models import TelegramWebAppSession
        TelegramWebAppSession.objects.create(
            user=user,
            telegram_id=user.telegram_id or '',
            is_authenticated=True,
            launch_source='fragment',
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:250],
            ip_address=_client_ip(request),
            location=loc_label,
        )
        TelegramWebAppSession.prune_old()
    except Exception:
        logger.exception('Fragment sessiya yozishda xato (ahamiyatsiz)')

    return Response({
        'refresh': tokens['refresh'],
        'access': tokens['access'],
        'user': UserSerializer(user).data,
        'fragment': info,
    })


# ── BOT ORQALI TASDIQLASH KODI ────────────────────────────────────────────
# Login amalga oshmasa foydalanuvchi username kiritadi → backend kod yaratib
# bot orqali shu foydalanuvchining Telegram chatiga yuboradi → foydalanuvchi
# kodni web app'ga kiritadi → JWT. Kod SHA-256 hash bo'lib saqlanadi,
# bir martalik, 5 daqiqa yaroqli (code_utils / TelegramLoginCode).


def _bot_chat_username(telegram_id: str):
    """Telegram bot orqali telegram_id ning username'ini oladi (getChat).

    Username mosligini tekshirish uchun: kod faqat o'sha telegram_id ga
    yuboriladi — agar username mos kelmasa, boshqa birovning username'ini
    egallash (takeover) mumkin bo'lardi. getChat bajarilmasa (bot foydalanuvchi
    bilan ishlamagan) None qaytadi — kod ham yuborilmaydi.

    Qaytadi: username (normalized) yoki None.
    """
    from apps.settings_app.models import Setting
    token = (Setting.get_setting('telegram_bot_token', '') or '').strip()
    if not token or not telegram_id:
        return None
    try:
        import requests
        resp = requests.get(
            f'https://api.telegram.org/bot{token}/getChat',
            params={'chat_id': str(telegram_id)}, timeout=8,
        )
        if not resp.ok:
            return None
        data = resp.json()
        if not data.get('ok'):
            return None
        uname = (data.get('result') or {}).get('username') or ''
        return _normalize_username(uname) or None
    except Exception:
        return None


def _verify_username_real(username: str):
    """Username haqiqiy Telegram user ekanini tekshiradi (fragment_login bilan
    bir xil qoida): mavjud user — tez yo'l; yangi user — Fragment majburiy.

    (info, error) qaytaradi. error bo'lsa kod yuborilmaydi (soxta username).
    """
    user = (
        User.objects.filter(telegram_username__iexact=username).first()
        or User.objects.filter(username__iexact=username).first()
    )
    if user is not None:
        from apps.services import fragment_api
        info = fragment_api.get_info(username, timeout=20)
        if not (isinstance(info, dict) and not info.get('error')):
            info = {}
        return info, None
    info, err = _get_info_with_retry(username)
    if info is None or not isinstance(info, dict) or info.get('error'):
        return None, err or 'FRAGMENT_ERROR'
    return info, None


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([LoginCodeThrottle])
def request_login_code(request):
    """
    POST /api/v1/auth/login-code/

    Foydalanuvchi username kiritadi → backend tasdiqlash kodini yaratib
    @DONZOROBOT orqali shu foydalanuvchining Telegram chatiga yuboradi.

    Body: {username, telegram_id?}
      • telegram_id — Telegram WebApp initDataUnsafe.user.id (JORIY akkaunt).
        Berilgan bo'lsa kod FAQAT bot orqali ketadi (javobda kod YO'Q).
      • telegram_id berilmasa (dev/test, Telegramdan tashqari) — kod javobda
        qaytadi (test oqimi uchun; production'da har doim Telegram ichida).

    Xavfsizlik: kod bir martalik, 5 daqiqa, SHA-256 hash saqlanadi; javobda
    hech qachon kod qaytmaydi (Telegram ichida). Throttle 10/min/IP.
    """
    from .code_utils import create_login_code, send_code_to_chat

    username = _normalize_username(request.data.get('username'))
    if not username:
        return Response({'detail': 'Telegram username kiriting'}, status=status.HTTP_400_BAD_REQUEST)

    telegram_id = str(request.data.get('telegram_id') or '').strip()

    if telegram_id:
        # ── Telegram ichida: username ↔ telegram_id mosligini BOT orqali
        # tekshiramiz (getChat). Kod shu telegram_id ga yuboriladi — agar
        # username mos kelmasa, boshqa birovning username'ini egallash
        # (account takeover) mumkin bo'lardi. Bu tekshiruv Fragment'siz ham
        # yangi foydalanuvchilarga kod orqali kirishni ochadi.
        chat_username = _bot_chat_username(telegram_id)
        if chat_username is None:
            logger.info('[LoginCode] getChat bajarilmadi username=%s tg=%s', username, telegram_id)
            return Response(
                {'detail': "Tasdiqlash kodi yuborilmadi. @DONZOROBOT'ni ochib Start tugmasini bosing, so'ng qayta urinib ko'ring."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if chat_username != username:
            logger.info('[LoginCode] username mos emas: typed=%s chat=%s', username, chat_username)
            return Response(
                {'detail': f"@{username} Telegram akkauntingizga mos emas. Siz @{chat_username} akkauntidasiz — faqat o'z username'ingiz bilan kirishingiz mumkin."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # ── Kod bot orqali chatga yuboriladi ──
        code_obj = create_login_code(telegram_id, tg_username=username)
        bot_token = (Setting.get_setting('telegram_bot_token', '') or '').strip()
        sent = bool(bot_token) and send_code_to_chat(bot_token, telegram_id, code_obj.plain_code)
        if not sent:
            logger.info('[LoginCode] kod yuborilmadi username=%s tg=%s', username, telegram_id)
            return Response(
                {'detail': "Tasdiqlash kodi yuborilmadi. @DONZOROBOT'ni ochib Start tugmasini bosing, so'ng qayta urinib ko'ring."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({'status': 'sent', 'detail': 'Kod Telegram bot orqali yuborildi'})

    # ── Telegramdan tashqari (desktop/dev): telegram_id yo'q — username
    # haqiqiy ekanini Fragment orqali tekshiramiz (soxta username'ga kod
    # yuborilmaydi). Mavjud user — tez yo'l; yangi user — Fragment majburiy.
    _info, err = _verify_username_real(username)
    if err:
        logger.info('[LoginCode] username tasdiqlanmadi username=%s err=%s', username, err)
        return Response(
            {'detail': f"{username} tasdiqlanmadi ({err}). To'g'ri Telegram username kiriting."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Dev / Telegramdan tashqari: kodni javobda qaytaramiz ──
    code_obj = create_login_code('dev', tg_username=username)
    return Response({'status': 'dev', 'code': code_obj.plain_code,
                     'detail': 'Dev rejim: kod Telegramga yuborilmadi, javobda berildi'})


@api_view(['POST'])
@permission_classes([permissions.AllowAny])
@throttle_classes([LoginCodeVerifyThrottle])
def verify_login_code(request):
    """
    POST /api/v1/auth/login-code/verify/

    Foydalanuvchi botdan olgan 6 xonali kodni kiritadi → tekshiriladi →
    JWT qaytariladi (username identifikator, user id ga biriktiriladi).

    Body: {username, code}
    Kod username'ga bog'langan, bir martalik, 5 daqiqa yaroqli.
    """
    from .code_utils import hash_code
    from .models import TelegramLoginCode

    username = _normalize_username(request.data.get('username'))
    code = str(request.data.get('code') or '').strip()
    if not username or not code:
        return Response({'detail': 'Username va kod kiritish shart'}, status=status.HTTP_400_BAD_REQUEST)

    now = timezone.now()
    obj = (
        TelegramLoginCode.objects
        .filter(telegram_username__iexact=username, code=hash_code(code),
                used=False, expires_at__gte=now)
        .order_by('-created_at')
        .first()
    )
    if obj is None:
        return Response(
            {'detail': "Kod noto'g'ri yoki muddati o'tgan. Yangi kod so'rab qayta urinib ko'ring."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # ── Atomik iste'mol (faqat bitta so'rov yutadi) ──
    consumed = TelegramLoginCode.objects.filter(pk=obj.pk, used=False).update(used=True)
    if not consumed:
        return Response({'detail': 'Kod allaqachon ishlatilgan.'}, status=status.HTTP_400_BAD_REQUEST)

    # Kod Telegram orqali tasdiqlandi — Fragment ma'lumotisiz ham login o'tadi
    # (kodning o'zi tasdiqlash). Profil saqlangan holda qoladi.
    user, created = _get_or_create_user_by_username(username, {})
    logger.info('[LoginCode] %s kod bilan kirdi -> user#%s (created=%s)', username, user.pk, created)

    # Telegram akkaunt id'sini bog'laymiz (agar raqamli bo'lsa va band bo'lmasa)
    if obj.telegram_id and obj.telegram_id != 'dev' and obj.telegram_id.isdigit():
        if not user.telegram_id:
            user.telegram_id = obj.telegram_id
            user.save(update_fields=['telegram_id'])

    # Anti-fraud: IP + joylashuv + vaqt user'ga va sessiyaga yoziladi
    loc_label = _capture_login_meta(user, request)

    tokens = get_tokens_for_user(user)

    # "Jonli sessiyalar" admin ekrani uchun metadata
    try:
        from .models import TelegramWebAppSession
        TelegramWebAppSession.objects.create(
            user=user,
            telegram_id=obj.telegram_id or '',
            is_authenticated=True,
            launch_source='login_code',
            user_agent=request.META.get('HTTP_USER_AGENT', '')[:250],
            ip_address=_client_ip(request),
            location=loc_label,
        )
        TelegramWebAppSession.prune_old()
    except Exception:
        logger.exception('LoginCode sessiya yozishda xato (ahamiyatsiz)')

    return Response({
        'refresh': tokens['refresh'],
        'access': tokens['access'],
        'user': UserSerializer(user).data,
    })


class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfileUpdateSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user

    def retrieve(self, request, *args, **kwargs):
        # Profile ochilganda Fragment getInfo bilan boyitish (agar 24h o'tgan
        # bo'lsa) — background thread, javobni bloklamaydi.
        try:
            sync_fragment_profile(request.user)
        except Exception:
            logger.exception('Fragment profile sync trigger failed')
        return Response(UserSerializer(request.user).data)


class ProfileSyncFragmentView(generics.GenericAPIView):
    """
    POST /api/v1/auth/profile/sync-fragment/

    Profilni Fragment API (getInfo) bilan HOZIROQ sinxronlaydi: ism,
    avatar va Telegram Premium holati.

    • force=True — 24 soatlik interval chetlab o'tiladi, LEKIN 5 daqiqalik
      grace bor: oxirgi sync'dan keyin 5 daqiqa ichida qayta so'rov tashqi
      Fragment API'ga bormaydi;
    • faqat o'z profilini sinxronlaydi (request.user) — IDOR yo'q;
    • rate-limit: 1 daqiqada 6 so'rov (IP bo'yicha).
    """
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fragment_sync'

    GRACE = timedelta(minutes=5)

    def post(self, request):
        from django.utils import timezone as dj_timezone
        from .fragment_profile import _sync_user

        user = request.user
        try:
            age = dj_timezone.now() - user.fragment_synced_at
            if user.fragment_synced_at and timedelta(0) <= age < self.GRACE:
                return Response({
                    'status': 'fresh',
                    'user': UserSerializer(user).data,
                })
        except TypeError:
            pass  # noto'g'ri sana — davom etamiz
        try:
            status_ = _sync_user(user, force=True)
        except Exception:
            logger.exception('Fragment force-sync failed for user %s', user.pk)
            status_ = 'error'

        if not isinstance(status_, str):
            status_ = str(status_)

        return Response({
            'status': status_,
            'user': UserSerializer(user).data,
        })


class DeviceInfoView(generics.GenericAPIView):
    """
    POST /api/v1/auth/profile/device-info/

    Anti-fraud: foydalanuvchi qurilmasi va joylashuvi haqidagi metadata'ni
    qabul qilib user'ga saqlaydi (admin panelda ko'rinadi).

    Body (hammasi ixtiyoriy):
      platform, language, timezone, screen_width, screen_height,
      user_agent, lat, lng  (lat/lng — brauzer geolokatsiyasi, ruxsat bo'lsa)

    Faqat request.user'ning o'z ma'lumotini yozadi (IDOR yo'q).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from decimal import Decimal, InvalidOperation
        from .geoip import location_label

        user = request.user
        data = request.data or {}
        changed = False

        platform = str(data.get('platform') or '').strip()[:100]
        if platform:
            user.last_platform = platform
            changed = True
        language = str(data.get('language') or '').strip()[:10]
        if language:
            user.last_language = language
            changed = True
        timezone_ = str(data.get('timezone') or '').strip()[:60]
        if timezone_:
            user.last_timezone = timezone_
            changed = True
        ua = str(data.get('user_agent') or '').strip()[:500]
        if ua:
            user.last_user_agent = ua
            changed = True
        # IP va uning joylashuvini ham yangilaymiz (so'rov kelgan IP bo'yicha)
        ip = _client_ip(request)
        if ip and ip != user.last_ip:
            user.last_ip = ip
            changed = True
            from .geoip import geolocate, location_label
            geo = geolocate(ip)
            label = location_label(geo) if geo else ''
            if label:
                user.last_ip_location = label
                # GPS to'liq manzili bo'lmasa — IP label'ini umumiy joylashuvga yozamiz
                if not user.last_location:
                    user.last_location = label
                changed = True
            # IP bo'yicha TAXMINIY koordinata — GPS kelmasa ham xarita uchun
            if geo and geo.get('lat') is not None and geo.get('lon') is not None and user.geo_lat is None:
                user.geo_lat = geo['lat']
                user.geo_lng = geo['lon']
                user.geo_source = 'ip'
                changed = True
        # Brauzer geolokatsiyasi (agar ruxsat berilgan bo'lsa) — ANIQ GPS
        lat = str(data.get('lat') or '').strip()
        lng = str(data.get('lng') or '').strip()
        try:
            if lat and lng:
                user.geo_lat = Decimal(lat)
                user.geo_lng = Decimal(lng)
                # GPS — qurilmadan aniq koordinata: IP taxminiy ustidan yoziladi
                user.geo_source = 'gps'
                changed = True
                # GPS koordinatadan TO'LIQ MANZIL: ko'cha, tuman, shahar
                # (reverse geocoding, Nominatim/OpenStreetMap — bepul).
                # IP-joylashuvdan aniqroq — foydalanuvchi ruxsat bergan
                # GPS nuqta bo'yicha. Xato bo'lsa eski qiymat qoladi.
                from .geoip import reverse_geocode
                full_address = reverse_geocode(lat, lng)
                if full_address:
                    user.last_location = full_address[:200]
                    changed = True
        except (InvalidOperation, ValueError):
            pass  # noto'g'ri koordinata — e'tiborsiz

        user.last_seen_at = timezone.now()
        changed = True
        if changed:
            user.save()
        return Response({'status': 'ok', 'user': UserSerializer(user).data})


class LogoutView(generics.GenericAPIView):
    """
    POST /api/v1/auth/logout/

    Blacklists the supplied refresh token (JWT token blacklist), so the
    session is invalidated server-side and the token can no longer be used
    to mint new access tokens after rotation. Returns 200 even when the
    token is already blacklisted/expired (idempotent logout).

    Body: { "refresh": "<refresh token>" }
    """
    permission_classes = [permissions.AllowAny]
    # Generous throttle — logout is cheap and idempotent.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'telegram_auth'

    def post(self, request):
        from rest_framework_simplejwt.exceptions import TokenError

        refresh = (request.data.get('refresh') or '').strip()
        if not refresh:
            return Response(
                {'detail': 'refresh token yetishmayapti'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            token = RefreshToken(refresh)
            token.blacklist()
            logger.info("Logout: refresh token blacklisted (user jti=%s)", token.get('jti'))
        except TokenError:
            # Already blacklisted / expired — treat as logged out anyway.
            pass
        except Exception:
            logger.exception("Logout: blacklist failed")

        return Response({'detail': 'Hisobdan chiqdingiz'})
