from django.db import models
from django.utils import timezone


class OrderStatus(models.TextChoices):
    PENDING = 'pending', 'Kutilmoqda'
    PROCESSING = 'processing', 'Bajarilmoqda'
    COMPLETED = 'completed', 'Tugallangan'
    CANCELLED = 'cancelled', 'Bekor qilingan'


class PaymentStatus(models.TextChoices):
    UNPAID = 'unpaid', "To'lanmagan"
    PAID = 'paid', "To'langan"
    REFUNDED = 'refunded', 'Qaytarilgan'


class Order(models.Model):
    order_number = models.CharField(max_length=50, unique=True, editable=False)
    customer = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='orders'
    )
    service = models.ForeignKey(
        'services.Service', on_delete=models.PROTECT,
        related_name='orders'
    )
    package = models.ForeignKey(
        'services.Package', on_delete=models.PROTECT,
        related_name='orders'
    )
    field_values = models.JSONField(default=dict, blank=True)
    customer_name = models.CharField(max_length=200)
    customer_telegram = models.CharField(max_length=200)

    status = models.CharField(
        max_length=20,
        choices=OrderStatus.choices,
        default=OrderStatus.PENDING,
    )
    cancel_reason = models.TextField(blank=True, null=True, default=None)
    assigned_operator = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='assigned_orders'
    )
    total_price = models.DecimalField(max_digits=15, decimal_places=2)

    payment_status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
    )
    payment_method = models.CharField(max_length=50, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'orders'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.order_number:
            # Generate unique order number
            import uuid
            self.order_number = f"TH{uuid.uuid4().hex[:8].upper()}"
        if not self.total_price and self.package:
            self.total_price = self.package.price
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.order_number} - {self.service.name}"

    @property
    def service_name(self):
        return self.service.name if self.service else ''

    @property
    def package_name(self):
        return self.package.name if self.package else ''
