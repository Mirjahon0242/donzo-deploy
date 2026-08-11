"""
Referral cashback engine — the ACTUAL money movement of the referral system.

Before this module existed the referral UI only *displayed* a hypothetical
"5% of your friends' spend" number — nothing was ever credited. All the real
movement lives here:

  • credit_referral_cashback(order)  → called right after a referred
    customer's order is PAID. Credits the referrer's cashback_balance (5% of
    the order, subject to MIN_ORDER_FOR_REFERRAL) and writes a 'cashback'
    BalanceTransaction. Idempotent at the DB level (idempotency_key =
    "ref:{order_number}") — a double-submit / retried webhook can never
    double-credit.

  • reverse_referral_cashback(order) → called when a paid order is refunded
    (admin rejects it). Reverses the credit — takes the money back from the
    referrer's cashback balance first, then (if already claimed/spent) claws
    back from the main balance so the platform never pays cashback for an
    order that was cancelled.
"""

import logging
from decimal import Decimal

from django.db import transaction

logger = logging.getLogger(__name__)

REFERRAL_BONUS_PERCENT = Decimal('5')      # 5% cashback to the referrer
MIN_ORDER_FOR_REFERRAL = Decimal('10000')  # order must be ≥ 10,000 so'm


def _cashback_idem_key(order):
    """Stable idempotency key per order — unique per (user, key) in DB."""
    return f"ref:{order.order_number}"


def grant_referral_milestone_rewards(referrer):
    """Grant referral milestone gifts (30 friends → 1 month Telegram Premium).

    Idempotent: a reward is only granted for milestone = 30 * (already
    granted + 1), and (referrer, milestone) is unique in the DB — running
    this any number of times never double-grants.

    Gift = the price of the '1 oy Premium' package credited to the referrer's
    balance (fragment-api.uz only supports 3/6/12-month premium buys, so the
    1-month gift is credited as money the user can spend on a 1-month
    Premium order). Writes a 'referral_gift' BalanceTransaction + AuditLog and
    notifies the user on Telegram.

    Returns the list of newly granted rewards (empty if none).
    """
    from apps.users.models import ReferralReward

    if not referrer or not referrer.is_active or referrer.is_blacklisted:
        return []

    from apps.users.models import User

    granted = []
    try:
        with transaction.atomic():
            referrer = User.objects.select_for_update().get(pk=referrer.pk)
            already = ReferralReward.objects.filter(
                referrer=referrer, status='granted'
            ).count()
            next_milestone = ReferralReward.MILESTONE_EVERY * (already + 1)
            referrals_count = User.objects.filter(referred_by=referrer).count()

            if referrals_count < next_milestone:
                return []

            amount = ReferralReward.REWARD_AMOUNT_1M_PREMIUM
            reward = ReferralReward.objects.create(
                referrer=referrer,
                milestone=next_milestone,
                reward_label=ReferralReward.REWARD_LABEL_1M_PREMIUM,
                amount=amount,
                status='granted',
            )

            # Credit the gift to the referrer's balance.
            from apps.payments.models import BalanceTransaction
            balance_before = referrer.balance or Decimal('0')
            referrer.balance = balance_before + amount
            referrer.save(update_fields=['balance'])
            BalanceTransaction.objects.create(
                user=referrer,
                tx_type='referral_gift',
                amount=amount,
                balance_before=balance_before,
                balance_after=referrer.balance,
                status='completed',
                provider='referral',
                description=(
                    f"Referal sovg'a: {next_milestone} ta do'st — "
                    f"{ReferralReward.REWARD_LABEL_1M_PREMIUM}"
                ),
            )

            from apps.audit_log.models import AuditLog
            AuditLog.objects.create(
                user=referrer,
                action='referral_milestone_reward',
                target_type='User',
                target_id=referrer.id,
                description=(
                    f"@{referrer.username} {next_milestone} ta do'st taklif qildi — "
                    f"sovg'a: {ReferralReward.REWARD_LABEL_1M_PREMIUM} "
                    f"({amount} so'm balansga kreditlandi)"
                ),
            )

            # Telegram orqali foydalanuvchiga xabar
            try:
                from apps.users.telegram_notify import send_to_user
                send_to_user(
                    referrer,
                    f"🎁 <b>Tabriklaymiz!</b> Siz {next_milestone} ta do'stingizni "
                    f"taklif qildingiz va sovg'a sifatida <b>{ReferralReward.REWARD_LABEL_1M_PREMIUM}</b> "
                    f"olishingiz kerak! {amount:,.0f} so'm balansingizga kreditlandi — "
                    f"uni Telegram Premium buyurtma qilishga ishlatishingiz mumkin.",
                )
            except Exception:
                logger.exception('referral milestone notify failed for %s', referrer.username)

            granted.append(reward)
            logger.info(
                '[Referral] %s granted %s (milestone %s)',
                referrer.username, reward.reward_label, next_milestone,
            )
    except Exception:
        logger.exception('grant_referral_milestone_rewards failed for %s',
                         getattr(referrer, 'username', referrer))
    return granted


