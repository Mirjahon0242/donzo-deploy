"""
Payment Provider Integration for TOPUP HUB.

Now simplified — only internal balance payments remain.
Users top up their balance directly (via admin-approved top-up requests),
then pay from balance.

SECURITY:
  • init_payment runs in an ATOMIC transaction with row locks
    (select_for_update) so two parallel payments can never double-spend
    or both read a stale balance (lost-update prevention).
  • Idempotent: an already-paid order is never charged twice — the
    existing successful Payment is returned instead.
  • Every deduction writes a 'purchase' BalanceTransaction for the audit
    trail, so every balance change is fully accounted for.
"""

import logging
import uuid
from abc import ABC, abstractmethod
from decimal import Decimal

from django.db import transaction

logger = logging.getLogger(__name__)


class PaymentProviderError(Exception):
    pass


class BaseProvider(ABC):
    """Abstract base class for payment providers."""

    @abstractmethod
    def init_payment(self, order, settings):
        """Initialize a payment with the provider."""
        pass

    @abstractmethod
    def verify_callback(self, data, settings):
        """Verify a callback/webhook from the provider."""
        pass

    @abstractmethod
    def get_provider_name(self):
        """Return the provider's slug name."""
        pass


class BalanceProvider(BaseProvider):
    """
    Internal wallet balance payment provider.
    Users pay from their account balance.
    """

    def get_provider_name(self):
        return 'balance'

    def init_payment(self, order, settings):
        from .models import Payment
        from apps.orders.models import Order
        from apps.users.models import User
        from apps.payments.models import BalanceTransaction
        from apps.audit_log.models import AuditLog

        # Whole operation is atomic with row locks — parallel payments can
        # never both read the same stale balance or double-charge the order.
        with transaction.atomic():
            # Lock the order row (re-fetch inside the transaction).
            try:
                order = Order.objects.select_for_update().get(pk=order.pk)
            except Order.DoesNotExist:
                raise PaymentProviderError("Buyurtma topilmadi")

            # ── Idempotency: never charge an already-paid order twice ──
            if order.payment_status == 'paid':
                existing = (
                    Payment.objects.filter(order=order, provider='balance', status='success')
                    .order_by('-created_at')
                    .first()
                )
                if existing:
                    # customer FK is SET_NULL — guard against a deleted customer.
                    new_balance = float(order.customer.balance) if order.customer else 0
                    return {
                        'payment_id': existing.id,
                        'transaction_id': existing.transaction_id,
                        'amount': str(order.total_price),
                        'provider': 'balance',
                        'status': 'success',
                        'message': "Buyurtma allaqachon to'langan",
                        'new_balance': new_balance,
                        'payment': existing,
                        'idempotent': True,
                    }
                raise PaymentProviderError("Buyurtma allaqachon to'langan")

            # Lock the customer row so balance read-modify-write is atomic.
            if not order.customer_id:
                raise PaymentProviderError("Buyurtma foydalanuvchiga bog'lanmagan")
            customer = User.objects.select_for_update().get(pk=order.customer_id)

            if not customer.is_active:
                raise PaymentProviderError("Hisobingiz bloklangan, to'lov amalga oshirilmadi")

            if customer.balance < order.total_price:
                raise PaymentProviderError(
                    f"Balansda yetarli mablag' mavjud emas. "
                    f"Mavjud: {customer.balance} so'm, Kerak: {order.total_price} so'm"
                )

            transaction_id = f"BAL{order.id}{uuid.uuid4().hex[:8].upper()}"

            payment = Payment.objects.create(
                order=order,
                provider='balance',
                transaction_id=transaction_id,
                amount=order.total_price,
                status='success',
            )

            # Deduct from balance (locked row — no lost update).
            balance_before = customer.balance
            customer.balance -= order.total_price
            customer.save(update_fields=['balance'])

            # Record the purchase in the balance transaction ledger so every
            # balance change is auditable (top-ups AND purchases).
            BalanceTransaction.objects.create(
                user=customer,
                tx_type='purchase',
                amount=-order.total_price,
                balance_before=balance_before,
                balance_after=customer.balance,
                status='completed',
                provider='balance',
                provider_transaction_id=transaction_id,
                description=f"Buyurtma #{order.order_number} uchun to'lov",
            )

            # Mark order as paid
            order.payment_status = 'paid'
            order.payment_method = 'balance'
            order.save(update_fields=['payment_status', 'payment_method'])

            # Audit trail
            AuditLog.objects.create(
                user=customer,
                action='payment_success',
                target_type='Order',
                target_id=order.id,
                description=f"Buyurtma #{order.order_number} balansdan to'landi: {order.total_price} so'm",
            )

            # ── Referral cashback ──
            # If this customer came via a referral code, credit the referrer
            # 5% cashback (idempotent — same order can never credit twice).
            # Inside the same atomic block, so the credit is all-or-nothing
            # with the payment itself.
            from apps.users.referral_service import credit_referral_cashback
            credit_referral_cashback(order)

        # Notify the customer on Telegram that their payment succeeded
        from apps.users.telegram_notify import notify_payment_success
        notify_payment_success(order, payment)

        # ── Telegram Stars/Premium: YETKAZIB BERISH ADMIN TASDIQINI KUTADI ──
        # To'lov balansdan o'tgach buyurtma 'pending' holatida qoladi va
        # Admin panel → 'Telegram buyurtmalar' bo'limida ko'rinadi. Admin
        # 'Tasdiqlash' tugmasini bosganda fragment-api.uz orqali haqiqiy
        # yetkazib berish amalga oshadi (wallet mablag'i faqat admin nazorati
        # ostida sarflanadi). 'Rad qilish' tugmasi esa balansni qaytaradi.
        # (Avvalgi auto-fulfillment o'chirildi — admin tasdig'i xavfsizroq.)

        return {
            'payment_id': payment.id,
            'transaction_id': transaction_id,
            'amount': str(order.total_price),
            'provider': 'balance',
            'status': 'success',
            'message': "To'lov balansdan amalga oshirildi",
            'new_balance': float(customer.balance),
            'payment': payment,
        }

    def verify_callback(self, data, settings):
        """Balance is instant, no callbacks needed."""
        return {
            'transaction_id': data.get('transaction_id', ''),
            'status': 'success',
            'amount': Decimal('0'),
            'raw_data': data,
        }


class PaymentProviderFactory:
    """Factory to get the appropriate provider."""

    _providers = {
        'balance': BalanceProvider,
    }

    @classmethod
    def get_provider(cls, provider_name):
        """Get a provider instance by name."""
        provider_class = cls._providers.get(provider_name)
        if not provider_class:
            raise PaymentProviderError(f"Unknown payment provider: {provider_name}")
        return provider_class()

    @classmethod
    def get_providers(cls):
        """Get all available provider names."""
        return list(cls._providers.keys())

    @classmethod
    def get_active_providers(cls, settings):
        """Balance is always active."""
        return ['balance']
