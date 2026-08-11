"""
Fragment fulfillment for Telegram Stars & Premium orders (MANUAL confirm).

Oqim:
  buyurtma to'lanadi (balance) -> Order.payment_status == 'paid'
  -> buyurtma 'pending' holatida qoladi va Admin panel → 'Telegram
     buyurtmalar' bo'limida tasdiqni kutadi (auto-fulfillment o'chirilgan).
  -> Admin 'Tasdiqlash' tugmasini bosganda auto_fulfill_order(order) DARHOL
     (sinxron) chaqiriladi:
       - paket nomidan Stars miqdori yoki Premium muddati aniqlanadi
       - fragment_api.buy_stars / buy_premium chaqiriladi (fragment-api.uz)
       - muvaffaqiyat -> order status='completed'
       - xatolik    -> order status='processing' (admin xatoni ko'radi),
                        izoh + audit log yoziladi
  -> Admin 'Rad qilish' bosganda balans qaytariladi (orders/admin_urls.py).

Qaytaruv: (ok, message) — ok=True muvaffaqiyat, ok=False xatolik,
ok=None o'tkazib yuborildi.

DOUBLE-SPEND HIMOYASI: buy so'rovi YUBORILISHIDAN OLDI `_fragment_attempted`
belgisi yoziladi. Shunda tarmoq timeout/retry holatida ham fragment'ga takroriy
xarid yuborilmaydi. Bundan tashqari confirm view 'processing' buyurtmani
qayta tasdiqlashni rad etadi.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Stars paketlari: '100 Stars' / '50 Stars' -> miqdor
# Premium paketlari: '3 oy Premium' / '6 oy' -> muddat (oy)

from apps.settings_app.models import Setting

AUTO_FULFILL_SERVICE_SLUGS = {'telegram-premium'}


def _parse_stars_amount(package_name: str):
    """Paket nomidan Stars miqdorini topadi: '100 Stars' -> 100."""
    m = re.search(r'(\d+)\s*[Ss]tars?', package_name)
    return int(m.group(1)) if m else None


def _parse_premium_duration(package_name: str):
    """Paket nomidan Premium muddatini topadi: '3 oy Premium' -> 3."""
    m = re.search(r'(\d+)\s*(?:oy|month)', package_name)
    return int(m.group(1)) if m else None


def _extract_username(order) -> str:
    """Buyurtma field_values'dan @username ni oladi."""
    fv = order.field_values or {}
    uname = fv.get('username') or fv.get('telegram') or ''
    return str(uname).strip()


def auto_fulfill_order(order, settings=None, actor=None):
    """Buyurtmani fragment API orqali yetkazib beradi.

    Faqat xizmat slug'i AUTO_FULFILL_SERVICE_SLUGS'da bo'lsa ishlaydi.
    Xatoliklar order.status = 'processing' qoldiradi (operator ko'radi).

    Qaytaruv: (ok, message) — ok=True muvaffaqiyat, ok=False xatolik,
    ok=None o'tkazib yuborildi (xizmat/buyurtma mos emas). Admin confirm
    tugmasi bu natijani foydalanuvchiga bevosita ko'rsatadi.

    IDEMPOTENT: to'langan va hali bajarilmagan buyurtma bir marta yetkaziladi.
    Oldin urinilgan bo'lsa (_fragment_attempted field_values'da) yoki buyurtma
    to'lanmagan/allaqachon tugagan bo'lsa — takroriy yetkazib berish bo'lmaydi
    (double-spend himoyasi).

    actor: yetkazib berishni boshlagan admin/operator (audit log'da kim
    tasdiqlagani ko'rinishi uchun). None bo'lsa order.customer ishlatiladi.
    """
    from apps.orders.models import Order

    # ── Idempotency / double-spend guard ──
    if order.payment_status != 'paid':
        logger.info('Fragment: order %s to\'lanmagan — yetkazib berilmadi', order.order_number)
        return None, "Buyurtma hali to'lanmagan"
    if order.status == 'completed':
        logger.info('Fragment: order %s allaqachon bajarilgan — takrorlanmaydi', order.order_number)
        return None, 'Buyurtma allaqachon tugallangan'
    fv = order.field_values or {}
    if fv.get('_fragment_attempted') == '1':
        logger.info('Fragment: order %s allaqachon yetkazib berishga urinilgan — takrorlanmaydi',
                    order.order_number)
        return None, 'Bu buyurtma allaqachon yetkazib berishga urinilgan (takroriy xarid oldi olindi)'

    service_slug = (order.service.slug if order.service else '')
    if service_slug not in AUTO_FULFILL_SERVICE_SLUGS:
        return None, 'Bu Telegram buyurtmasi emas'

    package_name = order.package.name if order.package else ''
    username = _extract_username(order)
    if not username:
        return _mark(order, 'processing',
                     "Foydalanuvchi username aniqlanmadi (field_values.username)",
                     actor=actor)

    # Xizmat sozlanmaganmi?
    if not (Setting.get_setting('fragment_api_key', '') or '').strip():
        return _mark(order, 'processing',
                     "Fragment API sozlanmagan (Admin panel → Kalitlar → Fragment API Key)",
                     actor=actor)

    try:
        # ── Premium yoki Stars? ──
        premium_months = _parse_premium_duration(package_name)
        stars_amount = _parse_stars_amount(package_name)

        if premium_months and premium_months in (3, 6, 12):
            from . import fragment_api
            logger.info('Fragment: Premium %s oy -> %s (order %s)',
                        premium_months, username, order.order_number)
            # DOUBLE-SPEND: buy yuborilishidan OLDI belgi — timeout/retry'da
            # ham takroriy xarid bo'lmaydi.
            _store_attempt(order)
            result = fragment_api.buy_premium(username, premium_months)
            return _finish(order, username, f"Premium {premium_months} oy", result, actor=actor)

        elif stars_amount and stars_amount >= 50:
            from . import fragment_api
            logger.info('Fragment: %s Stars -> %s (order %s)',
                        stars_amount, username, order.order_number)
            _store_attempt(order)
            result = fragment_api.buy_stars(username, stars_amount)
            return _finish(order, username, f"{stars_amount} Stars", result, actor=actor)

        else:
            # Qo'lda bajarilishi kerak (masalan '1 oy Premium' — fragment API
            # faqat 3/6/12 qo'llab-quvvatlaydi).
            return _mark(order, 'processing',
                         f"Avtomatik yetkazib berish bu paket uchun qo'llab-quvvatlanmaydi "
                         f"({package_name}). Operator qo'lda bajarishi kerak.",
                         actor=actor)

    except Exception as exc:  # noqa: BLE001 — hech qachon chaqiruvchini urib tushirma
        logger.exception('Fragment fulfillment failed for order %s', order.order_number)
        return _mark(order, 'processing', f"Avtomatik yetkazib berishda xatolik: {exc}",
                     actor=actor)


