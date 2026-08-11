import logging

from django.urls import path
from rest_framework import generics, permissions, filters, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Sum, Count, F, Q, Avg, Min, Max, ExpressionWrapper, DurationField
from django.db.models.functions import ExtractHour
from django.utils import timezone
from datetime import timedelta

from .models import Order, OrderStatus
from .serializers import OrderListSerializer, OrderDetailSerializer
from apps.users.permissions import IsAdmin, IsOperator
from apps.users.models import Role

logger = logging.getLogger(__name__)

# Telegram Premium + Stars paketlari shu bitta xizmat ostida yashaydi.
TELEGRAM_SERVICE_SLUG = 'telegram-premium'


class AdminOrderListView(generics.ListAPIView):
    serializer_class = OrderListSerializer
    permission_classes = [permissions.IsAuthenticated, IsOperator]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['status', 'payment_status']
    search_fields = ['order_number', 'customer_name', 'customer_telegram']

    def get_queryset(self):
        """
        SECURITY: Non-admin operators see ONLY orders assigned to them.
        Admins and super admins see all orders.
        """
        user = self.request.user
        if user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return Order.objects.all().order_by('-created_at')
        # Operators see only their assigned orders
        return Order.objects.filter(assigned_operator=user).order_by('-created_at')


class AdminOrderDetailView(generics.RetrieveAPIView):
    serializer_class = OrderDetailSerializer
    permission_classes = [permissions.IsAuthenticated, IsOperator]

    def get_queryset(self):
        """
        SECURITY: Operators see only their assigned orders.
        """
        user = self.request.user
        if user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return Order.objects.all()
        return Order.objects.filter(assigned_operator=user)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOperator])
