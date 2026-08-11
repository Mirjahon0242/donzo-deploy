"""
Card payment auto-verification models (DONZO).

The platform watches a Telegram chat (usually a bank-card notification
chat/group) where incoming-transfer messages are posted. Every balance
top-up request gets a UNIQUE amount (requested + small random offset,
e.g. 5 000 → "send exactly 5 001"). The user client matches the received
message amount against active pending requests and credits the balance
atomically.

Models:
  • CardTopupRequest  — one user's pending top-up with its unique amount
  • CardPaymentMessage— one bank notification message (dedup guard)
  • SuspiciousPayment — transfers above the suspicious limit, awaiting
                        admin approve/reject before the balance is credited
  • PaymentCard       — one operator bank card with per-card limits
                        (max amount / max transfers). When a card hits its
                        limit the platform auto-rotates to the next card.

SECURITY:
  • A message can be consumed only ONCE (unique chat_id+message_id).
  • Balance is credited inside a single transaction with row locks.
  • Amounts above the suspicious limit are NEVER auto-credited.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.payments.models import BalanceTransaction



class CardTopupRequest(models.Model):
    """A pending balance top-up that waits for a real card transfer."""

    STATUS_CHOICES = [
        ('pending', 'Kutilmoqda'),
        ('paid', "To'landi"),
        ('cancelled', 'Bekor qilingan'),
        ('expired', 'Muddati o\'tgan'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='card_topup_requests',
    )
    # The pending BalanceTransaction created by the classic top-up flow.
    # Crediting this transaction is what actually moves the balance.
    balance_tx = models.ForeignKey(
        BalanceTransaction, on_delete=models.CASCADE,
        null=True, blank=True, related_name='card_requests',
    )
    requested_amount = models.DecimalField(max_digits=15, decimal_places=2)
    # The EXACT amount the user must send (requested + 0..offset_max).
    unique_amount = models.DecimalField(max_digits=15, decimal_places=2, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)

    # Set when the transfer was matched (paid) or the request was decided.
    paid_at = models.DateTimeField(null=True, blank=True)
    matched_message = models.ForeignKey(
        'CardPaymentMessage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='matched_requests',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'card_topup_requests'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'unique_amount']),
            models.Index(fields=['status', 'expires_at']),
        ]

    def __str__(self):
        return f"#{self.id} {self.user.username} → {self.unique_amount} ({self.status})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    def mark_expired(self, save: bool = True):
        """Cancel an unpaid request whose window elapsed (10 min default)."""
        self.status = 'expired'
        if save:
            self.save(update_fields=['status', 'updated_at'])


class PaymentCard(models.Model):
    """One operator bank card users are told to send money to.

    Limits (0 = unlimited):
      • max_amount    — stop using this card after this much money arrived
      • max_transfers — stop using this card after this many transfers
    Counters (total_amount / transfers_count) accumulate per period. With
    auto_reset_daily the counters restart every midnight. When a limit is
    reached the system automatically activates the next enabled card.
    """

    card_number = models.CharField(max_length=50, unique=True)
    card_holder = models.CharField(max_length=120, blank=True, default='')
    bank_name = models.CharField(max_length=120, blank=True, default='')
    enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(
        default=False, help_text="Ayni paytda mijozlarga ko'rsatiladigan karta"
    )

    max_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        help_text="Ushbu kartaga kelishi mumkin bo'lgan maksimal summa (0 = cheksiz)",
    )
    max_transfers = models.PositiveIntegerField(
        default=0, help_text="Maksimal o'tkazmalar soni (0 = cheksiz)"
    )

    total_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        help_text="Joriy davrda kelgan summa",
    )
    transfers_count = models.PositiveIntegerField(default=0)
    auto_reset_daily = models.BooleanField(
        default=True, help_text="Har kuni yarim tunda hisoblagichlarni tiklash"
    )
    period_started_at = models.DateTimeField(default=timezone.now)
    last_switch_at = models.DateTimeField(null=True, blank=True)
    order_index = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_cards'
        ordering = ['order_index', 'id']

    def __str__(self):
        tail = self.card_number[-4:] if len(self.card_number) >= 4 else self.card_number
        return f"***{tail} ({self.card_holder or '—'})"

    @property
    def card_tail(self) -> str:
        return self.card_number[-4:] if len(self.card_number) >= 4 else ''

    @property
    def is_exhausted(self) -> bool:
        """True when this card reached its amount or transfer limit."""
        if self.max_amount and self.total_amount >= self.max_amount:
            return True
        if self.max_transfers and self.transfers_count >= self.max_transfers:
            return True
        return False

    def save(self, *args, **kwargs):
        # Only one card can be active at a time.
        if self.is_active:
            PaymentCard.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)


class CardPaymentMessage(models.Model):
    """One bank-notification message received in the monitored chat.

    The unique (chat_id, message_id) constraint is the idempotency guard:
    a restart or duplicate delivery can never credit the same transfer
    twice.
    """

    chat_id = models.CharField(max_length=100, db_index=True)
    message_id = models.BigIntegerField()
    raw_text = models.TextField(blank=True, default='')
    # Amount parsed from the message (candidate list joined by ',' — the
    # matcher accepts any candidate equal to an active unique amount).
    parsed_amounts = models.CharField(max_length=200, blank=True, default='')
    is_outgoing = models.BooleanField(default=False, help_text="Chiqim/списание xabari")
    sender_id = models.CharField(max_length=100, blank=True, null=True)
    received_at = models.DateTimeField(default=timezone.now)

    # Consumption outcome (for admin visibility)
    outcome = models.CharField(
        max_length=30, blank=True, default='',
        help_text="matched / no_match / suspicious / ignored",
    )

    class Meta:
        db_table = 'card_payment_messages'
        ordering = ['-received_at']
        constraints = [
            models.UniqueConstraint(
                fields=['chat_id', 'message_id'],
                name='uniq_card_msg_chat_message',
            ),
        ]

    def __str__(self):
        return f"[{self.chat_id}:{self.message_id}] {self.parsed_amounts}"


class SuspiciousPayment(models.Model):
    """A transfer above the suspicious limit, held for manual review.

    Until an admin approves it, the money is NEVER credited to any balance.
    Approve → the balance is credited; Reject → it stays ignored.
    """

    STATUS_CHOICES = [
        ('pending', 'Kutilmoqda'),
        ('approved', 'Tasdiqlangan'),
        ('rejected', 'Rad etilgan'),
    ]

    message = models.ForeignKey(
        CardPaymentMessage, on_delete=models.CASCADE,
        null=True, blank=True, related_name='suspicious_payments',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suspicious_payments',
    )
    # The unique_amount (requested + offset) that triggered the suspicion.
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    # Optional: which pending request this suspicious transfer belonged to.
    topup_request = models.ForeignKey(
        CardTopupRequest, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='suspicious_payments',
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='decided_suspicious',
    )

    class Meta:
        db_table = 'suspicious_payments'
        ordering = ['-created_at']

    def __str__(self):
        return f"Suspicious {self.amount} so'm ({self.status})"


def parse_amounts_from_text(text: str) -> list:
    """Extract candidate UZS amounts from a bank-notification message.

    Conservative + tolerant: returns EVERY plausible amount (≥1 000,
    ≤ 100 000 000), ignoring card numbers, dates, phone numbers. The
    matcher only credits when a candidate equals an ACTIVE unique_amount,
    so over-parsing is harmless (never credits wrongly) while maximizing
    recall across different bank SMS formats.

    Outgoing lines (chiqim / списание / "-") are flagged separately.
    """
    import re

    text = (text or '').replace('\u00a0', ' ')
    candidates = []

    # 1) Numbers with thousand separators (space, dot, comma), optional
    #    decimals: "5 001.00", "1,250,000", "5000"
    #    Regex matches whole digit groups with separators between digits.
    for m in re.finditer(r'\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{1,2})?', text):
        raw = m.group(0)
        # Strip a trailing decimal part (".00", ",50") BEFORE removing
        # separators — "1 061.00" must become 1061, never 106100.
        cleaned = re.sub(r'[.,]\d{1,2}$', '', raw)
        cleaned = re.sub(r'[^\d]', '', cleaned)
        try:
            val = int(cleaned)
        except ValueError:
            continue
        if 1000 <= val <= 100_000_000 and val not in candidates:
            candidates.append(val)

    # 2) Bare numbers ≥ 1000 (no separators), e.g. "+5001".
    #    Skip card masks like "***2917" (a digit right after '*' is a
    #    card suffix, never a payment amount).
    for m in re.finditer(r'(?<![*\d])(\d{4,9})(?!\d)', text):
        val = int(m.group(1))
        if 1000 <= val <= 100_000_000 and val not in candidates:
            candidates.append(val)

    # 3) Noise filter: drop years (dates) — they are never payment amounts.
    candidates = [c for c in candidates if not (1900 <= c <= 2100)]

    candidates.sort()
    return candidates
