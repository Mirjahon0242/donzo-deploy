"""
Fragment API live price sync.

Telegram Premium va Stars paketlari narxlarini Fragment API ning jonli
narxlari bilan avtomatik yangilaydi (kuniga bir marta):

    POST /premium/pricing   ->  {packages: [{months, ton, usd}, ...]}
    POST /stars/pricing     ->  {amount, price: {ton, usd, selected}} (har paket uchun)

Narx konvertatsiyasi (faqat backendda):

    UZS = USDT  *  fragment_usd_uzs_rate  *  (1 + margin%)

Sozlamalar (Admin panel → Kalitlar → Fragment API):
    fragment_api_key            — API key (X-API-Key header)
    fragment_usd_uzs_rate        — 1 USDT = necha so'm (default 12800)
    fragment_price_margin_percent— narx ustiga ustama foizi (default 15)
    fragment_price_sync_enabled  — True/False (kunlik sinxronlash yoqilganmi)
    fragment_last_price_sync     — oxirgi muvaffaqiyatli sinxronlash vaqti
    fragment_last_sync_result    — oxirgi sinxronlash natijasi (admin panel)

Xavfsizlik: faqat 'telegram-premium' xizmatining paketlari o'zgaradi;
narx hisoblab bo'lmagan paketga tegilmaydi; hech qachon loglarga secret
yozilmaydi.
"""

import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, ROUND_UP

from django.utils import timezone

from apps.settings_app.models import Setting

logger = logging.getLogger(__name__)

SYNC_SERVICE_SLUG = 'telegram-premium'
DEFAULT_USD_UZS_RATE = 12800
DEFAULT_MARGIN_PERCENT = 15
DEFAULT_SYNC_ENABLED = 'True'
SYNC_INTERVAL_HOURS = 24

# Premium paket nomidagi muddat (oy) — API packages[].months bilan solishtiriladi
SUPPORTED_PREMIUM_MONTHS = (3, 6, 12)


def _get_rate() -> Decimal:
    try:
        return Decimal((Setting.get_setting('fragment_usd_uzs_rate', '') or '').strip() or DEFAULT_USD_UZS_RATE)
    except Exception:
        return Decimal(DEFAULT_USD_UZS_RATE)


def _get_margin() -> Decimal:
    try:
        return Decimal((Setting.get_setting('fragment_price_margin_percent', '') or '').strip() or DEFAULT_MARGIN_PERCENT)
    except Exception:
        return Decimal(DEFAULT_MARGIN_PERCENT)


def sync_enabled() -> bool:
    val = (Setting.get_setting('fragment_price_sync_enabled', '') or '').strip().lower()
    return val not in ('', '0', 'false', 'no', 'off')


def _round_uzs(value: Decimal) -> int:
    """Narxni chiroyli raqamga yaxlitlaydi (1000 ga karrali, pastga emas)."""
    value = Decimal(value).quantize(Decimal('1'), rounding=ROUND_UP)
    if value < 1000:
        return int(value)
    # 12 345 -> 13 000; 123 456 -> 124 000
    rounded = (value / Decimal(1000)).quantize(Decimal('1'), rounding=ROUND_UP) * 1000
    return int(rounded)


def _usdt_to_uzs(usdt: str) -> int:
    """USDT qiymatini UZS ga o'tkazadi (rate + margin bilan)."""
    try:
        base = Decimal(str(usdt))
    except Exception:
        raise ValueError(f"noto'g'ri USDT qiymat: {usdt!r}")
    rate = _get_rate()
    margin = _get_margin()
    total = base * rate * (Decimal(1) + margin / Decimal(100))
    return _round_uzs(total)


def _parse_stars_amount(package_name: str):
    m = re.search(r'(\d+)\s*[Ss]tars?', package_name)
    return int(m.group(1)) if m else None


def _parse_premium_months(package_name: str):
    m = re.search(r'(\d+)\s*(?:oy|month)', package_name)
    return int(m.group(1)) if m else None


