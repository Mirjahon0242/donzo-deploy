import base64
import hashlib
import logging
import os
import threading
import time

from django.db import models

logger = logging.getLogger(__name__)

# Settings whose values are SECRETS and must be encrypted at rest in the DB
# (bot token, payment keys, DB password, SMTP password, Django secret...).
# Everything else (site name, URLs, flags) stays plaintext.
SECRET_SETTING_KEYS = frozenset({
    'telegram_bot_token',
    'telegram_bot_token_alt',
    'health_report_bot_token',
    'telegram_api_hash',
    'db_password',
    'click_merchant_id',
    'click_secret_key',
    'payme_merchant_id',
    'payme_secret_key',
    'uzum_merchant_id',
    'uzum_secret_key',
    'email_smtp_password',
    'django_secret_key',
    'fragment_api_key',
    'gemini_api_key',
    'user_client_session_b64',  # Telethon sessiyasi (cloud deploy uchun)
})

_fernet_lock = threading.Lock()
_fernet_instance = None
_FERNET_PREFIX = 'enc:'


def _get_fernet():
    """Lazily build a Fernet cipher keyed off the DJANGO_SECRET_KEY (or a
    dedicated SETTINGS_ENCRYPTION_KEY env var). The key itself is NEVER
    stored in the database, so a DB leak does not expose the secrets."""
    global _fernet_instance
    if _fernet_instance is not None:
        return _fernet_instance
    with _fernet_lock:
        if _fernet_instance is not None:
            return _fernet_instance
        try:
            from cryptography.fernet import Fernet
            raw = (os.getenv('SETTINGS_ENCRYPTION_KEY') or os.getenv('DJANGO_SECRET_KEY') or '').encode('utf-8')
            if not raw:
                raise RuntimeError('no key material')
            key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
            _fernet_instance = Fernet(key)
        except Exception:
            logger.exception('Fernet initialization failed — secrets will stay plaintext')
            _fernet_instance = False  # sentinel: don't retry on every read
        return _fernet_instance


def encrypt_setting_value(value: str) -> str:
    """Return 'enc:<ciphertext>' for a secret value, or the raw value if the
    cipher is unavailable (fail-safe: never break the app over encryption)."""
    if not value:
        return value
    f = _get_fernet()
    if not f:
        return value
    try:
        return _FERNET_PREFIX + f.encrypt(value.encode('utf-8')).decode('ascii')
    except Exception:
        logger.exception('Setting encryption failed')
        return value


def decrypt_setting_value(value) -> str:
    """Decrypt 'enc:...' values; return plaintext values untouched (backwards
    compatible with legacy rows written before encryption existed)."""
    if not value:
        return value
    if not str(value).startswith(_FERNET_PREFIX):
        return value
    f = _get_fernet()
    if not f:
        return value
    try:
        return f.decrypt(str(value)[len(_FERNET_PREFIX):].encode('ascii')).decode('utf-8')
    except Exception:
        # Corrupted / wrong key — return raw so the value is still visible
        # (better than hard-failing the whole site).
        logger.error(
            'Setting decryption FAILED — if DJANGO_SECRET_KEY was rotated, '
            'run backfill_settings_encryption.py / re-save the setting. '
            'Returning raw value.',
            exc_info=True,
        )
        return value