def credit_referral_cashback(order):
    """
    Credit the referrer's cashback for a PAID order.

    Safe to call anywhere after the order is marked paid — it is atomic,
    row-locked and idempotent. Returns the created BalanceTransaction or
    None (no referral / below threshold / already credited).
    """
    try:
        from apps.users.models import User
        from apps.payments.models import BalanceTransaction

        customer = order.customer
        if not customer or not customer.referred_by_id:
            return None  # not a referral order
        if order.total_price < MIN_ORDER_FOR_REFERRAL:
            return None  # below threshold — no cashback
        if customer.referred_by_id == customer.pk:
            return None  # self-referral guard

        amount = order.total_price * REFERRAL_BONUS_PERCENT / Decimal('100')

        with transaction.atomic():
            referrer = User.objects.select_for_update().get(pk=customer.referred_by_id)
            if referrer.pk == customer.pk:
                return None
            if not referrer.is_active or referrer.is_blacklisted:
                return None

            # DB-level idempotency: the unique (user, idempotency_key)
            # constraint makes a second credit for the same order fail the
            # create — the exception is swallowed below, so we never
            # double-credit even under a retried/duplicate payment callback.
            balance_before = referrer.cashback_balance or Decimal('0')
            referrer.cashback_balance = balance_before + amount
            referrer.save(update_fields=['cashback_balance'])

            tx = BalanceTransaction.objects.create(
                user=referrer,
                idempotency_key=_cashback_idem_key(order),
                tx_type='cashback',
                amount=amount,
                balance_before=balance_before,
                balance_after=referrer.cashback_balance,
                status='completed',
                provider='balance',
                provider_transaction_id=f"REF:{order.order_number}",
                description=(
                    f"Referal cashback: buyurtma #{order.order_number} "
                    f"(@{customer.username or customer.telegram_username or customer.id})"
                ),
            )
            return tx
    except Exception:
        logger.exception('credit_referral_cashback failed for order %s', order.id)
        return None


def reverse_referral_cashback(order):
    """
    Reverse the referrer's cashback when a PAID order is refunded.

    Finds the original 'cashback' transaction by its idempotency key, takes
    the money back from cashback_balance first, then claws back from the
    main balance if the referrer already claimed/spent it (clamped at the
    available balance — we never drive a balance negative). The original tx
    is marked cancelled so the ledger stays truthful.
    """
    try:
        from apps.users.models import User
        from apps.payments.models import BalanceTransaction

        idem_key = _cashback_idem_key(order)

        with transaction.atomic():
            tx = (
                BalanceTransaction.objects
                .filter(idempotency_key=idem_key, tx_type='cashback', status='completed')
                .select_for_update()
                .first()
            )
            if not tx:
                return None  # no cashback was ever credited for this order

            referrer = User.objects.select_for_update().get(pk=tx.user_id)
            amount = tx.amount

            # 1) Take from cashback balance first.
            from_cashback = min(referrer.cashback_balance or Decimal('0'), amount)
            # 2) If the referrer already claimed/spent it, claw back from the
            #    main balance — but never below zero.
            rest = amount - from_cashback
            from_balance = min(rest, referrer.balance or Decimal('0'))

            referrer.cashback_balance = (referrer.cashback_balance or Decimal('0')) - from_cashback
            referrer.balance = (referrer.balance or Decimal('0')) - from_balance
            referrer.save(update_fields=['cashback_balance', 'balance'])

            tx.status = 'cancelled'
            tx.description = (
                f"{tx.description or ''} — bekor qilindi (buyurtma rad etildi, "
                f"{from_cashback + from_balance} so'm qaytarib olindi)"
            )
            tx.save(update_fields=['status', 'description'])
            return tx
    except Exception:
        logger.exception('reverse_referral_cashback failed for order %s', order.id)
        return None