def sync_fragment_prices(force: bool = False, timeout=None) -> dict:
    """
    Fragment API jonli narxlari bilan Telegram Premium/Stars paketlarini
    yangilaydi.

    force=False bo'lsa: (1) avtomatik sinxronlash o'chirilgan bo'lsa, yoki
    (2) oxirgi sinxronlash 24 soat ichida bo'lgan bo'lsa — o'tkazib yuboradi.
    timeout: Fragment API so'rovi uchun timeout (sekund). Default: modul
    DEFAULT_TIMEOUT (30). Admin panel qo'lda bosilganda qisqa beriladi.

    Qaytaradi:
        {'synced': bool, 'updated': int, 'skipped': int, 'result': str, ...}
    Hech qachon exception tashlamaydi — natijani settings'ga yozadi.
    """
    from apps.services.models import Package, Service
    from . import fragment_api

    result = {
        'synced': False,
        'updated': 0,
        'skipped': 0,
        'errors': 0,
        'details': [],
        'result': '',
        'ran_at': timezone.now().isoformat(),
    }

    # ── Avtomatik sinxronlash o'chirilgan bo'lsa (force bundan mustasno) ──
    if not force and not sync_enabled():
        result['result'] = "Avtomatik narx sinxronlash o'chirilgan (fragment_price_sync_enabled=False)."
        return result

    # ── Kunlik interval (force bo'lsa o'tkazib yuboriladi) ──
    if not force:
        last = Setting.get_setting('fragment_last_price_sync', '')
        if last:
            try:
                last_dt = datetime.fromisoformat(str(last))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=dt_timezone.utc)
                if timezone.now() - last_dt < timedelta(hours=SYNC_INTERVAL_HOURS):
                    result['result'] = (
                        f"Oxirgi sinxronlash {last_dt.strftime('%d.%m %H:%M')} da "
                        f"bo'lgan — navbatdagi {SYNC_INTERVAL_HOURS} soatdan keyin."
                    )
                    return result
            except (ValueError, TypeError):
                pass  # noto'g'ri sana — davom etamiz

    # ── Xizmatni topamiz ──
    try:
        service = Service.objects.get(slug=SYNC_SERVICE_SLUG)
    except Service.DoesNotExist:
        result['result'] = f"'{SYNC_SERVICE_SLUG}' xizmati topilmadi — sinxronlash o'tkazib yuborildi."
        return result

    # ── Jonli narxlarni olamiz ──
    # 1) Premium paketlari: POST /premium/pricing -> {packages: [{months, ton, usd}]}
    try:
        premium_data = fragment_api.get_premium_pricing(timeout=timeout)
        premium_prices = {
            int(p.get('months')): p for p in (premium_data.get('packages') or [])
            if p.get('months')
        }
    except fragment_api.FragmentAPIError as exc:
        result['result'] = f"Fragment API Premium narxlarni qaytarmadi: {exc}"
        Setting.set_setting('fragment_last_sync_result', result['result'])
        return result
    except Exception as exc:  # noqa: BLE001 — hech qachon chaqiruvchini urib tushirma
        logger.exception('Fragment price sync: kutilmagan xato narxlarni olishda')
        result['result'] = f"Fragment API narxlarni olishda xatolik: {exc}"
        Setting.set_setting('fragment_last_sync_result', result['result'])
        return result

    if not premium_prices:
        result['result'] = "Fragment API javobida Premium narx ma'lumotlari yo'q."
        Setting.set_setting('fragment_last_sync_result', result['result'])
        return result

    # ── Paketlarni yangilaymiz ──
    packages = list(service.packages.filter(is_active=True))
    for pkg in packages:
        new_uzs = None
        kind = ''
        try:
            months = _parse_premium_months(pkg.name)
            if months and months in SUPPORTED_PREMIUM_MONTHS:
                mon_price = premium_prices.get(months) or {}
                base = mon_price.get('usd') or mon_price.get('ton')
                if base:
                    new_uzs = _usdt_to_uzs(base)
                    kind = f"Premium {months} oy"
            else:
                stars = _parse_stars_amount(pkg.name)
                if stars:
                    # 2) Stars paketi uchun alohida narx so'rovi
                    try:
                        star_price = fragment_api.get_stars_price(stars, timeout=timeout)
                        usd = ((star_price.get('price') or {}).get('usd')
                               or (star_price.get('price') or {}).get('selected'))
                        if usd:
                            new_uzs = _usdt_to_uzs(usd)
                            kind = f"{stars} Stars"
                    except fragment_api.FragmentAPIError as exc:
                        logger.warning('Fragment Stars narxi olinmadi (%s): %s', stars, exc)

            if new_uzs is None:
                result['skipped'] += 1
                continue

            old_uzs = int(pkg.price)
            if old_uzs != new_uzs:
                pkg.price = new_uzs
                pkg.save(update_fields=['price'])
                result['updated'] += 1
                result['details'].append(f"{pkg.name}: {old_uzs:,} → {new_uzs:,} so'm")
            else:
                result['skipped'] += 1
        except Exception:
            logger.exception('Fragment price sync: paket yangilashda xatolik (%s)', pkg.name)
            result['errors'] += 1

    result['synced'] = result['errors'] == 0
    result['result'] = (
        f"{result['updated']} ta paket yangilandi, {result['skipped']} ta o'zgarishsiz, "
        f"{result['errors']} ta xato. (1 USDT = {_get_rate():,} so'm, +{_get_margin():.0f}%)"
    )

    if result['synced']:
        Setting.set_setting('fragment_last_price_sync', timezone.now().isoformat())
    Setting.set_setting('fragment_last_sync_result', result['result'])

    logger.info('Fragment price sync: %s', result['result'])
    return result