class Setting(models.Model):
    """Key-Value settings store for system configuration."""
    key = models.CharField(max_length=200, unique=True)
    value = models.TextField(blank=True, null=True)
    is_encrypted = models.BooleanField(default=False)
    description = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'settings'
        verbose_name = 'Setting'
        verbose_name_plural = 'Settings'

    def __str__(self):
        return self.key

    # ── In-process TTL cache ──
    # get_setting() is called on almost EVERY request (bot config, telegram
    # auth, payments, web app URL...). Without a cache each call is a DB hit.
    # A short TTL (3s) keeps reads instant while admin edits still propagate
    # almost immediately. set_setting() always invalidates the key.
    #
    # NOTE: None values ARE cached (via a sentinel) — a key whose stored
    # value is None (or absent) doesn't re-hit the DB on every call.
    _cache: dict = {}
    _cache_lock = threading.Lock()
    _CACHE_TTL = 3.0
    _MISSING = object()  # sentinel: distinguishes 'not in cache' from cached None

    @classmethod
    def _cache_get(cls, key):
        with cls._cache_lock:
            item = cls._cache.get(key)
            if item is None:
                return cls._MISSING
            if time.time() - item[0] < cls._CACHE_TTL:
                return item[1]
            cls._cache.pop(key, None)
            return cls._MISSING

    @classmethod
    def _cache_set(cls, key, value):
        with cls._cache_lock:
            cls._cache[key] = (time.time(), value)

    @classmethod
    def _cache_invalidate(cls, key):
        with cls._cache_lock:
            cls._cache.pop(key, None)

    @classmethod
    def clear_cache(cls):
        """Drop the whole in-process cache (used by tests and admin flush)."""
        with cls._cache_lock:
            cls._cache.clear()

    @classmethod
    def backfill_encryption(cls, keys=None):
        """Upgrade any legacy plaintext secret rows to encrypted at rest.

        Runs idempotently: already-encrypted rows are skipped. Called from a
        management command / deploy step.
        """
        from django.db.models import Q
        targets = keys or SECRET_SETTING_KEYS
        rows = cls.objects.filter(key__in=targets).filter(
            Q(is_encrypted=False) | Q(is_encrypted__isnull=True)
        )
        upgraded = 0
        for row in rows:
            if not row.value:
                continue
            new_val = encrypt_setting_value(row.value)
            if new_val != row.value:
                row.value = new_val
                row.is_encrypted = True
                row.save(update_fields=['value', 'is_encrypted'])
                cls._cache_invalidate(row.key)
                upgraded += 1
        return upgraded

    @classmethod
    def get_setting(cls, key, default=None):
        cached = cls._cache_get(key)
        if cached is not cls._MISSING:
            return cached
        try:
            obj = cls.objects.get(key=key)
            raw = obj.value
            if obj.is_encrypted:
                raw = decrypt_setting_value(raw)
        except cls.DoesNotExist:
            raw = default
        # Cache the DECRYPTED value (what every caller expects).
        cls._cache_set(key, raw)
        return raw

    @classmethod
    def set_setting(cls, key, value, description=''):
        store_value = str(value)
        is_encrypted = False
        # SECURITY: secrets are encrypted at rest. Keys that are NOT secret
        # keep the is_encrypted flag False so legacy plaintext rows stay
        # readable. If a secret was previously stored plaintext (legacy),
        # re-writing it here upgrades it to encrypted automatically.
        if key in SECRET_SETTING_KEYS and store_value:
            store_value = encrypt_setting_value(store_value)
            is_encrypted = True
        obj, created = cls.objects.update_or_create(
            key=key,
            defaults={'value': store_value, 'description': description, 'is_encrypted': is_encrypted}
        )
        cls._cache_invalidate(key)
        return obj


