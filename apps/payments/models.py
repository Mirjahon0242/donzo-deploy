from django.db import models


class Payment(models.Model):
    PROVIDERS = [
        ('balance', 'Balans'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Kutilmoqda'),
        ('success', 'Muvaffaqiyatli'),
        ('failed', 'Xatolik'),
    ]

    order = models.ForeignKey(
        'orders.Order', on_delete=models.CASCADE,
        null=True, blank=True,
        related_name='payments'
    )
    provider = models.CharField(max_length=50, choices=PROVIDERS)
    transaction_id = models.CharField(max_length=200, blank=True, null=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    raw_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payments'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.provider} - {self.transaction_id} - {self.status}"


class BalanceTransaction(models.Model):
    """Track all balance operations — top-ups, purchases, admin adjustments."""

    TX_TYPES = [
        ('topup', "To'ldirish"),
        ('purchase', "Xarid"),
        ('cashback', 'Cashback'),
        ('cashback_claim', 'Cashback balansga o\'tkazildi'),
        ('referral_gift', 'Referal sovg\'a'),
        ('admin', 'Admin tuzatmasi'),
        ('refund', 'Qaytarildi'),
    ]

    STATUS_CHOICES = [
        ('pending', 'Kutilmoqda'),
        ('completed', 'Tugallangan'),
        ('failed', 'Xatolik'),
        ('cancelled', 'Bekor qilingan'),
    ]

    user = models.ForeignKey(
        'users.User', on_delete=models.CASCADE,
        related_name='balance_transactions'
    )
    # Client-generated unique key so a double-submit / retry of the same
    # top-up can never credit the balance twice (idempotency guard).
    idempotency_key = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    tx_type = models.CharField(max_length=20, choices=TX_TYPES)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    balance_before = models.DecimalField(max_digits=15, decimal_places=2)
    balance_after = models.DecimalField(max_digits=15, decimal_places=2)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Payment reference for top-ups via external providers
    provider = models.CharField(max_length=50, blank=True, null=True)
    provider_transaction_id = models.CharField(max_length=200, blank=True, null=True)

    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'balance_transactions'
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'idempotency_key'],
                name='uniq_user_idemkey',
                condition=models.Q(idempotency_key__isnull=False),
            )
        ]

    def __str__(self):
        return f"{self.user.username} - {self.tx_type} - {self.amount} so'm"