def operator_dashboard(request):
    """
    Simplified dashboard for operators.
    Shows today's orders, pending orders, and their completed orders.
    """
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)

    is_admin = request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]

    # ── SECURITY: scope EVERYTHING to the operator's own assigned orders ──
    # Non-admin operators must never see global revenue / order volumes or
    # other customers' orders. All aggregates below are scoped accordingly.
    if is_admin:
        base_qs = Order.objects.all()
    else:
        base_qs = Order.objects.filter(assigned_operator=request.user)

    today_orders = base_qs.filter(created_at__date=today)
    week_orders = base_qs.filter(created_at__date__gte=week_ago)

    # Operator-specific stats
    operator_orders = Order.objects.filter(assigned_operator=request.user)
    operator_completed_today = operator_orders.filter(
        status='completed', created_at__date=today
    ).count()

    # Status breakdown (scoped to own orders for operators)
    status_counts = {
        status: base_qs.filter(status=status).count()
        for status in ['pending', 'processing', 'completed', 'cancelled']
    }

    today_revenue = today_orders.filter(
        payment_status='paid'
    ).aggregate(Sum('total_price'))['total_price__sum'] or 0

    # Recent orders — operators see ONLY their own assigned orders.
    recent_orders = base_qs.select_related('service').order_by('-created_at')[:20]

    return Response({
        'today_orders': today_orders.count(),
        'today_revenue': float(today_revenue),
        'today_completed': today_orders.filter(status='completed').count(),
        'today_pending': today_orders.filter(status='pending').count(),
        'pending_orders': status_counts['pending'],
        'processing_orders': status_counts['processing'],
        'completed_orders': status_counts['completed'],
        'total_orders': base_qs.count(),
        'operator_completed_today': operator_completed_today,
        'operator_total_assigned': operator_orders.count(),
        'recent_orders': [
            {
                'id': o.id,
                'order_number': o.order_number,
                'customer_name': o.customer_name,
                'service_name': o.service.name if o.service else '',
                'total_price': float(o.total_price),
                'status': o.status,
                'payment_status': o.payment_status,
                'customer_telegram': o.customer_telegram,
                'created_at': o.created_at.isoformat(),
            }
            for o in recent_orders
        ],
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOperator])
def operator_stats(request):
    """
    Detailed operator performance statistics.
    Returns completion metrics, hourly distribution, and trends.
    """
    user = request.user
    today = timezone.now().date()
    
    # ── 1. Operator's completed orders ──
    operator_completed = Order.objects.filter(
        assigned_operator=user,
        status='completed',
    )
    total_completed = operator_completed.count()
    
    # ── 2. Average completion time (database-level aggregation) ──
    duration_expr = ExpressionWrapper(
        F('updated_at') - F('created_at'),
        output_field=DurationField()
    )
    time_stats = operator_completed.exclude(updated_at__lt=F('created_at')).aggregate(
        avg_duration=Avg(duration_expr),
        min_duration=Min(duration_expr),
        max_duration=Max(duration_expr),
    )
    
    avg_td = time_stats['avg_duration']
    min_td = time_stats['min_duration']
    max_td = time_stats['max_duration']
    
    avg_completion_minutes = round(avg_td.total_seconds() / 60, 1) if avg_td else 0
    fastest_minutes = round(min_td.total_seconds() / 60, 1) if min_td else 0
    longest_minutes = round(max_td.total_seconds() / 60, 1) if max_td else 0
    
    # ── 3. Operator ranking ──
    operator_counts = (
        Order.objects.filter(status='completed', assigned_operator__isnull=False)
        .values('assigned_operator')
        .annotate(count=Count('id'))
        .order_by('-count')
    )
    
    operator_rank = 1
    total_operators = len(operator_counts)
    for idx, oc in enumerate(operator_counts):
        if oc['assigned_operator'] == user.id:
            operator_rank = idx + 1
            break
    
    # ── 4. Hourly distribution (last 30 days) ──
    thirty_days_ago = today - timedelta(days=30)
    hourly_qs = Order.objects.filter(
        created_at__date__gte=thirty_days_ago,
        assigned_operator=user,
    ).annotate(
        hour=ExtractHour('created_at')
    ).values('hour').annotate(
        count=Count('id')
    ).order_by('hour')
    
    hourly_distribution = [0] * 24
    for h in hourly_qs:
        hour = h['hour']
        if 0 <= hour <= 23:
            hourly_distribution[hour] = h['count']
    
    # ── 5. Daily trend (last 7 days) with database-level aggregation ──
    week_dates = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
    daily_stats = []
    for d in week_dates:
        day_completed = operator_completed.filter(created_at__date=d).count()
        day_total = Order.objects.filter(assigned_operator=user, created_at__date=d).count()
        
        # Database-level average for the day
        day_avg = operator_completed.filter(
            created_at__date=d
        ).exclude(updated_at__lt=F('created_at')).aggregate(
            avg_d=Avg(ExpressionWrapper(
                F('updated_at') - F('created_at'),
                output_field=DurationField()
            ))
        )['avg_d']
        day_avg_min = round(day_avg.total_seconds() / 60, 1) if day_avg else 0
        
        daily_stats.append({
            'date': d.isoformat(),
            'day_name': d.strftime('%a'),
            'completed': day_completed,
            'total': day_total,
            'avg_completion_minutes': day_avg_min,
        })
    
    # ── 6. Service breakdown ──
    service_breakdown = (
        operator_completed
        .values('service__name')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )
    
    # ── 7. Today's performance (database-level aggregation) ──
    today_completed = operator_completed.filter(created_at__date=today).count()
    today_total = Order.objects.filter(assigned_operator=user, created_at__date=today).count()
    today_avg = operator_completed.filter(
        created_at__date=today
    ).exclude(updated_at__lt=F('created_at')).aggregate(
        avg_d=Avg(ExpressionWrapper(
            F('updated_at') - F('created_at'),
            output_field=DurationField()
        ))
    )['avg_d']
    today_avg_min = round(today_avg.total_seconds() / 60, 1) if today_avg else 0
    
    return Response({
        'total_completed': total_completed,
        'average_completion_minutes': avg_completion_minutes,
        'fastest_completion_minutes': fastest_minutes,
        'longest_completion_minutes': longest_minutes,
        'operator_rank': operator_rank,
        'total_operators': total_operators,
        'hourly_distribution': hourly_distribution,
        'daily_stats': daily_stats,
        'service_breakdown': list(service_breakdown),
        'today_completed': today_completed,
        'today_total': today_total,
        'today_avg_completion_minutes': today_avg_min,
        'completion_rate': round((today_completed / today_total * 100), 1) if today_total > 0 else 0,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsOperator])