class SiteSetting:
    """Helper class to manage site settings as a dict."""
    DEFAULTS = {
        # Site
        'site_name': 'TOPUP HUB',
        'site_description': "O'yinlar va raqamli xizmatlarga tez va ishonchli donat",
        'support_telegram': '@topuphub',
        'currency': 'UZS',
        'maintenance_mode': 'False',

        # Telegram
        'telegram_bot_token': '',
        'telegram_bot_token_alt': '',
        'telegram_bot_username': '',
        'web_app_url': '',
        'super_admin_telegram_id': '',

        # User Client (Telethon) — https://my.telegram.org → API development tools
        'telegram_api_id': '',
        'telegram_api_hash': '',

        # Email
        'email_smtp_host': '',
        'email_smtp_port': '587',
        'email_smtp_user': '',
        'email_smtp_password': '',

        # Django server
        'django_secret_key': '',
        'debug': 'True',
        'allowed_hosts': 'localhost,127.0.0.1',

        # CORS
        'cors_allowed_origins': 'http://localhost:3000,http://localhost:8000',

        # Database
        'db_name': '',
        'db_user': '',
        'db_password': '',
        'db_host': '',
        'db_port': '',

        # Payment gateways (legacy - stored for reference)
        'click_merchant_id': '',
        'click_secret_key': '',
        'payme_merchant_id': '',
        'payme_secret_key': '',
        'uzum_merchant_id': '',
        'uzum_secret_key': '',

        # Fragment API (Telegram Stars & Premium auto-fulfillment, fragment-api.uz)
        'fragment_api_base_url': 'https://fragment-api.uz/api/v1',
        'fragment_api_key': '',

        # Fragment API live price sync (daily)
        'fragment_usd_uzs_rate': '12800',
        'fragment_price_margin_percent': '15',
        'fragment_price_sync_enabled': 'True',
        'fragment_last_price_sync': '',
        'fragment_last_sync_result': '',

        # ── Card payment auto-verification (Telethon user client) ──
        # payment_monitor_chat_id — bank-xabar chat/guruh; payment_report_chat_id
        # — hisobot guruh; payment_suspicious_limit — shu qiymatdan katta tushum
        # balansga avtomatik TUSHMADI (admin tasdiqlaydi); payment_timeout_minutes
        # — to'lov uchun beriladigan vaqt; payment_unique_offset_max — yagona
        # summani farqlash uchun qo'shiladigan offset; payment_card_number —
        # mijozga ko'rsatiladigan karta raqami.
        'payment_monitor_chat_id': '',
        'payment_report_chat_id': '',
        'payment_suspicious_limit': '500000',
        'payment_timeout_minutes': '10',
        'payment_unique_offset_max': '999',
        'payment_card_number': '',
        'payment_card_holder': '',
        'payment_card_monitor_enabled': 'False',

        # ── Security / Anti-Fraud (Gemini AI risk engine) ──
        'gemini_api_key': '',
        'gemini_model': 'gemini-3.1-flash-lite',
        'security_ai_enabled': 'False',
        'security_shadow_mode': 'True',
        'security_fail_open': 'False',
        'risk_low_max': '29',
        'risk_medium_max': '49',
        'risk_high_max': '69',
        'velocity_10m_limit': '200000',
        'velocity_1h_limit': '500000',
        'velocity_24h_limit': '1500000',
        'velocity_7d_limit': '5000000',
        'new_user_max_payment': '300000',
        'unique_amount_cooldown_min': '5',
        'emergency_telegram_id': '',
        'security_high_alerts_enabled': 'True',
        'security_critical_alerts_enabled': 'True',
        'security_ack_timeout_min': '2',
        'security_escalation_timeout_min': '5',
        'security_secondary_admin_id': '',
        'security_lockdown': 'False',
        'security_blacklist': '',
        'security_whitelist': '',

        # ── Marketing (boshqa guruhlarda reklama + selektiv javob) ──
        # marketing_group_enabled — bot guruhlarda marketing qiladimi;
        # marketing_ad_prob — javobga reklama qo'shilish ehtimoli (0.0-1.0);
        # marketing_rate_per_hour — har guruhda soatiga maks javob soni.
        'marketing_group_enabled': 'False',
        'marketing_ad_prob': '0.03',
        'marketing_rate_per_hour': '5',
        # marketing_daily_enabled — kunlik ertalabki suratli reklama yoq/o'chir;
        # marketing_daily_time — yuborish vaqti (HH:MM, Asia/Tashkent);
        # marketing_daily_image — surat URL (bo'sh bo'lsa faol Banner ishlatiladi).
        'marketing_daily_enabled': 'False',
        'marketing_daily_time': '09:00',
        'marketing_daily_image': '',
        # marketing_roast_enabled — guruh a'zolarini username bilan kinoyali
        # murojaat qilish (staff/hisobot/monitor guruhlari mustasno);
        # marketing_roast_interval_min — murojaatlar orasidagi interval (daqiqa).
        'marketing_roast_enabled': 'False',
        'marketing_roast_interval_min': '20',
    }

    @classmethod
    def get_all(cls):
        settings = {}
        for key, default in cls.DEFAULTS.items():
            settings[key] = Setting.get_setting(key, default)
        return settings

    @classmethod
    def update(cls, data):
        for key, value in data.items():
            if key in cls.DEFAULTS:
                Setting.set_setting(key, value)


class MarketingGroupStat(models.Model):
    """Marketing rejimi ishlagan har bir guruh bo'yicha yig'ma statistika.

    Bot guruhda qiziqarli xabarga javob berganida, reklama yuborganida va
    guruhga yangi qo'shilganida hisoblagichlar oshiriladi — admin panel
    "Marketing" bo'limida jonli ko'rinadi.
    """
    chat_id = models.CharField(max_length=64, unique=True)
    chat_title = models.CharField(max_length=255, blank=True, default='')
    replies_count = models.PositiveIntegerField(default=0)
    ads_count = models.PositiveIntegerField(default=0)
    joins_count = models.PositiveIntegerField(default=0)
    last_reply_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'marketing_group_stats'
        verbose_name = 'Marketing group stat'
        verbose_name_plural = 'Marketing group stats'

    @classmethod
    def record(cls, chat_id: str, chat_title: str = '', event: str = 'reply'):
        """Atomik hisoblagich oshirish — race xavfsiz (F() + update_or_create).

        event: 'reply' | 'ad' | 'join'
        Hech qachon xato tashlamaydi (bot oqimini buzmaslik uchun).
        """
        from django.db.models import F
        from django.utils import timezone
        try:
            row, _ = cls.objects.get_or_create(
                chat_id=str(chat_id),
                defaults={'chat_title': (chat_title or '')[:255]},
            )
            updates = {}
            if event == 'reply':
                updates['replies_count'] = F('replies_count') + 1
                updates['last_reply_at'] = timezone.now()
            elif event == 'ad':
                updates['ads_count'] = F('ads_count') + 1
            elif event == 'join':
                updates['joins_count'] = F('joins_count') + 1
            if (chat_title or '').strip():
                updates['chat_title'] = (chat_title or '')[:255]
            if updates:
                cls.objects.filter(pk=row.pk).update(**updates)
            MarketingDailyStat.record(event)
        except Exception:
            logger.exception('MarketingGroupStat.record failed')


