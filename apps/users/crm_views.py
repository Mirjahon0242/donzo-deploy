"""
Professional CRM dashboard statistics for the admin panel.

GET /api/v1/admin/crm/stats/?period=daily|weekly|monthly|yearly

Returns KPI cards + chart datasets consumed by the recharts-powered
admin dashboard (admin/page.tsx).
"""
from datetime import timedelta

from django.db.models import Avg, Count, F, Sum, ExpressionWrapper, DurationField
from django.db.models.functions import ExtractHour
from django.utils import timezone
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.orders.models import Order
from apps.payments.models import BalanceTransaction
from apps.users.models import User
from apps.users.permissions import IsAdmin, IsSuperAdmin
from apps.ws.metrics import metrics


def _sum(qs):
    return float(qs.aggregate(total=Sum('total_price'))['total'] or 0)


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsSuperAdmin])
def admin_reset_sales_stats(request):
    """
    Savdo statistikasini 0 ga keltirish (faqat Super Admin).

    Xuddi backend/reset_sales_stats.py kabi: Orders, Payments,
    BalanceTransaction va AuditLog'lar o'chiriladi; jonli WebSocket
    hisoblagichlar (metrics) ham nollanadi. Userlar, xizmatlar,
    paketlar va balanslarga TEGILMAYDI.

    Reset amali keyin AuditLog'ga yoziladi (kim, qachon, qancha o'chirildi).
    """
    from django.db import transaction

    from apps.audit_log.models import AuditLog
    from apps.payments.models import Payment

    counts = {
        'orders': Order.objects.count(),
        'payments': Payment.objects.count(),
        'balance_transactions': BalanceTransaction.objects.count(),
        'audit_logs': AuditLog.objects.count(),
    }

    # Hamma o'chirish bitta transaction ichida — qandaydir xato yuz bersa
    # DB yarim tozalangan holatda qolmaydi (all-or-nothing).
    with transaction.atomic():
        # In-memory WebSocket live counters har doim nollanadi.
        metrics.reset()

        # FK-safe tartibda o'chirish: payments (order FK) -> orders -> tranzaksiyalar -> loglar
        Payment.objects.all().delete()
        Order.objects.all().delete()
        BalanceTransaction.objects.all().delete()
        AuditLog.objects.all().delete()

        # Reset amali o'zi ham loglanadi (keyingi qayd sifatida qoladi).
        AuditLog.objects.create(
            user=request.user,
            action='sales_stats_reset',
            target_type='system',
            description=(
                f"Savdo statistikasi 0 ga keltirildi: {counts['orders']} buyurtma, "
                f"{counts['payments']} to'lov, {counts['balance_transactions']} tranzaksiya, "
                f"{counts['audit_logs']} log o'chirildi. Operator: @{request.user.username}"
            ),
        )

    return Response({
        'ok': True,
        'deleted': counts,
        'detail': 'Savdo statistikasi 0 ga keltirildi',
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_crm_stats(request):
    """
    Comprehensive CRM dashboard payload:
      - kpis:        ~20 metric cards
      - revenue:     chart series (daily / weekly / monthly / yearly switch)
      - orders:      line chart series
      - top_products: bar chart
      - top_users:    bar chart
      - payment_methods: pie chart
      - referral_growth: line chart
      - registrations: line chart
      - live:        real-time counters (WS-aware)
    """
    period = request.query_params.get('period', 'daily')
    today = timezone.now().date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)
    month_ago = today - timedelta(days=30)

    paid = Order.objects.filter(payment_status='paid')
    all_orders = Order.objects.all()

    # ── Revenue KPIs ──
    total_revenue = _sum(paid)
    today_revenue = _sum(paid.filter(created_at__date=today))
    yesterday_revenue = _sum(paid.filter(created_at__date=yesterday))
    week_revenue = _sum(paid.filter(created_at__date__gte=week_ago))
    month_revenue = _sum(paid.filter(created_at__date__gte=month_ago))

    # ── Order KPIs ──
    total_orders = all_orders.count()
    pending_orders = all_orders.filter(status='pending').count()
    processing_orders = all_orders.filter(status='processing').count()
    completed_orders = all_orders.filter(status='completed').count()
    cancelled_orders = all_orders.filter(status='cancelled').count()

    # ── User KPIs ──
    total_users = User.objects.count()
    active_users = User.objects.filter(is_active=True).count()
    new_users_today = User.objects.filter(created_at__date=today).count()
    blocked_users = User.objects.filter(is_active=False).count()
    blacklisted_users = User.objects.filter(is_blacklisted=True).count()
    online_users = metrics.get_active_connections()

    # ── Referral KPIs ──
    total_referrals = User.objects.filter(referred_by__isnull=False).count()
    referral_cashback = BalanceTransaction.objects.filter(
        tx_type='cashback', status='completed'
    ).aggregate(total=Sum('amount'))['total'] or 0

    # ── Quality KPIs ──
    avg_order_price = (total_revenue / total_orders) if total_orders else 0
    conversion_rate = ((paid.count() / total_orders) * 100) if total_orders else 0
    success_rate = ((completed_orders / total_orders) * 100) if total_orders else 0

    # Average processing time (completed orders only)
    duration_expr = ExpressionWrapper(
        F('updated_at') - F('created_at'),
        output_field=DurationField()
    )
    avg_duration = (
        Order.objects.filter(status='completed')
        .exclude(updated_at__lt=F('created_at'))
        .aggregate(avg=Avg(duration_expr))['avg']
    )
    avg_processing_minutes = round(avg_duration.total_seconds() / 60, 1) if avg_duration else 0

    # ── Revenue series by period ──
    revenue_series = []
    if period == 'daily':
        days = [(today - timedelta(days=i)) for i in range(29, -1, -1)]
        for d in days:
            revenue_series.append({
                'label': d.strftime('%d.%m'),
                'revenue': _sum(paid.filter(created_at__date=d)),
                'orders': all_orders.filter(created_at__date=d).count(),
            })
    elif period == 'weekly':
        for i in range(11, -1, -1):
            start = today - timedelta(days=7 * (i + 1)) + timedelta(days=1)
            end = start + timedelta(days=6)
            revenue_series.append({
                'label': f"{start.strftime('%d.%m')}-{min(end, today).strftime('%d.%m')}",
                'revenue': _sum(paid.filter(created_at__date__range=(start, end))),
                'orders': all_orders.filter(created_at__date__range=(start, end)).count(),
            })
    elif period == 'monthly':
        from django.db.models.functions import TruncMonth
        monthly = (
            paid.filter(created_at__date__gte=month_ago - timedelta(days=365))
            .annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(revenue=Sum('total_price'), orders=Count('id'))
            .order_by('month')
        )
        for m in monthly:
            revenue_series.append({
                'label': m['month'].strftime('%b %y') if m['month'] else '—',
                'revenue': float(m['revenue'] or 0),
                'orders': m['orders'],
            })
    else:  # yearly
        from django.db.models.functions import TruncYear
        yearly = (
            paid
            .annotate(year=TruncYear('created_at'))
            .values('year')
            .annotate(revenue=Sum('total_price'), orders=Count('id'))
            .order_by('year')
        )
        for y in yearly:
            revenue_series.append({
                'label': y['year'].strftime('%Y') if y['year'] else '—',
                'revenue': float(y['revenue'] or 0),
                'orders': y['orders'],
            })

    # ── Orders line (last 14 days) ──
    orders_series = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        orders_series.append({
            'label': d.strftime('%d.%m'),
            'created': all_orders.filter(created_at__date=d).count(),
            'completed': all_orders.filter(status='completed', created_at__date=d).count(),
        })

    # ── Top products (bar) ──
    top_products = list(
        all_orders
        .values('service__name')
        .annotate(orders=Count('id'), revenue=Sum('total_price'))
        .order_by('-orders')[:8]
    )

    # ── Top users (bar) ──
    top_users = list(
        Order.objects.filter(customer__isnull=False)
        .values('customer__username')
        .annotate(orders=Count('id'), spent=Sum('total_price'))
        .order_by('-spent')[:8]
    )

    # ── Payment methods (pie) ──
    payment_methods = list(
        all_orders
        .values('payment_method')
        .annotate(count=Count('id'), total=Sum('total_price'))
        .order_by('-count')
    )

    # ── Referral growth (line, cumulative last 14 days) ──
    referral_growth = []
    cumulative = 0
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        day_count = User.objects.filter(
            referred_by__isnull=False, created_at__date=d
        ).count()
        cumulative += day_count
        referral_growth.append({'label': d.strftime('%d.%m'), 'count': cumulative})

    # ── User registrations (line, last 14 days) ──
    registrations = []
    for i in range(13, -1, -1):
        d = today - timedelta(days=i)
        registrations.append({
            'label': d.strftime('%d.%m'),
            'registrations': User.objects.filter(created_at__date=d).count(),
        })

    # ── Hourly distribution (heatmap-style, last 30 days) ──
    hourly = (
        all_orders.filter(created_at__date__gte=month_ago)
        .annotate(hour=ExtractHour('created_at'))
        .values('hour')
        .annotate(count=Count('id'))
    )
    hourly_distribution = [0] * 24
    for h in hourly:
        if 0 <= (h['hour'] or 0) <= 23:
            hourly_distribution[h['hour']] = h['count']

    return Response({
        'kpis': {
            'total_revenue': total_revenue,
            'today_revenue': today_revenue,
            'yesterday_revenue': yesterday_revenue,
            'week_revenue': week_revenue,
            'month_revenue': month_revenue,
            'total_orders': total_orders,
            'pending_orders': pending_orders,
            'processing_orders': processing_orders,
            'completed_orders': completed_orders,
            'cancelled_orders': cancelled_orders,
            'paid_orders': paid.count(),
            'total_users': total_users,
            'active_users': active_users,
            'new_users_today': new_users_today,
            'blocked_users': blocked_users,
            'blacklisted_users': blacklisted_users,
            'online_users': online_users,
            'total_referrals': total_referrals,
            'referral_cashback': float(referral_cashback or 0),
            'avg_order_price': round(avg_order_price, 2),
            'conversion_rate': round(conversion_rate, 2),
            'success_rate': round(success_rate, 2),
            'avg_processing_minutes': avg_processing_minutes,
        },
        'charts': {
            'revenue': revenue_series,
            'orders': orders_series,
            'top_products': top_products,
            'top_users': top_users,
            'payment_methods': payment_methods,
            'referral_growth': referral_growth,
            'registrations': registrations,
            'hourly_distribution': hourly_distribution,
        },
    })
