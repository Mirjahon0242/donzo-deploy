from django.urls import path
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.db.models import Sum, Count, Q
from django.utils import timezone
from .models import AuditLog
from .serializers import AuditLogSerializer
from apps.users.permissions import IsAdmin
from apps.orders.models import Order
from datetime import timedelta


class AuditLogListView(generics.ListAPIView):
    queryset = AuditLog.objects.all().order_by('-created_at')
    serializer_class = AuditLogSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    search_fields = ['action', 'description']
    pagination_class = None

    def get_queryset(self):
        qs = super().get_queryset()

        # ?action=fragment_sync — faqat ma'lum turdagi amallar (Loglar sahifasi tablari).
        action = self.request.query_params.get('action')
        if action:
            qs = qs.filter(action=action)

        # ?q=... — tavsif/username bo'yicha qidiruv.
        q = (self.request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(
                Q(description__icontains=q)
                | Q(user__username__icontains=q)
                | Q(user__telegram_username__icontains=q)
            )

        limit = self.request.query_params.get('limit')
        if limit:
            try:
                qs = qs[:int(limit)]
            except ValueError:
                pass
        return qs


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_dashboard(request):
    today = timezone.now().date()
    week_ago = today - timedelta(days=7)

    today_orders = Order.objects.filter(created_at__date=today)
    week_orders = Order.objects.filter(created_at__date__gte=week_ago)

    # Revenue stats
    today_revenue = today_orders.aggregate(Sum('total_price'))['total_price__sum'] or 0
    week_revenue = week_orders.aggregate(Sum('total_price'))['total_price__sum'] or 0
    total_revenue = Order.objects.aggregate(Sum('total_price'))['total_price__sum'] or 0

    # Status breakdown
    status_counts = {
        status: Order.objects.filter(status=status).count()
        for status in ['pending', 'processing', 'completed', 'cancelled']
    }

    # Payment stats
    paid_orders = Order.objects.filter(payment_status='paid').count()
    unpaid_orders = Order.objects.filter(payment_status='unpaid').count()

    # Payment method breakdown
    payment_methods = {}
    for pm in Order.objects.values('payment_method').distinct():
        if pm['payment_method']:
            count = Order.objects.filter(payment_method=pm['payment_method']).count()
            payment_methods[pm['payment_method']] = count

    # Daily stats for the week (chart data)
    daily_stats = []
    for i in range(7):
        day = today - timedelta(days=6 - i)
        day_orders = Order.objects.filter(created_at__date=day)
        daily_stats.append({
            'date': day.isoformat(),
            'orders': day_orders.count(),
            'revenue': float(day_orders.aggregate(Sum('total_price'))['total_price__sum'] or 0),
        })

    # Top services
    top_services = (
        Order.objects.values('service__name', 'service__id')
        .annotate(count=Count('id'), revenue=Sum('total_price'))
        .order_by('-count')[:5]
    )

    # Operator stats (users who are operators or admins)
    from apps.users.models import User
    operator_stats = []
    operators = User.objects.filter(
        role__in=['operator', 'senior_operator', 'admin', 'super_admin']
    )
    for op in operators:
        completed = Order.objects.filter(assigned_operator=op, status='completed').count()
        total_assigned = Order.objects.filter(assigned_operator=op).count()
        if total_assigned > 0:
            operator_stats.append({
                'id': op.id,
                'username': op.username,
                'role': op.role,
                'completed': completed,
                'total_assigned': total_assigned,
                'completion_rate': round(completed / total_assigned * 100, 1),
            })

    return Response({
        # Today
        'today_orders': today_orders.count(),
        'today_revenue': float(today_revenue),
        'today_pending': today_orders.filter(status='pending').count(),
        'today_completed': today_orders.filter(status='completed').count(),

        # Status counts
        'pending_orders': status_counts['pending'],
        'processing_orders': status_counts['processing'],
        'completed_orders': status_counts['completed'],
        'cancelled_orders': status_counts['cancelled'],

        # Totals
        'total_orders': Order.objects.count(),
        'total_revenue': float(total_revenue),
        'week_revenue': float(week_revenue),
        'week_orders': week_orders.count(),

        # Payment
        'paid_orders': paid_orders,
        'unpaid_orders': unpaid_orders,
        'payment_methods': payment_methods,

        # Analytics
        'daily_stats': daily_stats,
        'top_services': [
            {
                'name': s['service__name'],
                'id': s['service__id'],
                'count': s['count'],
                'revenue': float(s['revenue'] or 0),
            }
            for s in top_services
        ],
        'operator_stats': operator_stats,

        # Recent orders
        'recent_orders': [
            {
                'id': o.id,
                'order_number': o.order_number,
                'customer_name': o.customer_name,
                'service_name': o.service.name if o.service else '',
                'total_price': float(o.total_price),
                'status': o.status,
                'payment_status': o.payment_status,
                'created_at': o.created_at.isoformat(),
            }
            for o in Order.objects.select_related('service').order_by('-created_at')[:10]
        ],
    })


urlpatterns = [
    path('logs/', AuditLogListView.as_view(), name='admin-logs-list'),
    path('dashboard/', admin_dashboard, name='admin-dashboard'),
    path('analytics/', admin_dashboard, name='admin-analytics'),
]
