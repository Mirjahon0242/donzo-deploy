"""
Balance Top-Up Views — users add money to their account.

SECURITY: top-ups are NOT instant. A top-up request creates a PENDING
BalanceTransaction that an admin must approve in the admin panel (after
receiving the real money via bank transfer / cash). This closes the
"free money" hole: without a trusted external payment provider there is
no way to verify that money actually arrived, so an unverified auto-credit
would let anyone print unlimited balance.

Flow:
  1. User selects amount → POST /api/v1/balance/topup/
     → creates a PENDING BalanceTransaction (balance NOT credited)
  2. Admin approves  → POST /api/v1/admin/balance-topups/<id>/approve/
     → balance credited atomically (row-locked)
  3. Admin rejects   → POST /api/v1/admin/balance-topups/<id>/reject/
     → transaction cancelled, nothing credited
"""
import logging
from decimal import Decimal

from django.db import transaction
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from .models import BalanceTransaction
from .serializers import BalanceTopUpSerializer, BalanceTransactionSerializer
from apps.users.permissions import IsAdmin

logger = logging.getLogger(__name__)


def _card_extra(card_req):
    """Unique-amount payment instructions for the top-up response.

    When card monitoring is on and a CardTopupRequest exists, the user must
    send the EXACT unique amount to the card within the timeout window —
    the balance is then credited automatically. Otherwise falls back to the
    classic admin-approval flow (requires_approval=True).
    """
    if card_req is None:
        return {'requires_unique_payment': False}
    from apps.cardpay import services as cardpay_services
    s = cardpay_services.get_settings()
    return {
        'requires_unique_payment': True,
        'card_request_id': card_req.id,
        'unique_amount': str(card_req.unique_amount),
        'requested_amount': str(card_req.requested_amount),
        'expires_at': card_req.expires_at.isoformat(),
        'timeout_minutes': s['timeout_minutes'],
        'card_number': s['card_number'],
        'card_holder': s['card_holder'],
        'suspicious_limit': s['suspicious_limit'],
        'instructions': (
            f"Kartaga AYNAN {card_req.unique_amount:,.0f} so'm yuboring. "
            f"Boshqa summa yuborsangiz hisobga tushmaydi! "
            f"To'lov {s['timeout_minutes']} daqiqa ichida amalga oshishi kerak."
        ),
    }


class BalanceTopUpStatusView(generics.GenericAPIView):
    """
    GET /api/v1/payments/balance/topup/<tx_id>/status/

    Polled by the balance page while the user waits for the card payment.
    Returns pending → paid/cancelled/expired and the new balance when paid.
    """
    permission_classes = [permissions.IsAuthenticated]
    # Separate, generous scope: the balance page polls every 5s (12/min) —
    # the strict 'payments' scope (10/min) would 429 the poller.
    throttle_scope = 'cardpay_status'
    throttle_classes = [ScopedRateThrottle]

    def get(self, request, tx_id):
        try:
            tx = BalanceTransaction.objects.get(pk=tx_id, user=request.user, tx_type='topup')
        except BalanceTransaction.DoesNotExist:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        from apps.cardpay.models import CardTopupRequest
        card_req = CardTopupRequest.objects.filter(balance_tx=tx).order_by('-created_at').first()
        return Response({
            'status': tx.status,
            'balance_after': float(request.user.balance),
            'card_request': None if not card_req else {
                'id': card_req.id,
                'unique_amount': str(card_req.unique_amount),
                'status': card_req.status,
                'expires_at': card_req.expires_at.isoformat(),
            },
        })


