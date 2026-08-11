"""
Fragment API profile enrichment (getInfo).

Foydalanuvchi kirganida (login) yoki profilga so'rov tushganda biz Fragment
API ning `POST /getInfo` endpointi orqali Telegram foydalanuvchisi haqidagi
jonli ma'lumotlarni olamiz va profilga saqlaymiz:

    result: {'username', 'name', 'photo', 'is_premium'}

  • name        -> first_name (Telegram'da ko'rinadigan to'liq ism)
  • photo       -> avatar_url (NFT/profile rasmi)
  • is_premium  -> is_telegram_premium (Telegram Premium holati)

XAVFSIZLIK / CHEKLOVLAR:
  • getInfo faqat username bo'yicha ishlaydi — username yo'q bo'lsa o'tkazib
    yuboriladi (xato hisoblanmaydi);
  • har bir foydalanuvchi uchun ko'pi bilan 24 soatda bir marta chaqiriladi
    (fragment_synced_at orqali) — API'ni spam qilmaydi;
  • background thread'da ishlaydi — login hech qachon bloklanmaydi;
  • API kalit/initData hech qachon loglanmaydi yoki DB'ga yozilmaydi.
"""

import logging
import threading
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

# getInfo'ni har foydalanuvchi uchun qanchalik tez-tez yangilash mumkin
SYNC_INTERVAL = timedelta(hours=24)


def _apply_info(user, info: dict) -> bool:
    """getInfo natijasini user profiliaga qo'llaydi. Qaysidir maydon
    o'zgarsa True qaytaradi (saqlash faqat o'zgarish bo'lsa amalga oshadi)."""
    changed = False

    name = (info.get('name') or '').strip()
    if name and user.first_name != name:
        user.first_name = name
        changed = True

    photo = (info.get('photo') or '').strip()
    if photo and user.avatar_url != photo:
        user.avatar_url = photo
        changed = True

    is_premium = bool(info.get('is_premium'))
    if user.is_telegram_premium != is_premium:
        user.is_telegram_premium = is_premium
        changed = True

    return changed


def _log_sync(user, status: str, detail: str = ''):
    """Har bir Fragment API so'rov natijasini admin 'Loglar' (AuditLog)
    bo'limiga yozadi. Logging hech qachon sync'ni buzmasligi uchun o'z
    try/except ichida ishlaydi."""
    try:
        from apps.audit_log.models import AuditLog
        username = (user.telegram_username or user.username or '').strip()
        desc_map = {
            'updated': f"Fragment sync: @{username} — profil yangilandi (premium: {'ha' if user.is_telegram_premium else 'yo\'q'})",
            'synced': f"Fragment sync: @{username} — API OK, o'zgarish yo'q",
            # Fallback (login kabi): Fragment API bu username'ni tasdiqlay
            # olmasa ham profil SAQLANGAN holda qoladi — xato emas, holat.
            'not_verified': f"Fragment sync: @{username} — profil saqlandi (Fragment API tasdiqlay olmadi: {detail or 'FRAGMENT_ERROR'})",
            'error_transient': f"Fragment sync: @{username} — xato ({detail or 'NETWORK_ERROR'})",
        }
        AuditLog.objects.create(
            user=None,  # avtomatik tizim log'i (admin emas)
            action='fragment_sync',
            target_type='User',
            target_id=user.pk,
            description=desc_map.get(status, f"Fragment sync: @{username} — {status} {detail}"),
        )
    except Exception:
        logger.exception('Fragment sync log failed for user %s', getattr(user, 'pk', '?'))


