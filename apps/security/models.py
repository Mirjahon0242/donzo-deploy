"""
DONZO Security / Anti-Fraud models.

Core principle: AI observes & explains — the backend enforces — humans
control irreversible decisions. These models store the *evidence trail*
for every risk decision so the admin panel can always answer "why?".

Models:
  • PaymentRiskAssessment — one per analyzed payment (risk score + decision)
  • RiskEvent             — one deterministic rule trigger (explainability)
  • SecurityIncident      — HIGH/CRITICAL event requiring human attention
  • SecurityCase          — grouped investigation for related payments
  • UserRiskProfile       — per-user lifetime risk snapshot
  • SecurityAlert         — alert log + acknowledgement tracking

SECURITY:
  • Sensitive fields (usernames inside AI payloads, raw text) are NEVER
    stored. Only masked/summarized values are persisted.
  • Gemini never stores raw initData, card numbers or tokens.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

# ── Risk levels (stable vocabulary used across the system) ──
LOW = 'LOW'
MEDIUM = 'MEDIUM'
HIGH = 'HIGH'
CRITICAL = 'CRITICAL'
RISK_LEVELS = [
    (LOW, 'Past'),
    (MEDIUM, "O'rta"),
    (HIGH, 'Yuqori'),
    (CRITICAL, 'Kritik'),
]

# ── Final decisions / statuses ──
APPROVED = 'APPROVED'
HOLD = 'HOLD'
MANUAL_REVIEW = 'MANUAL_REVIEW'
BLOCKED = 'BLOCKED'
REJECTED = 'REJECTED'
ANALYZING = 'ANALYZING'
AI_UNAVAILABLE = 'AI_UNAVAILABLE'

DECISION_CHOICES = [
    (APPROVED, 'Tasdiqlandi'),
    (HOLD, 'Ushlab turildi'),
    (MANUAL_REVIEW, 'Qo\'lda tekshirish'),
    (BLOCKED, 'Bloklandi'),
    (REJECTED, 'Rad etildi'),
    (ANALYZING, 'Tahlil qilinmoqda'),
    (AI_UNAVAILABLE, 'AI mavjud emas'),
]

INCIDENT_STATUS = [
    ('OPEN', 'Ochiq'),
    ('ACKED', 'Qabul qilingan'),
    ('INVESTIGATING', 'Tekshirilmoqda'),
    ('RESOLVED', 'Hal qilindi'),
    ('FALSE_POSITIVE', 'Yolg\'on signal'),
    ('CONFIRMED_FRAUD', 'Firibgarlik tasdiqlandi'),
]

CASE_STATUS = [
    ('OPEN', 'Ochiq'),
    ('INVESTIGATING', 'Tekshirilmoqda'),
    ('WAITING', 'Kutilmoqda'),
    ('CLEARED', 'Tozalangan'),
    ('CONFIRMED_FRAUD', 'Firibgarlik tasdiqlandi'),
    ('CLOSED', 'Yopilgan'),
]

ALERT_STATUS = [
    ('SENT', 'Yuborilgan'),
    ('ESCALATED', 'Kuchaytirilgan'),
    ('ACKED', 'Qabul qilingan'),
]


class PaymentRiskAssessment(models.Model):
    """Risk evaluation of ONE payment (card top-up)."""

    payment_message = models.ForeignKey(
        'cardpay.CardPaymentMessage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='risk_assessments',
    )
    topup_request = models.ForeignKey(
        'cardpay.CardTopupRequest', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='risk_assessments',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        null=True, blank=True, related_name='risk_assessments',
    )

    # Amounts (masked log policy: amounts are not PII, safe to store)
    requested_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    received_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)

    # Scores
    rule_score = models.PositiveSmallIntegerField(default=0)
    ai_score = models.PositiveSmallIntegerField(null=True, blank=True)
    final_score = models.PositiveSmallIntegerField(default=0)
    risk_level = models.CharField(max_length=10, choices=RISK_LEVELS, default=LOW)
    ai_confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    ai_available = models.BooleanField(default=False)
    ai_error = models.CharField(max_length=200, blank=True, default='')
    shadow_mode = models.BooleanField(default=False)
    ai_summary = models.TextField(blank=True, default='')

    # Explainability: "+20 New account" lines
    reasons = models.JSONField(default=list, blank=True)
    detected_patterns = models.JSONField(default=list, blank=True)
    rule_triggers = models.JSONField(default=list, blank=True)

    decision = models.CharField(max_length=20, choices=DECISION_CHOICES, default=ANALYZING)
    # What would the decision be if AI affected the outcome (shadow mode)
    shadow_decision = models.CharField(max_length=20, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_risk_assessments'
        ordering = ['-created_at']

    def __str__(self):
        return f"Risk {self.final_score}/{self.risk_level} → {self.decision}"


class RiskEvent(models.Model):
    """One deterministic rule trigger — the "why" behind the score."""

    assessment = models.ForeignKey(
        PaymentRiskAssessment, on_delete=models.CASCADE,
        related_name='risk_events',
    )
    rule_name = models.CharField(max_length=100)
    points = models.SmallIntegerField(default=0)
    severity = models.CharField(max_length=10, choices=RISK_LEVELS, default=LOW)
    description = models.TextField(blank=True, default='')
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'risk_events'
        ordering = ['-points']

    def __str__(self):
        return f"{self.rule_name} (+{self.points})"


class SecurityIncident(models.Model):
    """A HIGH/CRITICAL event that requires human attention."""

    SEVERITY_CHOICES = [(LOW, 'Past'), (MEDIUM, "O'rta"), (HIGH, 'Yuqori'), (CRITICAL, 'Kritik')]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='security_incidents',
    )
    assessment = models.ForeignKey(
        PaymentRiskAssessment, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='incidents',
    )
    topup_request = models.ForeignKey(
        'cardpay.CardTopupRequest', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='security_incidents',
    )

    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default=HIGH)
    risk_score = models.PositiveSmallIntegerField(default=0)
    status = models.CharField(max_length=20, choices=INCIDENT_STATUS, default='OPEN')
    escalation_level = models.PositiveSmallIntegerField(default=0)
    acked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='acked_incidents',
    )
    acked_at = models.DateTimeField(null=True, blank=True)

    # Evidence (masked summaries only — never raw card data / tokens)
    payment_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    rule_triggers = models.JSONField(default=list, blank=True)
    ai_summary = models.TextField(blank=True, default='')
    reasons = models.JSONField(default=list, blank=True)
    timeline = models.JSONField(default=list, blank=True)  # [{ts, action, note}]
    related_game_ids = models.JSONField(default=list, blank=True)

    resolution_note = models.TextField(blank=True, default='')
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='resolved_incidents',
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'security_incidents'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status', 'severity'])]

    def __str__(self):
        return f"#{self.id} {self.severity} {self.risk_score} ({self.status})"

    def add_timeline(self, action: str, note: str = ''):
        from django.utils import timezone
        self.timeline = list(self.timeline or []) + [{
            'ts': timezone.now().isoformat(),
            'action': action,
            'note': note,
        }]
        # FULL save (NOT update_fields=['timeline', ...]): callers routinely
        # set status / resolved_by / resolved_at / escalation_level on this
        # object right before add_timeline, and a partial save silently
        # DROPS those changes (e.g. resolve_incident kept incidents OPEN in
        # the DB while the UI showed RESOLVED, re-escalating forever).
        self.save()


class SecurityCase(models.Model):
    """Grouped investigation for related payments/users."""

    case_id = models.CharField(max_length=30, unique=True, blank=True)
    status = models.CharField(max_length=20, choices=CASE_STATUS, default='OPEN')
    severity = models.CharField(max_length=10, choices=RISK_LEVELS, default=MEDIUM)
    assigned_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='security_cases',
    )
    users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='security_cases_users')
    incidents = models.ManyToManyField(SecurityIncident, blank=True, related_name='cases')
    evidence = models.JSONField(default=list, blank=True)
    ai_summary = models.TextField(blank=True, default='')
    admin_notes = models.TextField(blank=True, default='')
    resolution = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'security_cases'
        ordering = ['-created_at']

    def __str__(self):
        return f"Case {self.case_id or self.id} ({self.status})"

    def save(self, *args, **kwargs):
        if not self.case_id:
            from django.utils.crypto import get_random_string
            self.case_id = f"CSE-{timezone.now().strftime('%y%m%d')}-{get_random_string(5).upper()}"
        super().save(*args, **kwargs)


class UserRiskProfile(models.Model):
    """Per-user lifetime risk snapshot (updated on each evaluation)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='risk_profile',
    )
    risk_score = models.PositiveSmallIntegerField(default=0)
    risk_level = models.CharField(max_length=10, choices=RISK_LEVELS, default=LOW)

    # Flags controlled by admins
    TRUSTED = 'trusted'
    WATCH = 'watch'
    BLOCKED = 'blocked'
    NORMAL = 'normal'
    FLAG_CHOICES = [
        (TRUSTED, 'Ishonchli'),
        (WATCH, 'Kuzatuvda'),
        (BLOCKED, 'Bloklangan'),
        (NORMAL, 'Oddiy'),
    ]
    admin_flag = models.CharField(max_length=10, choices=FLAG_CHOICES, default=NORMAL)

    # Aggregates (updated by the risk engine)
    lifetime_volume = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    volume_24h = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    volume_7d = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    payment_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    suspicious_count = models.PositiveIntegerField(default=0)
    hold_count = models.PositiveIntegerField(default=0)
    game_ids = models.JSONField(default=list, blank=True)
    last_evaluated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'user_risk_profiles'

    def __str__(self):
        return f"{self.user.username}: {self.risk_level} ({self.risk_score})"


class SecurityAlert(models.Model):
    """Sent alert + acknowledgement tracking (escalation trail)."""

    incident = models.ForeignKey(
        SecurityIncident, on_delete=models.CASCADE,
        null=True, blank=True, related_name='alerts',
    )
    alert_type = models.CharField(max_length=20, choices=[(HIGH, HIGH), (CRITICAL, CRITICAL)], default=HIGH)
    severity = models.CharField(max_length=10, choices=RISK_LEVELS, default=HIGH)
    recipient = models.CharField(max_length=200, blank=True, default='')  # chat id / 'group'
    message_text = models.TextField(blank=True, default='')
    status = models.CharField(max_length=20, choices=ALERT_STATUS, default='SENT')
    escalation_level = models.PositiveSmallIntegerField(default=0)
    acked_by = models.CharField(max_length=100, blank=True, default='')
    acked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'security_alerts'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.alert_type} alert → {self.recipient} ({self.status})"