class BalanceTopUpInitView(generics.GenericAPIView):
    """
    POST /api/v1/balance/topup/

    Create a PENDING balance top-up request. The balance is NOT credited
    here — an admin must approve the request first (manual transfer flow).
    Idempotency: re-sending the same idempotency_key returns the original
    pending request instead of creating a duplicate.
    """
    serializer_class = BalanceTopUpSerializer
    permission_classes = [permissions.IsAuthenticated]
    # Prevent top-up spam: max 10 attempts/min.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        amount = serializer.validated_data['amount']
        idem_key = serializer.validated_data.get('idempotency_key') or None

        user = request.user

        # Idempotency: a request with this exact key already exists → return
        # it without creating a duplicate (network retry / double-click safety).
        if idem_key:
            existing = BalanceTransaction.objects.filter(
                user=user, idempotency_key=idem_key, tx_type='topup',
            ).first()
            if existing:
                logger.info(
                    f"[Balance] Idempotent top-up request hit for {user.username} "
                    f"key={idem_key} — returning existing"
                )
                from apps.cardpay.models import CardTopupRequest
                card_req = CardTopupRequest.objects.filter(
                    balance_tx=existing, status='pending',
                ).first()
                extra = _card_extra(card_req)
                return Response({
                    'balance_tx_id': existing.id,
                    'amount': str(existing.amount),
                    'balance_after': float(user.balance),
                    'status': existing.status,
                    'idempotent': True,
                    'requires_approval': not extra['requires_unique_payment'],
                    **extra,
                })

        balance_before = user.balance

        # Create a PENDING BalanceTransaction — balance is only credited on
        # admin approval (or automatically by the card payment listener).
        tx = BalanceTransaction.objects.create(
            user=user,
            idempotency_key=idem_key,
            tx_type='topup',
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_before,  # unchanged until approved
            status='pending',
            description="Balans to'ldirish so'rovi (tasdiqlash kutilmoqda)",
        )

        # ── Card payment auto-verification: if enabled, generate a UNIQUE
        #    amount to send (requested + random offset) + expiry window. The
        #    Telethon user client matches the incoming transfer and credits
        #    the balance automatically. If disabled → classic admin approval.
        card_req = None
        try:
            from apps.cardpay import services as cardpay_services
            s = cardpay_services.get_settings()
            if s['enabled']:
                card_req = cardpay_services.create_topup_request(
                    user, tx, amount, s['timeout_minutes'], s['offset_max'])
                tx.description = (
                    f"Karta to'lovi kutilmoqda: aynan {card_req.unique_amount:,.0f} so'm "
                    f"yuboring ({s['timeout_minutes']} daqiqa)"
                )
                tx.save(update_fields=['description'])
                logger.info(
                    f"[CardPay] Top-up #{card_req.id}: @{user.username} "
                    f"unique={card_req.unique_amount} (requested {amount})"
                )
        except Exception:
            logger.exception('CardPay top-up request creation failed (falling back to admin approval)')
            card_req = None

        extra = _card_extra(card_req)

        logger.info(
            f"[Balance] Top-up REQUEST: {user.username} +{amount} so'm "
            f"(pending approval, tx #{tx.id})"
        )

        # Audit log
        from apps.audit_log.models import AuditLog
        desc = f"Balans to'ldirish so'rovi: {amount} so'm (tasdiqlash kutilmoqda)"
        if card_req:
            desc += f" | Karta: aynan {card_req.unique_amount:,.0f} so'm yuboring"
        AuditLog.objects.create(
            user=user,
            action='balance_topup_requested',
            target_type='BalanceTransaction',
            target_id=tx.id,
            description=desc,
        )

        return Response({
            'balance_tx_id': tx.id,
            'amount': str(amount),
            'balance_after': float(user.balance),
            'status': 'pending',
            'idempotent': False,
            'requires_approval': not extra['requires_unique_payment'],
            **extra,
        })


class AdminBalanceTopUpListView(generics.ListAPIView):
    """
    GET /api/v1/admin/balance-topups/

    List top-up requests (default: pending ones needing approval).
    Admin only.
    """
    serializer_class = BalanceTransactionSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get_queryset(self):
        qs = BalanceTransaction.objects.filter(tx_type='topup').select_related('user')
        status_filter = self.request.query_params.get('status', 'pending')
        if status_filter and status_filter != 'all':
            qs = qs.filter(status=status_filter)
        return qs.order_by('-created_at')[:100]

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        # Return user as a nested object (id + username) so the admin UI can
        # show "@username" instead of a bare numeric FK id.
        data = BalanceTransactionSerializer(qs, many=True).data
        user_map = {}
        for t in qs:
            if t.user_id not in user_map:
                user_map[t.user_id] = {
                    'id': t.user_id,
                    'username': t.user.username if t.user else None,
                }
        for item in data:
            item['user'] = user_map.get(item.get('user'))
        return Response({'count': len(data), 'results': data})


