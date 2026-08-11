import logging

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from .models import Payment
from .serializers import PaymentInitSerializer, PaymentSerializer
from .providers import PaymentProviderFactory, PaymentProviderError
from apps.orders.models import Order
from apps.settings_app.models import SiteSetting

logger = logging.getLogger(__name__)


class PaymentInitView(generics.GenericAPIView):
    serializer_class = PaymentInitSerializer
    # SECURITY: payments require an authenticated account AND the order must
    # belong to the requester (no IDOR — you cannot pay for someone else's
    # order or drain another user's balance). The provider itself is atomic
    # and idempotent (never double-charges an already-paid order).
    permission_classes = [permissions.IsAuthenticated]
    # Prevent payment spamming / abuse: max 10 payment init attempts/min.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            order = Order.objects.get(id=serializer.validated_data['order_id'])
        except Order.DoesNotExist:
            # Don't leak order existence — same response as unauthorized.
            return Response({'detail': 'Buyurtma topilmadi'}, status=status.HTTP_404_NOT_FOUND)

        # SECURITY: the order must belong to the authenticated user. An
        # attacker passing another user's order_id must not be able to
        # trigger a payment (which would debit the victim's balance).
        if order.customer_id != request.user.id:
            return Response({'detail': 'Buyurtma topilmadi'}, status=status.HTTP_404_NOT_FOUND)

        # SECURITY: never re-init an already-paid order (idempotency guard at
        # the API level; the provider has a second row-locked guard).
        if order.payment_status == 'paid':
            existing = (
                Payment.objects.filter(order=order, provider='balance', status='success')
                .order_by('-created_at')
                .first()
            )
            if existing:
                return Response({
                    'payment_id': existing.id,
                    'order_id': order.id,
                    'order_number': order.order_number,
                    'amount': str(order.total_price),
                    'provider': 'balance',
                    'status': 'success',
                    'idempotent': True,
                })
            return Response(
                {'detail': "Buyurtma allaqachon to'langan"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        provider_name = serializer.validated_data['provider']

        try:
            provider = PaymentProviderFactory.get_provider(provider_name)
        except PaymentProviderError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        settings = SiteSetting.get_all()
        settings['site_url'] = request.build_absolute_uri('/').rstrip('/')

        try:
            result = provider.init_payment(order, settings)
        except PaymentProviderError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        response_data = {
            'payment_id': result.get('payment_id'),
            'order_id': order.id,
            'order_number': order.order_number,
            'amount': str(order.total_price),
            'provider': provider_name,
            'redirect_url': result.get('redirect_url', ''),
            'status': result.get('status', 'pending'),
        }

        if result.get('idempotent'):
            response_data['idempotent'] = True
        if 'merchant_id' in result:
            response_data['merchant_id'] = result['merchant_id']
        if 'transaction_id' in result:
            response_data['transaction_id'] = result['transaction_id']
        if 'message' in result:
            response_data['message'] = result['message']
        if 'new_balance' in result:
            response_data['new_balance'] = result['new_balance']

        return Response(response_data)


class PaymentProviderListView(generics.GenericAPIView):
    """Get list of available payment providers."""
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        return Response([{
            'id': 'balance',
            'name': 'Balans',
            'icon': '💰',
            'description': "Hisobingizdagi mablag' orqali",
        }])
