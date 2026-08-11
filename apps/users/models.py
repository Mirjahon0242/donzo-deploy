from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    GUEST = 'guest', 'Guest'
    CUSTOMER = 'customer', 'Customer'
    SUPPORT = 'support', 'Support'
    OPERATOR = 'operator', 'Operator'
    SENIOR_OPERATOR = 'senior_operator', 'Senior Operator'
    ADMIN = 'admin', 'Admin'
    SUPER_ADMIN = 'super_admin', 'Super Admin'


class User(AbstractUser):
    """
    Custom User model with role-based access control.
    """
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    telegram_id = models.CharField(max_length=100, blank=True, null=True, unique=True)
    telegram_username = models.CharField(max_length=100, blank=True, null=True)
    # Telegram identity extras (captured automatically on WebApp login)
    language_code = models.CharField(max_length=10, blank=True, null=True)
    avatar_url = models.CharField(max_length=2000, blank=True, null=True)

    # Enriched from Fragment API (getInfo) after login — live profile data:
    # Telegram Premium status and the last time getInfo was successfully pulled.
    is_telegram_premium = models.BooleanField(default=False)
    fragment_synced_at = models.DateTimeField(null=True, blank=True)

    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.CUSTOMER,
    )

    is_active = models.BooleanField(default=True)
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    cashback_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    referral_code = models.CharField(max_length=50, unique=True, blank=True, null=True)
    referred_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='referrals'
    )

    is_blacklisted = models.BooleanField(default=False)
    notes = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ── Anti-fraud: qurilma / joylashuv metadata (har kirishda yangilanadi) ──
    # Faqat foydalanuvchining o'zi yuborgan/backend aniqlagan metadata — hech
    # qanday maxfiy token emas. Admin panelda ko'rinadi (firibgarlik tahlili).
    last_ip = models.CharField(max_length=45, blank=True, null=True)
    last_ip_location = models.CharField(max_length=200, blank=True, null=True)  # IP bo'yicha: "Toshkent, UZ · ISP"
    last_location = models.CharField(max_length=200, blank=True, null=True)  # TO'LIQ manzil (GPS reverse-geocode): ko'cha, tuman, shahar
    last_user_agent = models.CharField(max_length=500, blank=True, null=True)
    last_platform = models.CharField(max_length=100, blank=True, null=True)  # Android/iOS/WebApp
    last_language = models.CharField(max_length=10, blank=True, null=True)
    last_timezone = models.CharField(max_length=60, blank=True, null=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    # Brauzer geolokatsiyasi (foydalanuvchi ruxsat bersa) — aniq koordinatalar
    geo_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geo_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Koordinata manbasi: 'gps' = qurilmadan aniq (asosiy), 'ip' = IP taxminiy
    # (fallback — GPS ruxsati berilmaganda). Admin panel shunga qarab
    # "GPS (aniq)" yoki "IP (taxminiy)" ko'rsatadi.
    geo_source = models.CharField(max_length=10, blank=True, null=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        db_table = 'users'
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

    def has_permission(self, required_roles):
        """Check if user has required role level."""
        role_hierarchy = {
            Role.GUEST: 0,
            Role.CUSTOMER: 1,
            Role.SUPPORT: 2,
            Role.OPERATOR: 3,
            Role.SENIOR_OPERATOR: 4,
            Role.ADMIN: 5,
            Role.SUPER_ADMIN: 6,
        }
        user_level = role_hierarchy.get(self.role, 0)
        if isinstance(required_roles, list):
            return any(
                role_hierarchy.get(r, 0) <= user_level
                for r in required_roles
            )
        return role_hierarchy.get(required_roles, 0) <= user_level


class TelegramLoginCode(models.Model):
    """
    One-time login code issued by the bot (e.g. /login).

    Lets a user authenticate from ANY browser (not only inside Telegram):
    the bot sends a short-lived 6-digit code to their chat, the user types
    it into the web app, and the backend binds it to the telegram_id.

    SECURITY: codes are random, single-use, expire after 5 minutes and are
    tied to exactly one telegram_id. Only a SHA-256 hash of the code is
    stored — the plaintext is never persisted, so a database leak can never
    be used to log in. Never stores the bot token, session data or initData.
    """
    # SHA-256 hex digest of the 6-digit code (never the code itself).
    code = models.CharField(max_length=64, db_index=True)
    telegram_id = models.CharField(max_length=100, db_index=True)
    telegram_username = models.CharField(max_length=100, blank=True, null=True)
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    language_code = models.CharField(max_length=10, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)

    class Meta:
        db_table = 'telegram_login_codes'
        verbose_name = 'Telegram Login Code'
        verbose_name_plural = 'Telegram Login Codes'
        ordering = ['-created_at']

    def __str__(self):
        # Never print the stored value in admin — it is a hash, but hiding it
        # entirely avoids any confusion (and the plaintext is never around).
        return f"Login code (hashed) for tg:{self.telegram_id}"


class TelegramWebAppSession(models.Model):
    """
    One record per Telegram Web App open that passed HMAC signature
    verification (or a rejected attempt carrying only an error_code).

    SECURITY: stores ONLY metadata — user, telegram_id, timestamps,
    launch source, (optional) user-agent and client IP. NEVER raw
    initData, the hash, or the bot token.
    """
    user = models.ForeignKey(
        User, on_delete=models.CASCADE,
        null=True, blank=True, related_name='webapp_sessions',
    )
    telegram_id = models.CharField(max_length=100, blank=True, null=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    is_authenticated = models.BooleanField(default=True)
    launch_source = models.CharField(max_length=50, default='telegram_webapp')
    user_agent = models.CharField(max_length=500, blank=True, null=True)
    # Client IP for audit/forensics only — never used for auth decisions.
    # Length 45 covers the longest IPv6 representation.
    ip_address = models.CharField(max_length=45, blank=True, null=True)
    # IP bo'yicha aniqlangan joylashuv ("Toshkent, UZ") — sessiya ochilganda
    # bitta bepul geolokatsiya so'rovi, IP bo'yicha 24h kesh.
    location = models.CharField(max_length=200, blank=True, null=True)
    error_code = models.CharField(max_length=50, blank=True, null=True)
    # Masked structural diagnostics for FAILED attempts only — which initData
    # keys were present, hash length, auth_date skew, how many bot tokens were
    # tried. NEVER stores raw initData, the hash, or tokens — only facts that
    # let an admin tell a wrong-bot open apart from a forgery/replay.
    diag = models.CharField(max_length=300, blank=True, null=True)

    class Meta:
        db_table = 'telegram_webapp_sessions'
        verbose_name = 'Telegram WebApp Session'
        verbose_name_plural = 'Telegram WebApp Sessions'
        ordering = ['-opened_at']

    def __str__(self):
        return f"TG WebApp {self.telegram_id or '?'} @ {self.opened_at}"

    @classmethod
    def prune_old(cls, keep_days=30):
        """Delete session rows older than `keep_days` (unbounded growth guard).

        Every Telegram login writes a row; without pruning the table grows
        forever. Runs cheaply with an index on opened_at and is called from
        the auth views after insert.
        """
        from django.utils import timezone
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=keep_days)
        return cls.objects.filter(opened_at__lt=cutoff).delete()[0]


class ReferralReward(models.Model):
    """Referral milestone gift — e.g. 30 friends → 1 month Telegram Premium.

    One row per granted milestone, so a reward is NEVER given twice for the
    same milestone even if the check runs again (idempotent by construction:
    we only grant milestone = 30 * (count(granted) + 1)).
    """

    REWARD_LABEL_1M_PREMIUM = 'Telegram Premium 1 oy'
    REWARD_AMOUNT_1M_PREMIUM = Decimal('45000')  # = '1 oy Premium' paket narxi
    MILESTONE_EVERY = 30  # har 30 ta do'st uchun bir sovg'a

    STATUS_CHOICES = [
        ('granted', 'Berildi'),
        ('failed', 'Muvaffaqiyatsiz'),
    ]

    referrer = models.ForeignKey(
        'users.User', on_delete=models.CASCADE, related_name='referral_rewards'
    )
    milestone = models.PositiveIntegerField()
    reward_label = models.CharField(max_length=100, default=REWARD_LABEL_1M_PREMIUM)
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=REWARD_AMOUNT_1M_PREMIUM)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='granted')
    note = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'referral_rewards'
        ordering = ['-created_at']
        unique_together = [('referrer', 'milestone')]

    def __str__(self):
        return f"@{self.referrer.username} milestone {self.milestone} -> {self.reward_label}"
