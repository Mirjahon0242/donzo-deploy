"""
Fragment Stars & Premium API client (fragment-api.uz).

Live endpoint: https://fragment-api.uz/api/v1
Bu xizmat orqali Telegram Stars va Telegram Premium sotib olinadi:

    POST /getInfo           {username}                       -> foydalanuvchi ma'lumoti
    POST /stars/pricing     {amount}                         -> Stars narxi
    POST /premium/pricing   {}                               -> Premium paketlari narxlari
    POST /stars/buy         {amount, username}               -> Stars sotib olish
    POST /premium/buy       {duration, username}             -> Premium sotib olish
    POST /wallet/balance    {}                               -> loyiha hamyoni balansi
    POST /wallet/calculate  {}                               -> hamyon imkoniyatlari

Autentifikatsiya: HAR bir so'rovga `X-API-Key: <key>` header yuboriladi.
Kalit backend Settings'da `fragment_api_key` sifatida saqlanadi (browser
yoki mijozga yuborilmaydi; DB'da shifrlangan holda turadi).

Javob formati:
    ok: true  -> {"ok": true,  "message": "...", "result": {...}}
    ok: false -> {"ok": false, "message": "...", "code": "FRAGMENT_ERROR|VALIDATION_ERROR|..."}

Buy oqimi BEVOSITA — queue/poll yo'q. `stars/buy` yoki `premium/buy`
muvaffaqiyatli bo'lsa API o'zi Stars/Premium yetkazib beradi va natijani
qaytaradi (payment_method + cost).
"""

import logging

import requests

from apps.settings_app.models import Setting

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = 'https://fragment-api.uz/api/v1'
DEFAULT_TIMEOUT = 30

# API tomonidan qo'llab-quvvatlanadigan Premium muddatlari (oy)
SUPPORTED_PREMIUM_MONTHS = (3, 6, 12)
# Stars sotib olish minimal miqdori
MIN_STARS_AMOUNT = 50