def _finish(order, username, what, result, actor=None):
    """Buy natijasiga qarab buyurtmani completed/processing qiladi.

    fragment-api.uz buy BEVOSITA bajariladi — request_id yo'q. `_fragment_attempted`
    belgisi auto_fulfill_order'da buy yuborilishidan oldin qo'yilgan (double-spend
    himoyasi — bu yerga kelgani = buy muvaffaqiyatli bajarilgani).

    Qaytaruv: (True, message).
    """
    # Muvaffaqiyatli javob: result {'username', 'amount'|'duration',
    # 'payment_method', 'cost'}. exception tashlansa bu funksiyaga
    # kelmaydi — auto_fulfill_order dagi except ushlaydi.
    detail = _result_summary(result)
    ok, message = _mark(order, 'completed',
                        f"✅ {what} @{username.lstrip('@')} ga yetkazildi ({detail})",
                        actor=actor)
    # Telegram orqali mijozga xabar
    try:
        from apps.users.telegram_notify import notify_order_status
        notify_order_status(order, 'processing', 'completed')
    except Exception:
        logger.exception('notify_order_status failed')
    return ok, message


def _result_summary(result: dict) -> str:
    """Fragment API buy javobidan qisqa xulosa chiqaradi."""
    if not isinstance(result, dict):
        return ''
    parts = []
    for key in ('payment_method', 'cost', 'amount', 'duration'):
        if result.get(key) is not None:
            parts.append(f"{key}: {result[key]}")
    return ', '.join(parts)


def _store_attempt(order):
    """`_fragment_attempted=1` belgisini order.field_values'ga yozadi."""
    try:
        fv = dict(order.field_values or {})
        fv['_fragment_attempted'] = '1'
        order.field_values = fv
        order.save(update_fields=['field_values', 'updated_at'])
    except Exception:
        logger.exception('fragment attempt belgisini saqlashda xatolik (order %s)', order.order_number)


def _mark(order, new_status, note, actor=None):
    """Order status maydonini yangilaydi va audit log yozadi.

    Qaytaruv: (ok, note) — ok=True faqat buyurtma 'completed' bo'lganda
    (yetkazib berish muvaffaqiyatli); aks holda False (xatolik/processing).
    """
    from apps.audit_log.models import AuditLog

    order.status = new_status
    order.save(update_fields=['status', 'updated_at'])
    AuditLog.objects.create(
        user=actor if actor else order.customer,
        action='auto_fulfillment',
        target_type='Order',
        target_id=order.id,
        description=f"#{order.order_number}: {note}",
    )
    logger.info('Order %s -> %s (%s)', order.order_number, new_status, note)
    return new_status == 'completed', note
