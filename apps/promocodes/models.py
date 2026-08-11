import uuid
from decimal import Decimal
from django.db import models
from django.utils import timezone


class PromoCodeType(models.TextChoices):
    PERCENTAGE = 'percentage', 'Foiz (%)'
    FIXED = 'fixed', 'Maxsus miqdor (so\'m)'


class PromoCode(models.Model):
    """Promo code for discounts on orders."""

    code = models.CharField(max_length=50, unique=True, db_index=True)
    description = models.TextField(blank=True, null=True)

    discount_type = models.CharField(
        max_length=20,
        choices=PromoCodeType.choices,
        default=PromoCodeType.PERCENTAGE,
    )
    discount_value = models.DecimalField(max_digits=10, decimal_places=2)
    max_discount_amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True,
        help_text="Maksimal chegirma miqdori (foizli chegirma uchun)",
    )
    min_order_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0,
        help_text="Minimal buyurtma summasi",
    )

    max_uses = models.IntegerField(default=0, help_text="0 = cheksiz")
    current_uses = models.IntegerField(default=0)
    max_uses_per_user = models.IntegerField(default=1)

    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'promo_codes'
        ordering = ['-created_at']
        verbose_name = 'Promo Code'
        verbose_name_plural = 'Promo Codes'

    def __str__(self):
        return f"{self.code} ({self.discount_value}{'%' if self.discount_type == 'percentage' else ' so\'m'})"

    def is_valid(self, user=None, order_amount=Decimal('0')) -> tuple[bool, str]:
        """Check if promo code is valid for use."""
        now = timezone.now()

        if not self.is_active:
            return False, "Promo kod faol emas"

        if self.starts_at and now < self.starts_at:
            return False, "Promo kod hali boshlanmagan"

        if self.expires_at and now > self.expires_at:
            return False, "Promo kod muddati tugagan"

        if self.max_uses > 0 and self.current_uses >= self.max_uses:
            return False, "Promo kod limiti tugagan"

        if order_amount < self.min_order_amount:
            return False, f"Minimal buyurtma summasi: {self.min_order_amount:,.0f} so'm"

        if user and user.is_authenticated and self.max_uses_per_user > 0:
            from .models import PromoCodeUsage
            user_uses = PromoCodeUsage.objects.filter(
                promo_code=self,
                user=user,
            ).count()
            if user_uses >= self.max_uses_per_user:
                return False, "Bu promo kodni allaqachon ishlatgansiz"

        return True, ""

    def calculate_discount(self, order_amount: Decimal) -> Decimal:
        """Calculate the discount amount for a given order total."""
        if self.discount_type == 'percentage':
            discount = order_amount * (self.discount_value / Decimal('100'))
            if self.max_discount_amount and discount > self.max_discount_amount:
                discount = self.max_discount_amount
            return discount
        else:
            # Fixed discount — cannot exceed order amount
            return min(self.discount_value, order_amount)

    def use(self, user=None, order=None, original_amount=Decimal('0'), final_amount=Decimal('0')):
        """Increment usage counter and create usage record."""
        self.current_uses += 1
        self.save(update_fields=['current_uses'])

        # Track usage for per-user limits
        if user or order:
            from .models import PromoCodeUsage
            PromoCodeUsage.objects.create(
                promo_code=self,
                user=user,
                order=order,
                original_amount=original_amount,
                final_amount=final_amount,
                discount_amount=original_amount - final_amount,
            )


class PromoCodeUsage(models.Model):
    """Track who used which promo code."""
    promo_code = models.ForeignKey(
        PromoCode, on_delete=models.CASCADE,
        related_name='usages'
    )
    order = models.ForeignKey(
        'orders.Order', on_delete=models.CASCADE,
        related_name='promo_usages'
    )
    user = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='promo_usages'
    )
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2)
    original_amount = models.DecimalField(max_digits=15, decimal_places=2)
    final_amount = models.DecimalField(max_digits=15, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'promo_code_usages'
        verbose_name = 'Promo Code Usage'
        verbose_name_plural = 'Promo Code Usages'

    def __str__(self):
        return f"{self.promo_code.code} — Order #{self.order.id}"