def _sync_user(user, force: bool) -> str:
    """Bitta foydalanuvchini Fragment getInfo bilan sinxronlaydi.

    Returns status:
      'updated'          — profil yangilandi (ism/rasm/premium)
      'synced'           — yangilash shart emas edi, sync vaqti belgilandi
      'no_username'      — telegram_username yo'q (getInfo chaqirib bo'lmaydi)
      'not_configured'   — Fragment API key sozlanmagan
      'not_verified'     — FRAGMENT_ERROR (foydalanuvchi topilmadi): login kabi
                            fallback — saqlangan profil qoladi, synced_at
                            belgilanadi, xato emas (24h da 1 chaqiruv)
      'error_transient'  — vaqtinchalik xato (NETWORK_ERROR) — synced_at qo'yilmaydi
      'skipped_interval' — 24h interval ichida, force=False
    """
    # 24 soatlik interval — force bo'lmasa oxirgi sync'dan keyingi
    # chaqiruvlar o'tkazib yuboriladi (API'ni ortiqcha yuklamaslik).
    if not force and user.fragment_synced_at:
        try:
            if timezone.now() - user.fragment_synced_at < SYNC_INTERVAL:
                return 'skipped_interval'
        except TypeError:
            pass  # noto'g'ri sana bo'lsa — davom etamiz

    username = (user.telegram_username or '').strip()
    if not username:
        # Username bo'lmasa getInfo'ni chaqirib bo'lmaydi — profilni
        # yangilamasdan sync vaqtini belgilaymiz (har 24h tekshirish).
        user.fragment_synced_at = timezone.now()
        user.save(update_fields=['fragment_synced_at'])
        return 'no_username'

    from apps.services import fragment_api
    if not fragment_api.configured():
        # API key sozlanmagan — hech narsa qilmaymiz (key qo'yilgach
        # avtomatik ishlaydi).
        return 'not_configured'

    info = fragment_api.get_info(username, timeout=8)
    if not isinstance(info, dict) or info.get('error'):
        # getInfo xato qaytarsa — profilga tegilmaydi (saqlangan ma'lumot
        # qoladi — LOGIN bilan bir xil fallback).
        #   • FRAGMENT_ERROR (foydalanuvchi Fragment'da topilmadi) — endi
        #     doimiy XATO emas: login kabi profil saqlangan holda qoladi,
        #     synced_at belgilaymiz (24h da bittagina chaqiruv — API'ni
        #     spam qilmaymiz), log'ga neytral holat yoziladi;
        #   • NETWORK_ERROR / boshqa xato — vaqtinchalik, synced_at
        #     qo'ymaymiz, shunda Fragment tiklanishi bilan keyingi
        #     login/profilda darhol qayta uriniladi.
        err = ((info or {}).get('error') or {}).get('code') if isinstance((info or {}).get('error'), dict) else ''
        logger.info('Fragment getInfo skip user=%s: %s', user.pk, err or 'unknown')
        if err == 'FRAGMENT_ERROR' or not err:
            user.fragment_synced_at = timezone.now()
            user.save(update_fields=['fragment_synced_at'])
            _log_sync(user, 'not_verified', err or 'FRAGMENT_ERROR')
            return 'not_verified'
        _log_sync(user, 'error_transient', err or 'NETWORK_ERROR')
        return 'error_transient'

    changed = _apply_info(user, info)
    if changed:
        user.fragment_synced_at = timezone.now()
        user.save(update_fields=['first_name', 'avatar_url', 'is_telegram_premium',
                                 'fragment_synced_at'])
        logger.info('Fragment profile enriched user=%s (premium=%s)',
                    user.pk, user.is_telegram_premium)
        _log_sync(user, 'updated')
        return 'updated'
    else:
        # O'zgarish bo'lmasa ham sync vaqtini belgilaymiz — har 24h da
        # bittagina chaqiruv.
        user.fragment_synced_at = timezone.now()
        user.save(update_fields=['fragment_synced_at'])
        _log_sync(user, 'synced')
        return 'synced'


def _sync_in_thread(user_id: int, force: bool):
    """Haqiqiy sync — background thread ichida bajariladi (DB faqat shu yerda
    o'qiladi/yoziladi; chaqiruvchi o'z user obyektini bermaydi)."""
    from .models import User

    try:
        user = User.objects.filter(pk=user_id).first()
        if user is None:
            return
        _sync_user(user, force)
    except Exception:
        logger.exception('Fragment profile sync failed for user %s', user_id)