class FragmentAPIError(Exception):
    """Fragment API xatosi (error_code + message bilan)."""

    def __init__(self, message, error_code=None, status_code=None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code

    def __str__(self):
        return f"[{self.error_code or 'ERR'}] {self.message}"


def get_base_url() -> str:
    url = (Setting.get_setting('fragment_api_base_url', '') or '').strip()
    return url.rstrip('/') if url else DEFAULT_BASE_URL


def get_api_key() -> str:
    return (Setting.get_setting('fragment_api_key', '') or '').strip()


def configured() -> bool:
    """Fragment API ishlashi uchun API key o'rnatilganmi."""
    return bool(get_api_key())


def _headers() -> dict:
    return {
        'Content-Type': 'application/json',
        'X-API-Key': get_api_key(),
    }


def _request(path: str, json_body=None, timeout=DEFAULT_TIMEOUT):
    """Xavfsiz POST so'rov — xato holatlarini FragmentAPIError ga o'giradi.

    HEch qachon loglarga API key yozilmaydi (faqat headers orqali o'tadi).
    """
    if not configured():
        raise FragmentAPIError(
            "Fragment API sozlanmagan: Admin panel → Kalitlar → 'Fragment API Key'",
            error_code='API_KEY_MISSING',
        )
    url = get_base_url() + path
    try:
        resp = requests.post(url, headers=_headers(), json=json_body or {}, timeout=timeout)
    except requests.RequestException as exc:
        logger.warning('Fragment API network error %s %s: %s', 'POST', path, exc)
        raise FragmentAPIError(
            "Fragment API bilan aloqa yo'q", error_code='NETWORK_ERROR'
        ) from exc

    try:
        data = resp.json()
    except ValueError:
        raise FragmentAPIError(
            f"Fragment API noto'g'ri javob (HTTP {resp.status_code})",
            error_code='BAD_RESPONSE', status_code=resp.status_code,
        )

    # API javoblarida: ok:false -> code orqali xato turini ajratamiz
    if not data.get('ok', False):
        code = data.get('code') or 'API_ERROR'
        # 401 -> VALIDATION_ERROR (kalit noto'g'ri)
        if resp.status_code == 401:
            code = 'INVALID_API_KEY'
        raise FragmentAPIError(
            data.get('message') or "Fragment API xatosi",
            error_code=code,
            status_code=resp.status_code,
        )
    return data.get('result') or data


# ─────────────────────────── Ma'lumot ───────────────────────────


def get_info(username: str, timeout=DEFAULT_TIMEOUT) -> dict:
    """Telegram foydalanuvchi ma'lumotlarini qaytaradi.

    result: {'username', 'name', 'photo', 'is_premium'}
    """
    uname = username if username.startswith('@') else f'@{username}'
    try:
        return _request('/getInfo', {'username': uname}, timeout=timeout)
    except FragmentAPIError as exc:
        return {'username': uname, 'name': '', 'photo': '', 'is_premium': False,
                'error': {'code': exc.error_code, 'message': exc.message}}


# ─────────────────────────── Narxlar ───────────────────────────


def get_stars_price(amount: int, timeout=DEFAULT_TIMEOUT) -> dict:
    """Berilgan Stars miqdori uchun narxni qaytaradi.

    result: {'amount': int, 'price': {'ton': str, 'usd': str, 'selected': str}}
    """
    return _request('/stars/pricing', {'amount': int(amount)}, timeout=timeout)


def get_premium_pricing(timeout=DEFAULT_TIMEOUT) -> dict:
    """Telegram Premium paketlari narxlarini qaytaradi.

    result: {'packages': [{'months': 3, 'ton': '8.65', 'usd': '11.99'}, ...]}
    """
    return _request('/premium/pricing', {}, timeout=timeout)


# ─────────────────────────── Sotib olish ───────────────────────────


def buy_stars(username: str, amount: int, timeout=DEFAULT_TIMEOUT) -> dict:
    """Foydalanuvchiga amount Stars sotib oladi (bevosita bajariladi).

    result: {'username', 'amount', 'payment_method', 'cost'}
    """
    amount = int(amount)
    if amount < MIN_STARS_AMOUNT:
        raise FragmentAPIError(
            f"Stars minimal miqdori {MIN_STARS_AMOUNT} (olindi: {amount})",
            error_code='INVALID_AMOUNT',
        )
    uname = username if username.startswith('@') else f'@{username}'
    return _request('/stars/buy', {'username': uname, 'amount': amount}, timeout=timeout)


def buy_premium(username: str, duration: int, timeout=DEFAULT_TIMEOUT) -> dict:
    """Foydalanuvchiga Telegram Premium sotib oladi (bevosita bajariladi).

    duration: 3, 6 yoki 12 oy.
    result: {'username', 'duration', 'payment_method', 'cost'}
    """
    if int(duration) not in SUPPORTED_PREMIUM_MONTHS:
        raise FragmentAPIError(
            f"Premium muddati 3, 6 yoki 12 bo'lishi kerak (olindi: {duration})",
            error_code='INVALID_DURATION',
        )
    uname = username if username.startswith('@') else f'@{username}'
    return _request('/premium/buy', {'username': uname, 'duration': int(duration)}, timeout=timeout)


# ─────────────────────────── Hamyon ───────────────────────────


def get_wallet_balance(timeout=DEFAULT_TIMEOUT) -> dict:
    """Loyiha hamyoni balansini qaytaradi.

    result: {'project', 'address', 'balance_ton', 'balance_usdt', 'wallet_version', 'network'}
    """
    return _request('/wallet/balance', {}, timeout=timeout)


def get_wallet_calculate(timeout=DEFAULT_TIMEOUT) -> dict:
    """Hamyon bilan nimalar olish mumkinligini hisoblaydi.

    result: {'balance_ton', 'balance_usdt', 'stars': {...}, 'premium': {'packages': []}}
    """
    return _request('/wallet/calculate', {}, timeout=timeout)


# ─────────────────────────── Health ───────────────────────────


def check_health() -> bool:
    """API jonlimi tekshiradi (key + bitta narx so'rovi orqali)."""
    try:
        get_stars_price(MIN_STARS_AMOUNT, timeout=8)
        return True
    except Exception:
        return False