def operator_available_orders(request):
    """
    Returns all pending/unassigned orders for operators to accept.
    Operators see orders that no one has claimed yet.
    Excludes the operator's own assigned orders (shown in the normal list).
    """
    available_orders = Order.objects.filter(
        assigned_operator__isnull=True,
        status=OrderStatus.PENDING,
    ).select_related('service', 'package').order_by('-created_at')

    serializer = OrderListSerializer(available_orders, many=True)
    return Response({
        'count': available_orders.count(),
        'results': serializer.data,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsOperator])
def operator_accept_order(request, pk):
    """
    Assign an order to the current operator using an atomic update.
    Only works for orders that are still pending and unassigned.
    Uses filter-then-update with database-level filtering to prevent
    double-assignment in concurrent requests.
    """
    from django.db import transaction

    with transaction.atomic():
        # Atomically claim the order: only if still unassigned & pending
        updated = Order.objects.filter(
            pk=pk,
            assigned_operator__isnull=True,
            status=OrderStatus.PENDING,
        ).select_for_update().update(
            assigned_operator=request.user,
            status=OrderStatus.PROCESSING,
        )

        if updated == 0:
            # No rows updated — either doesn't exist or was already taken
            try:
                order = Order.objects.get(pk=pk)
                if order.assigned_operator == request.user:
                    return Response(
                        {'detail': 'Bu buyurtma allaqachon sizga biriktirilgan'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                if order.assigned_operator is not None:
                    return Response(
                        {'detail': 'Bu buyurtma allaqachon boshqa operator tomonidan qabul qilingan'},
                        status=status.HTTP_409_CONFLICT
                    )
                return Response(
                    {'detail': f"Bu buyurtma {order.status} holatida, qabul qilib bo'lmaydi"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            except Order.DoesNotExist:
                return Response(
                    {'detail': 'Buyurtma topilmadi'},
                    status=status.HTTP_404_NOT_FOUND
                )

        # Re-fetch the updated order
        order = Order.objects.get(pk=pk)

        # Create audit log
        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            user=request.user,
            action='order_accepted',
            target_type='Order',
            target_id=order.id,
            description=f"Operator {request.user.username} buyurtma #{order.order_number} ni qabul qildi",
        )

        # Notify the customer that an operator picked up their order
        from apps.users.telegram_notify import notify_order_status
        notify_order_status(order, OrderStatus.PENDING, OrderStatus.PROCESSING)

    return Response(OrderDetailSerializer(order).data, status=status.HTTP_200_OK)


# ─────────────────────────── Telegram buyurtmalar (admin tasdiqlash) ───────────────────────────


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_telegram_orders(request):
    """Telegram Premium/Stars buyurtmalari ro'yxati + KPI statistikasi.

    Faqat 'telegram-premium' xizmatidagi buyurtmalar ko'rinadi (Premium VA
    Stars paketlari). Admin 'Tasdiqlash'/'Rad qilish' tugmalari orqali
    yetkazib berishni boshqaradi. IsAdmin bilan himoyalangan — operatorlar
    bu bo'limni ko'ra olmaydi (fragment wallet pulini faqat admin ishlatadi).
    """
    qs = (
        Order.objects
        .filter(service__slug=TELEGRAM_SERVICE_SLUG)
        .select_related('service', 'package', 'customer')
        .order_by('-created_at')
    )

    status_f = request.query_params.get('status')
    if status_f in ('pending', 'processing', 'completed', 'cancelled'):
        qs = qs.filter(status=status_f)
    pay_f = request.query_params.get('payment_status')
    if pay_f in ('unpaid', 'paid', 'refunded'):
        qs = qs.filter(payment_status=pay_f)
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(order_number__icontains=q) | Q(customer_telegram__icontains=q))

    # KPI — barcha Telegram buyurtmalari ustida (filterga bog'liq emas)
    all_tg = Order.objects.filter(service__slug=TELEGRAM_SERVICE_SLUG)
    paid_tg = all_tg.filter(payment_status='paid')
    waiting_qs = paid_tg.filter(status__in=['pending', 'processing'])
    done_qs = paid_tg.filter(status='completed')
    stats = {
        'total': all_tg.count(),
        'waiting': waiting_qs.count(),
        'completed': done_qs.count(),
        # Rad etilganlar — to'lov holatidan qat'i nazar (refunded ham kiradi)
        'cancelled': all_tg.filter(status='cancelled').count(),
        'waiting_revenue': float(waiting_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0),
        'total_revenue': float(done_qs.aggregate(Sum('total_price'))['total_price__sum'] or 0),
        'refunded_revenue': float(
            all_tg.filter(status='cancelled', payment_status='refunded')
            .aggregate(Sum('total_price'))['total_price__sum'] or 0
        ),
    }

    serializer = OrderDetailSerializer(qs[:200], many=True)
    return Response({
        'count': qs.count(),
        'results': serializer.data,
        'stats': stats,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_telegram_confirm(request, pk):
    """Tasdiqlash → fragment-api.uz orqali buyurtmani DARHOL bajarish.

    Buy fragment API'da bevosita bajariladi (stars/buy yoki premium/buy),
    natija darhol qaytadi. Muvaffaqiyat → status='completed';
    xatolik → status='processing' (izoh bilan) — admin xatoni ko'radi.
    """
    try:
        order = (
            Order.objects
            .select_related('service', 'package', 'customer')
            .get(pk=pk, service__slug=TELEGRAM_SERVICE_SLUG)
        )
    except Order.DoesNotExist:
        return Response({'detail': 'Telegram buyurtmasi topilmadi', 'ok': False},
                        status=status.HTTP_404_NOT_FOUND)

    if order.status in ('completed', 'cancelled'):
        return Response({'detail': 'Bu buyurtma allaqachon yakunlangan', 'ok': False},
                        status=status.HTTP_400_BAD_REQUEST)
    # SECURITY (double-spend): 'processing' buyurtma avval yetkazib berishga
    # urinilgan — qayta tasdiqlash fragment'ga takroriy xarid yuborishi mumkin
    # (birinchi urinish tarmoq timeout'ida bajarilgan bo'lishi xavfi). Faqat
    # 'pending' buyurtma tasdiqlanadi.
    if order.status == 'processing':
        return Response({
            'detail': "Bu buyurtma allaqachon yetkazib berishga urinilgan. "
                      "Xavfsizlik uchun qayta tasdiqlash bloklangan — "
                      "'Rad qilish' orqali balansni qaytaring.",
            'ok': False,
        }, status=status.HTTP_409_CONFLICT)
    if order.payment_status != 'paid':
        return Response({'detail': "Buyurtma hali to'lanmagan", 'ok': False},
                        status=status.HTTP_400_BAD_REQUEST)

    from apps.services.fragment_fulfillment import auto_fulfill_order
    ok, message = auto_fulfill_order(order, actor=request.user)
    order.refresh_from_db()
    return Response({
        'ok': ok,
        'detail': message,
        'order': OrderDetailSerializer(order).data,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_telegram_reject(request, pk):
    """Rad qilish → buyurtmani bekor qilish + to'langan balansni qaytarish.

    To'lov balansdan yechilgan bo'lsa, summa mijoz balansiga atomik
    qaytariladi (row-lock bilan) va BalanceTransaction 'refund' sifatida
    yoziladi — audit trail buzilmaydi. Mijozga Telegram xabar boradi.
    """
    from decimal import Decimal
    from django.db import transaction
    from apps.users.models import User
    from apps.payments.models import BalanceTransaction
    from apps.audit_log.models import AuditLog

    reason = ((request.data or {}).get('cancel_reason') or '').strip()
    if not reason:
        reason = 'Administrator rad etdi'

    with transaction.atomic():
        try:
            order = (
                # of=('self',): PostgreSQL'da FOR UPDATE faqat order jadvalini
                # bloklaydi — select_related LEFT JOIN'li bo'lsa, "nullable side
                # of an outer join" xatosi chiqardi (SQLite'da yo'q edi).
                Order.objects.select_for_update(of=('self',))
                .select_related('service', 'package', 'customer')
                .get(pk=pk, service__slug=TELEGRAM_SERVICE_SLUG)
            )
        except Order.DoesNotExist:
            return Response({'detail': 'Telegram buyurtmasi topilmadi', 'ok': False},
                            status=status.HTTP_404_NOT_FOUND)

        if order.status == 'completed':
            return Response({'detail': 'Tugallangan buyurtmani rad etib bo\'lmaydi', 'ok': False},
                            status=status.HTTP_400_BAD_REQUEST)

        refunded = Decimal('0')
        # ── Balansdan to'langan bo'lsa — qaytarish (atomik) ──
        if order.payment_status == 'paid' and order.customer_id:
            customer = User.objects.select_for_update().get(pk=order.customer_id)
            balance_before = customer.balance
            customer.balance += order.total_price
            customer.save(update_fields=['balance'])
            BalanceTransaction.objects.create(
                user=customer,
                tx_type='refund',
                amount=order.total_price,
                balance_before=balance_before,
                balance_after=customer.balance,
                status='completed',
                provider='balance',
                description=f"Rad etilgan buyurtma #{order.order_number} uchun qaytarildi",
            )
            refunded = order.total_price

        order.status = OrderStatus.CANCELLED
        order.cancel_reason = reason
        if refunded:
            order.payment_status = 'refunded'
        order.save(update_fields=['status', 'cancel_reason', 'payment_status', 'updated_at'])

        # ── Referral cashback reversal ──
        # If this order earned the referrer cashback, take it back — the
        # platform never pays cashback for a cancelled order.
        from apps.users.referral_service import reverse_referral_cashback
        reverse_referral_cashback(order)

        AuditLog.objects.create(
            user=request.user,
            action='telegram_order_rejected',
            target_type='Order',
            target_id=order.id,
            description=(
                f"#{order.order_number} rad etildi ({reason})"
                + (f" — {refunded} so'm qaytarildi" if refunded else '')
            ),
        )

    # Mijozga Telegram xabar
    try:
        from apps.users.telegram_notify import notify_order_status
        notify_order_status(order, 'processing', 'cancelled')
    except Exception:
        logger.exception('reject notify failed for order %s', order.id)

    return Response({
        'ok': True,
        'detail': (
            f"Buyurtma rad etildi. {refunded} so'm mijoz balansiga qaytarildi"
            if refunded else 'Buyurtma rad etildi'
        ),
        'refunded': float(refunded),
        'order': OrderDetailSerializer(order).data,
    })


urlpatterns = [
    path('orders/', AdminOrderListView.as_view(), name='admin-orders-list'),
    path('orders/available/', operator_available_orders, name='operator-available-orders'),
    path('orders/<int:pk>/', AdminOrderDetailView.as_view(), name='admin-orders-detail'),
    path('orders/<int:pk>/accept/', operator_accept_order, name='operator-accept-order'),
    path('telegram-orders/', admin_telegram_orders, name='admin-telegram-orders'),
    path('telegram-orders/<int:pk>/confirm/', admin_telegram_confirm, name='admin-telegram-confirm'),
    path('telegram-orders/<int:pk>/reject/', admin_telegram_reject, name='admin-telegram-reject'),
    path('operator/dashboard/', operator_dashboard, name='operator-dashboard'),
    path('operator/stats/', operator_stats, name='operator-stats'),
]