class MarketingDailyStat(models.Model):
    """Kunlik marketing faolligi (14 kunlik grafik uchun)."""
    day = models.DateField(unique=True)
    replies_count = models.PositiveIntegerField(default=0)
    ads_count = models.PositiveIntegerField(default=0)
    joins_count = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'marketing_daily_stats'
        verbose_name = 'Marketing daily stat'
        verbose_name_plural = 'Marketing daily stats'

    @classmethod
    def record(cls, event: str):
        from django.db.models import F
        from django.utils import timezone
        try:
            day = timezone.localdate()
            row, _ = cls.objects.get_or_create(day=day)
            field = {'reply': 'replies_count', 'ad': 'ads_count', 'join': 'joins_count'}.get(event)
            if field:
                cls.objects.filter(pk=row.pk).update(**{field: F(field) + 1})
        except Exception:
            logger.exception('MarketingDailyStat.record failed')


class MarketingGroupMember(models.Model):
    """Marketing guruhlarida ko'rilgan a'zolar (username bo'yicha) — DB'da.

    Bot qayta ishga tushganda ham a'zolarni eslab qoladi: kim qaysi guruhda
    ko'rilgan, oxirgi marta qachon yozgan va qachon masxara qilingan.
    Roast loopi shu jadvaldan o'qiydi — xotira emas, DB manba hisoblanadi.
    """
    chat_id = models.CharField(max_length=64, db_index=True)
    username = models.CharField(max_length=64)  # kichik harflar bilan
    first_name = models.CharField(max_length=255, blank=True, default='')
    user_id = models.BigIntegerField(null=True, blank=True)
    last_seen_at = models.DateTimeField(db_index=True)
    last_roast_at = models.DateTimeField(null=True, blank=True)
    roast_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'marketing_group_members'
        verbose_name = 'Marketing group member'
        verbose_name_plural = 'Marketing group members'
        constraints = [
            models.UniqueConstraint(fields=['chat_id', 'username'], name='uniq_member_chat_username'),
        ]

    @classmethod
    def record_member(cls, chat_id: str, username: str, first_name: str = '',
                      user_id=None, seen_at=None):
        """A'zoni eslab qoladi (upsert). Hech qachon xato tashlamaydi."""
        from django.utils import timezone
        try:
            uname = (username or '').strip().lower()
            if not chat_id or not uname:
                return None
            seen = seen_at or timezone.now()
            row, _ = cls.objects.update_or_create(
                chat_id=str(chat_id),
                username=uname,
                defaults={
                    'first_name': (first_name or '')[:255],
                    'user_id': user_id,
                    'last_seen_at': seen,
                },
            )
            return row
        except Exception:
            logger.exception('MarketingGroupMember.record_member failed')
            return None

    @classmethod
    def mark_roasted(cls, chat_id: str, username: str, when=None):
        """Masxara qilinganini belgilaydi (takrorlanmasligi uchun)."""
        from django.db.models import F
        from django.utils import timezone
        try:
            when = when or timezone.now()
            cls.objects.filter(chat_id=str(chat_id), username=(username or '').lower())\
                .update(last_roast_at=when, roast_count=F('roast_count') + 1)
        except Exception:
            logger.exception('MarketingGroupMember.mark_roasted failed')

    @classmethod
    def prune(cls, days: int = 7):
        """Bir haftadan ko'p harakatsiz a'zolarni tozalaydi."""
        from django.utils import timezone
        from datetime import timedelta
        try:
            cls.objects.filter(last_seen_at__lt=timezone.now() - timedelta(days=days)).delete()
        except Exception:
            logger.exception('MarketingGroupMember.prune failed')