class AdminBalanceTopUpActionView(generics.GenericAPIView):
    """
    POST /api/v1/admin/balance-topups/<id>/approve/
    POST /api/v1/admin/balance-topups/<id>/reject/

    Approve = credit the user's balance atomically (row-locked so two
    parallel approvals can never double-credit).
    Reject  = cancel the request, nothing credited.
    Admin only. Idempotent — approving twice credits only once.
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    lookup_url_kwarg = 'pk'

    def get_object(self):
        pk = self.kwargs['pk']
        try:
            return BalanceTransaction.objects.get(pk=pk, tx_type='topup')
        except BalanceTransaction.DoesNotExist:
            return None

    def post(self, request, pk, action=None):
        # URL path() kwargs={'action': 'approve'|'reject'} Django tomonidan
        # view'ga kwargs sifatida uzatiladi — shu yerda qabul qilinadi.
        # (avvalgi `self.kwargs.get('action')` TypeError berardi: URL kwargs
        # `post()` metodiga bevosita uzatiladi, `self.kwargs` emas.)
        action = action or getattr(self, 'action', None) or self.kwargs.get('action')
        tx = self.get_object()

        if tx is None:
            return Response(
                {'detail': "To'ldirish so'rovi topilmadi"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if action == 'approve':
            return self._approve(request, tx)
        return self._reject(request, tx)

    def _approve(self, request, tx):
        from apps.users.models import User

        with transaction.atomic():
            # Row-lock the transaction AND the user so concurrent approvals
            # cannot double-credit (lost-update prevention).
            tx = BalanceTransaction.objects.select_for_update().get(pk=tx.pk)
            user = User.objects.select_for_update().get(pk=tx.user_id)

            if tx.status == 'completed':
                # Already approved — return the existing result (idempotent).
                return Response({
                    'balance_tx_id': tx.id,
                    'amount': str(tx.amount),
                    'balance_after': float(user.balance),
                    'status': 'completed',
                    'idempotent': True,
                })
            if tx.status != 'pending':
                return Response(
                    {'detail': f"So'rov {tx.status} holatida, tasdiqlab bo'lmaydi"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            tx.balance_after = tx.balance_before + tx.amount
            tx.status = 'completed'
            tx.description = "Balans to'ldirildi (admin tasdiqladi)"
            tx.save(update_fields=['status', 'balance_after', 'description'])

            user.balance += tx.amount
            user.save(update_fields=['balance'])

        logger.info(f"[Balance] Top-up APPROVED #{tx.id}: {user.username} +{tx.amount} so'm")

        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            user=request.user,
            action='balance_topup_approved',
            target_type='BalanceTransaction',
            target_id=tx.id,
            description=f"Balans to'ldirish tasdiqlandi: {tx.amount} so'm → @{user.username}",
        )

        # Notify the user on Telegram that their top-up was approved
        from apps.users.telegram_notify import notify_topup_status
        notify_topup_status(user, tx.amount, 'completed', balance_after=user.balance)

        return Response({
            'balance_tx_id': tx.id,
            'amount': str(tx.amount),
            'balance_after': float(user.balance),
            'status': 'completed',
            'idempotent': False,
        })

    def _reject(self, request, tx):
        with transaction.atomic():
            tx = BalanceTransaction.objects.select_for_update().get(pk=tx.pk)
            if tx.status != 'pending':
                return Response(
                    {'detail': f"So'rov {tx.status} holatida, bekor qilib bo'lmaydi"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tx.status = 'cancelled'
            tx.description = "Balans to'ldirish rad etildi"
            tx.save(update_fields=['status', 'description'])

        logger.info(f"[Balance] Top-up REJECTED #{tx.id}: {tx.amount} so'm")

        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            user=request.user,
            action='balance_topup_rejected',
            target_type='BalanceTransaction',
            target_id=tx.id,
            description=f"Balans to'ldirish rad etildi: {tx.amount} so'm",
        )

        # Notify the user on Telegram that their top-up was rejected
        from apps.users.telegram_notify import notify_topup_status
        notify_topup_status(tx.user, tx.amount, 'cancelled')

        return Response({
            'balance_tx_id': tx.id,
            'status': 'cancelled',
            'amount': str(tx.amount),
        })


class BalanceTransactionHistoryView(generics.ListAPIView):
    """
    GET /api/v1/balance/history/

    View your balance transaction history (top-ups, purchases, etc.)
    """
    serializer_class = BalanceTransactionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return BalanceTransaction.objects.filter(user=self.request.user)[:50]