# ── Ommaviy (barcha mijozlar) sinxronlash holati ──
_bulk_status = {
    'running': False,
    'total': 0,
    'updated': 0,
    'failed': 0,
    'skipped': 0,
    'started_at': None,
    'finished_at': None,
}
# Ikki admin bir vaqtda ishga tushirsa ham hisoblagichlar xavfsiz bo'ladi.
_bulk_lock = threading.Lock()


def get_bulk_sync_status() -> dict:
    """Ommaviy sinxronlash jarayonining joriy holati (admin panel uchun)."""
    with _bulk_lock:
        s = dict(_bulk_status)
    if s.get('started_at'):
        s['started_at'] = s['started_at'].isoformat()
    if s.get('finished_at'):
        s['finished_at'] = s['finished_at'].isoformat()
    return s


def sync_all_fragment_profiles() -> int:
    """Barcha foydalanuvchilarni (telegram_username'li) Fragment getInfo bilan
    sinxronlashni bitta background thread'da ishga tushiradi (admin tugmasi).

    force=True — 24 soatlik interval chetlab o'tiladi. Jarayon holati
    get_bulk_sync_status() orqali kuzatiladi. Qaytaradi: jami foydalanuvchilar
    soni (thread darhol boshlanadi, bloklamaydi).
    """
    from .models import User

    # Concurrent-run guard: bitta ommaviy sync allaqachon ishlayotgan bo'lsa
    # ikkinchi thread ochilmaydi (double-hit / counter chigalligi oldini olish).
    with _bulk_lock:
        if _bulk_status['running']:
            return -1
        users = list(
            User.objects
            .exclude(telegram_username__isnull=True)
            .exclude(telegram_username='')
        )
        _bulk_status.update(
            running=True,
            total=len(users),
            updated=0,
            failed=0,
            skipped=0,
            started_at=timezone.now(),
            finished_at=None,
        )
        total = len(users)

    def _run():
        try:
            for u in users:
                try:
                    status = _sync_user(u, force=True)
                except Exception:
                    logger.exception('Bulk fragment sync user=%s failed', u.pk)
                    with _bulk_lock:
                        _bulk_status['failed'] += 1
                    continue
                with _bulk_lock:
                    if status == 'updated':
                        _bulk_status['updated'] += 1
                    elif status == 'error_transient':
                        # Faqat vaqtinchalik xatolar 'failed' sanaladi.
                        # 'not_verified' (Fragment topa olmadi) — skip:
                        # login kabi fallback, xato emas.
                        _bulk_status['failed'] += 1
                    else:
                        _bulk_status['skipped'] += 1
        except Exception:
            logger.exception('Bulk fragment sync failed')
        finally:
            with _bulk_lock:
                _bulk_status['running'] = False
                _bulk_status['finished_at'] = timezone.now()

    threading.Thread(target=_run, daemon=True, name='fragment-bulk-sync').start()
    return total


def sync_fragment_profile(user, force: bool = False):
    """Foydalanuvchi profilini Fragment getInfo bilan boyitishni ishga
    tushiradi (background thread — hech qachon chaqiruvchini bloklamaydi).

    user: User obyekti (yoki faqat .pk bo'lgan stub).
    force: True bo'lsa 24 soatlik intervalni chetlab o'tadi (admin).
    """
    try:
        # 24 soatlik interval ichida bo'lsa thread ochmaymiz (har profil GET'ida
        # keraksiz thread spawn qilinmasligi uchun — tekshiruv arzon, thread emas).
        if not force:
            try:
                if user.fragment_synced_at and timezone.now() - user.fragment_synced_at < SYNC_INTERVAL:
                    return
            except TypeError:
                pass
        user_id = user.pk if hasattr(user, 'pk') else user
        threading.Thread(
            target=_sync_in_thread,
            args=(int(user_id), bool(force)),
            daemon=True,
            name=f'fragment-profile-{user_id}',
        ).start()
    except Exception:
        logger.exception('Fragment profile sync trigger failed')
