from decimal import Decimal
from datetime import timedelta

from django.db.models import Count, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from django_filters.rest_framework import DjangoFilterBackend
from .models import Order, OrderStatus
from .serializers import (
    OrderCreateSerializer, OrderListSerializer,
    OrderDetailSerializer, OrderStatusUpdateSerializer,
)
from apps.users.permissions import IsAdmin, IsOperator, IsOwnerOrAdmin
from apps.users.models import Role


class OrderCreateView(generics.CreateAPIView):
    queryset = Order.objects.all()
    serializer_class = OrderCreateSerializer
    # SECURITY: orders now require an authenticated account — guests browse the
    # catalogue freely but must log in (Telegram) to place an order, so every
    # order is always linked to a real user (needed for balance payments and
    # the operator flow). Prevent spamming: max 20/min.
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'order_create'

    def perform_create(self, serializer):
        order = serializer.save()
        # Create audit log entry
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            user=self.request.user,
            action='order_created',
            target_type='Order',
            target_id=order.id,
            description=f"Buyurtma yaratildi: #{order.order_number} - {order.service.name}",
        )


class OrderListView(generics.ListAPIView):
    serializer_class = OrderListSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # select_related: service_name/package_name in the serializer hit
        # service+package FKs — without this every order is 2 extra queries
        # (N+1). With it, one JOIN covers the whole list (speed fix).
        return Order.objects.filter(customer=self.request.user).select_related('service', 'package')


class OrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(customer=self.request.user).select_related('service', 'package')


class OrderStatusUpdateView(generics.GenericAPIView):
    serializer_class = OrderStatusUpdateSerializer
    permission_classes = [permissions.IsAuthenticated, IsOperator]

    def _user_can_modify_order(self, user, order) -> bool:
        """
        Check if the user has permission to modify this order.

        Admins can modify any order.
        Operators can only modify orders assigned to them.
        """
        if user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return True
        # Operators: only if assigned to this order
        return order.assigned_operator == user

    def patch(self, request, pk):
        from django.db import transaction
        # SECURITY: transaction + row lock (select_for_update) — two concurrent
        # PATCHes (operator + admin) can no longer both read the same status,
        # both pass the transition check and both persist (double-assign or
        # skip a legal transition). The lock serializes the read→check→save.
        with transaction.atomic():
            try:
                order = Order.objects.select_for_update().get(pk=pk)
            except Order.DoesNotExist:
                return Response({'detail': 'Buyurtma topilmadi'}, status=status.HTTP_404_NOT_FOUND)

            # SECURITY: Check operator has permission for this order
            if not self._user_can_modify_order(request.user, order):
                return Response(
                    {'detail': "Sizga bu buyurtmani o'zgartirishga ruxsat yo'q"},
                    status=status.HTTP_403_FORBIDDEN
                )

            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)

            old_status = order.status
            new_status = serializer.validated_data['status']

            # ── SECURITY: enforce legal status transitions ──
            # A completed or cancelled order is TERMINAL — it can never be
            # rolled back to pending/processing (prevents fraud: an operator
            # marking an order completed then re-processing to double-charge
            # or hide it from stats).
            ALLOWED = {
                'pending': {'processing', 'completed', 'cancelled'},
                'processing': {'completed', 'cancelled'},
                'completed': set(),
                'cancelled': set(),
            }
            if old_status == new_status:
                return Response(
                    {'detail': f"Buyurtma allaqachon {new_status} holatida"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if new_status not in ALLOWED.get(old_status, set()):
                return Response(
                    {'detail': f"Statusni {old_status} → {new_status} ga o'zgartirib bo'lmaydi"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Auto-assign operator if not yet assigned (inside the lock — two
            # operators can never both claim the same pending order).
            if not order.assigned_operator:
                order.assigned_operator = request.user

            # Save the cancel reason only when the order is being cancelled — a
            # reason on any other transition would be inconsistent state. Clearing
            # the reason when leaving the cancelled state keeps stale data out.
            cancel_reason = serializer.validated_data.get('cancel_reason')
            if new_status == 'cancelled':
                order.cancel_reason = cancel_reason or "Sabab ko'rsatilmagan"
            elif old_status == 'cancelled':
                order.cancel_reason = None

            order.status = new_status
            order.save()

            # Create audit log
            from apps.audit_log.models import AuditLog
            AuditLog.objects.create(
                user=request.user,
                action='order_status_changed',
                target_type='Order',
                target_id=order.id,
                description=f"Buyurtma #{order.order_number} statusi o'zgartirildi: {old_status} -> {new_status}",
            )

            # Notify the customer on Telegram about the status change
            from apps.users.telegram_notify import notify_order_status
            notify_order_status(order, old_status, new_status)

            return Response(OrderDetailSerializer(order).data)


class OrderStatsView(generics.GenericAPIView):
    """
    Returns order statistics for the authenticated user:
    - Monthly spending (last 12 months)
    - Top services by order count
    - Payment method distribution
    - Overall stats (total orders, total spent, avg order value)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        now = timezone.now()
        twelve_months_ago = now - timedelta(days=365)

        # ── Overall stats ──
        user_orders = Order.objects.filter(customer=user)
        total_orders = user_orders.count()
        total_spent = user_orders.aggregate(total=Sum('total_price'))['total'] or Decimal('0')
        avg_order_value = (
            (total_spent / total_orders)
            if total_orders > 0 else Decimal('0')
        )

        # ── Monthly spending (last 12 months) ──
        monthly_spending = (
            user_orders
            .filter(created_at__gte=twelve_months_ago, payment_status='paid')
            .annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(spent=Sum('total_price'), count=Count('id'))
            .order_by('month')
        )

        # Fill in missing months with zero
        monthly_data = []
        month_labels = []
        for i in range(11, -1, -1):
            month_date = (now.replace(day=1) - timedelta(days=i * 30)).replace(day=1)
            month_label = month_date.strftime('%b %Y')
            entry = next(
                (m for m in monthly_spending if m['month'] and m['month'].strftime('%b %Y') == month_label),
                None
            )
            monthly_data.append({
                'month': month_label,
                'spent': float(entry['spent']) if entry else 0,
                'count': entry['count'] if entry else 0,
            })
            month_labels.append(month_label)

        # ── Top services ──
        top_services = (
            user_orders
            .values('service__name', 'service__image_url')
            .annotate(count=Count('id'), total=Sum('total_price'))
            .order_by('-count')[:5]
        )

        # ── Payment method distribution ──
        payment_methods = (
            user_orders
            .values('payment_method')
            .annotate(count=Count('id'), total=Sum('total_price'))
            .order_by('-count')
        )

        # ── Status distribution ──
        status_distribution = (
            user_orders
            .values('status')
            .annotate(count=Count('id'))
        )

        return Response({
            'overall': {
                'total_orders': total_orders,
                'total_spent': float(total_spent),
                'avg_order_value': float(avg_order_value),
            },
            'monthly_spending': monthly_data,
            'top_services': [
                {
                    'name': s['service__name'],
                    'image_url': s['service__image_url'],
                    'count': s['count'],
                    'total': float(s['total']),
                }
                for s in top_services
            ],
            'payment_methods': [
                {
                    'method': p['payment_method'] or 'unknown',
                    'count': p['count'],
                    'total': float(p['total']),
                }
                for p in payment_methods
            ],
            'status_distribution': {
                s['status']: s['count'] for s in status_distribution
            },
        })
