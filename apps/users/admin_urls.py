from django.urls import path
from rest_framework import generics, permissions, filters, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django.db.models import Count, Sum, Q, F, Case, When, Value, DecimalField
from django.utils import timezone
from datetime import timedelta, date

from .models import User
from .serializers import UserSerializer, AdminUserSerializer
from .permissions import IsAdmin, IsSuperAdmin
from .referral_views import admin_referral_stats
from .crm_views import admin_crm_stats, admin_reset_sales_stats


class AdminUserListView(generics.ListAPIView):
    """
    List users (admin panel).

    SECURITY: this is a LIST-ONLY view. User CREATION goes exclusively
    through /admin/users/create/ (admin_create_user), which is guarded by
    IsSuperAdmin and validates roles — a ListCreateAPIView here would have
    let any admin POST a user with role=super_admin (privilege escalation).
    """
    # Annotate counts once per query to avoid N+1 on list views.
    queryset = User.objects.all().annotate(
        _orders_count=Count('orders', distinct=True),
        _referrals_count=Count('referrals', distinct=True),
    ).order_by('-created_at')
    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    search_fields = ['username', 'email', 'phone', 'telegram_username']
    filterset_fields = ['role', 'is_active', 'is_blacklisted']


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = User.objects.all()
    serializer_class = AdminUserSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def perform_update(self, serializer):
        """Log admin user edits to the audit trail."""
        from rest_framework.exceptions import PermissionDenied
        from .models import Role
        from .views import get_super_admin_telegram_id

        user = self.get_object()
        old_role = user.role
        old_balance = user.balance
        old_is_active = user.is_active
        old_is_blacklisted = user.is_blacklisted

        # ── SECURITY: role changes require SUPER_ADMIN ──
        new_role = serializer.validated_data.get('role', old_role)
        if new_role != old_role:
            if self.request.user.role != Role.SUPER_ADMIN:
                raise PermissionDenied("Rolni faqat Super Admin o'zgartira oladi")
            owner_tg = get_super_admin_telegram_id()
            # super_admin faqat egaga (owner telegram_id) tegishli
            if new_role == Role.SUPER_ADMIN and (not user.telegram_id or str(user.telegram_id) != owner_tg):
                raise PermissionDenied("super_admin roli faqat egaga (owner telegram_id) tegishli")
            # Egani super_admin holatidan tushirib bo'lmaydi
            if user.telegram_id and str(user.telegram_id) == owner_tg and new_role != Role.SUPER_ADMIN:
                raise PermissionDenied("Egasi super_admin holatidan tushirilishi mumkin emas")
            # is_staff/is_superuser flags sync
            STAFF_ROLES = ['admin', 'super_admin', 'senior_operator', 'operator', 'support']
            serializer.validated_data['is_staff'] = new_role in STAFF_ROLES
            serializer.validated_data['is_superuser'] = (new_role == Role.SUPER_ADMIN)

        # ── SECURITY: balance edits are a money operation ──
        # Only super admin may credit/debit a user's balance directly (a
        # regular admin granting themselves unlimited balance would be the
        # same free-money hole, one level up). Audit log below records it.
        new_balance = serializer.validated_data.get('balance', old_balance)
        if new_balance != old_balance and self.request.user.role != Role.SUPER_ADMIN:
            raise PermissionDenied("Balansni faqat Super Admin o'zgartira oladi")

        updated = serializer.save()

        # Build a description of what changed
        changes = []
        if old_role != updated.role:
            changes.append(f"role: {old_role} → {updated.role}")
        if old_balance != updated.balance:
            changes.append(f"balans: {old_balance} → {updated.balance} so'm")
        if old_is_active != updated.is_active:
            changes.append(f"holat: {'faol' if updated.is_active else 'bloklangan'}")
        if old_is_blacklisted != updated.is_blacklisted:
            changes.append(f"qora ro'yxat: {'qo\'shildi' if updated.is_blacklisted else 'olib tashlandi'}")

        from apps.audit_log.models import AuditLog
        if changes:
            description = f"Foydalanuvchi tahrirlandi: @{updated.username} — {'; '.join(changes)}"
        else:
            description = f"Foydalanuvchi tahrirlandi: @{updated.username}"

        AuditLog.objects.create(
            user=self.request.user,
            action='user_updated',
            target_type='User',
            target_id=user.id,
            description=description,
        )

    def perform_destroy(self, instance):
        """Log user deletion.

        SECURITY: only super admins may DELETE users (a regular admin could
        otherwise delete other admins / the owner). The owner account is
        always protected from deletion.
        """
        from rest_framework.exceptions import PermissionDenied
        from .models import Role
        from .views import get_super_admin_telegram_id

        if self.request.user.role != Role.SUPER_ADMIN:
            raise PermissionDenied("Foydalanuvchini faqat Super Admin o'chira oladi")
        # Never allow deleting the owner (super admin telegram id)
        if instance.telegram_id and str(instance.telegram_id) == get_super_admin_telegram_id():
            raise PermissionDenied("Egasi hisobini o'chirib bo'lmaydi")

        from apps.audit_log.models import AuditLog
        AuditLog.objects.create(
            user=self.request.user,
            action='user_deleted',
            target_type='User',
            target_id=instance.id,
            description=f"Foydalanuvchi o'chirildi: @{instance.username} ({instance.email})",
        )
        instance.delete()


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_fragment_sync_all(request):
    """
    POST /api/v1/admin/users/fragment-sync-all/

    Barcha foydalanuvchilarni (telegram_username'li) Fragment API getInfo
    orqali sinxronlashni background thread'da boshlaydi: ism, rasm va
    Telegram Premium holati yangilanadi. Javob darhol qaytadi, jarayon
    holati /admin/users/fragment-sync-status/ orqali kuzatiladi.
    """
    from .fragment_profile import sync_all_fragment_profiles

    total = sync_all_fragment_profiles()

    if total < 0:
        # Boshqa ommaviy sync hali ishlayapti — yangisini boshlamaymiz.
        return Response(
            {'status': 'already_running', 'detail': 'Ommaviy sinxronlash allaqachon ishlamoqda'},
            status=status.HTTP_409_CONFLICT,
        )

    from apps.audit_log.models import AuditLog
    AuditLog.objects.create(
        user=request.user,
        action='fragment_bulk_sync',
        target_type='User',
        description=f"Barcha mijozlar Fragment API bilan sinxronlash boshlandi ({total} ta)",
    )

    return Response({'status': 'started', 'total': total})


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_fragment_sync_status(request):
    """
    GET /api/v1/admin/users/fragment-sync-status/

    Ommaviy Fragment sinxronlash jarayonining joriy holati:
    running/total/updated/failed/skipped/started_at/finished_at.
    """
    from .fragment_profile import get_bulk_sync_status
    return Response(get_bulk_sync_status())


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_fragment_sync_user(request, pk):
    """
    POST /api/v1/admin/users/<pk>/fragment-sync/

    Foydalanuvchi profilini Fragment API (getInfo) orqali HOZIROQ
    yangilaydi (24 soatlik intervalni chetlab o'tadi): ism, foto,
    Telegram Premium holati. Background thread'da bajariladi.
    """
    from .fragment_profile import sync_fragment_profile

    try:
        user = User.objects.get(pk=pk)
    except User.DoesNotExist:
        return Response({'detail': 'Foydalanuvchi topilmadi'}, status=status.HTTP_404_NOT_FOUND)

    sync_fragment_profile(user, force=True)
    return Response({
        'status': 'ok',
        'detail': 'Fragment\'dan yangilash boshlandi (1-3 soniyada profilga yoziladi).',
        'telegram_username': user.telegram_username or '',
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_telegram_sessions(request):
    """
    GET /api/v1/admin/telegram-sessions/

    Recent Telegram Web App opens (metadata only — never initData/hash/token).
    Used by the admin 'Bot holati' page to show the last Web App logins.
    """
    from .models import TelegramWebAppSession
    limit = min(int(request.query_params.get('limit', 20)), 50)
    qs = (
        TelegramWebAppSession.objects
        .select_related('user')
        .order_by('-opened_at')[:limit]
    )
    return Response({
        'count': qs.count(),
        'results': [{
            'id': s.id,
            'telegram_id': s.telegram_id,
            'username': s.user.username if s.user else None,
            'first_name': s.user.first_name if s.user else None,
            'role': s.user.role if s.user else None,
            'is_authenticated': s.is_authenticated,
            'launch_source': s.launch_source,
            'opened_at': s.opened_at.isoformat(),
            'last_seen_at': s.last_seen_at.isoformat(),
            'user_agent': s.user_agent,
            'ip_address': s.ip_address,
            'error_code': s.error_code,
            'diag': s.diag,
        } for s in qs],
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_users_analytics(request):
    """
    GET /api/v1/admin/users/analytics/

    Returns user analytics: registrations over time, role distribution, etc.
    """
    today = timezone.now().date()
    thirty_days_ago = today - timedelta(days=30)

    # Total users by role
    role_distribution = (
        User.objects
        .values('role')
        .annotate(count=Count('id'))
        .order_by('role')
    )

    # New registrations (last 30 days)
    new_users_30d = User.objects.filter(created_at__date__gte=thirty_days_ago).count()

    # Daily registrations (last 7 days)
    daily_registrations = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        count = User.objects.filter(created_at__date=day).count()
        daily_registrations.append({
            'date': day.isoformat(),
            'registrations': count,
        })

    # Active vs inactive
    active_users = User.objects.filter(is_active=True).count()
    inactive_users = User.objects.filter(is_active=False).count()
    blacklisted_users = User.objects.filter(is_blacklisted=True).count()

    # Users with referral codes
    users_with_referrer = User.objects.filter(referred_by__isnull=False).count()

    return Response({
        'total_users': User.objects.count(),
        'role_distribution': list(role_distribution),
        'new_users_30d': new_users_30d,
        'new_users_today': User.objects.filter(created_at__date=today).count(),
        'daily_registrations': daily_registrations,
        'active_users': active_users,
        'inactive_users': inactive_users,
        'blacklisted_users': blacklisted_users,
        'users_with_referrer': users_with_referrer,
    })


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_analytics_dashboard(request):
    """
    GET /api/v1/admin/analytics/

    Comprehensive analytics dashboard data:
    - Revenue by provider
    - Revenue by service category
    - Order trends
    - User growth
    - Referral stats
    """
    from apps.orders.models import Order
    from apps.payments.models import Payment

    today = timezone.now().date()
    thirty_days_ago = today - timedelta(days=30)

    # Revenue by payment provider
    revenue_by_provider = (
        Payment.objects
        .filter(status='success', created_at__date__gte=thirty_days_ago)
        .values('provider')
        .annotate(
            total=Sum('amount'),
            count=Count('id'),
        )
        .order_by('-total')
    )

    # Revenue by service category
    revenue_by_category = (
        Order.objects
        .filter(payment_status='paid', created_at__date__gte=thirty_days_ago)
        .values('service__category__name')
        .annotate(
            total=Sum('total_price'),
            count=Count('id'),
        )
        .order_by('-total')
    )

    # Daily revenue (last 7 days)
    daily_revenue = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        rev = Order.objects.filter(
            payment_status='paid',
            created_at__date=day
        ).aggregate(total=Sum('total_price'))['total'] or 0
        orders = Order.objects.filter(created_at__date=day).count()
        daily_revenue.append({
            'date': day.isoformat(),
            'revenue': float(rev),
            'orders': orders,
        })

    # Referral stats
    total_referrals = User.objects.filter(referred_by__isnull=False).count()
    referral_revenue = Order.objects.filter(
        customer__referred_by__isnull=False,
        payment_status='paid'
    ).aggregate(total=Sum('total_price'))['total'] or 0

    return Response({
        'total_revenue_30d': float(Order.objects.filter(
            payment_status='paid',
            created_at__date__gte=thirty_days_ago
        ).aggregate(total=Sum('total_price'))['total'] or 0),
        'total_orders_30d': Order.objects.filter(
            created_at__date__gte=thirty_days_ago
        ).count(),
        'revenue_by_provider': list(revenue_by_provider),
        'revenue_by_category': list(revenue_by_category),
        'daily_revenue': daily_revenue,
        'referral_stats': {
            'total_referrals': total_referrals,
            'referral_revenue': float(referral_revenue),
        },
        'conversion_rate': float(Order.objects.filter(
            payment_status='paid'
        ).count() / max(Order.objects.count(), 1) * 100),
    })


STAFF_ROLES = ['admin', 'super_admin', 'senior_operator', 'operator', 'support']


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_role_holders(request):
    """
    GET /api/v1/admin/roles/

    List all users with staff roles (admin, operator, support, ...).
    Supports ?search=telegram_id|username|telegram_username filter.
    """
    qs = User.objects.filter(role__in=STAFF_ROLES).order_by('role', 'username')
    search = request.query_params.get('search', '').strip()
    if search:
        qs = qs.filter(
            Q(telegram_id__icontains=search)
            | Q(username__icontains=search)
            | Q(telegram_username__icontains=search)
            | Q(email__icontains=search)
        )
    return Response({
        'count': qs.count(),
        'results': AdminUserSerializer(qs, many=True).data,
    })


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsSuperAdmin])
def admin_set_role(request):
    """
    POST /api/v1/admin/roles/set/

    Grant/change a staff role for a user identified by telegram_id OR
    telegram username OR platform username.
    Body: { "telegram_id": "123456789", "username": "@nick" | "nick", "role": "operator" }

    The user will then be able to log in via their own Telegram account
    and automatically get access to their role panel.
    """
    telegram_id = (request.data.get('telegram_id') or '').strip()
    username = (request.data.get('username') or '').strip().lstrip('@')
    role = (request.data.get('role') or '').strip()

    # 'customer' = rolni olib tashlash (revoke). Boshqa rollar STAFF_ROLES ichida.
    allowed = STAFF_ROLES + ['customer']
    if role not in allowed:
        return Response(
            {'detail': f"Noto'g'ri rol. Ruxsat etilganlar: {', '.join(allowed)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not telegram_id and not username:
        return Response(
            {'detail': 'telegram_id yoki username ko\'rsatilishi shart'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = None
    if telegram_id:
        user = User.objects.filter(telegram_id=telegram_id).first()
    if user is None and username:
        user = (
            User.objects.filter(telegram_username=username).first()
            or User.objects.filter(username=username).first()
        )

    if user is None:
        return Response(
            {'detail': 'Foydalanuvchi topilmadi. Avval u Telegram orqali kamida bir marta kirmishi kerak.'},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Protect the owner super admin from being demoted
    from .views import get_super_admin_telegram_id
    owner_tg = get_super_admin_telegram_id()
    if (user.telegram_id == owner_tg and role != 'super_admin'):
        return Response(
            {'detail': 'Egasi super_admin holatidan tushirilishi mumkin emas'},
            status=status.HTTP_403_FORBIDDEN,
        )

    # super_admin rolini faqat ega (owner) olishi mumkin — boshqasiga berilmaydi
    if role == 'super_admin' and user.telegram_id != owner_tg:
        return Response(
            {'detail': 'super_admin roli faqat egaga (owner telegram_id) tegishli'},
            status=status.HTTP_403_FORBIDDEN,
        )

    old_role = user.role
    user.role = role
    # is_staff/is_superuser faqat tegishli rollar uchun; customer'ga qaytarilsa (revoke) False.
    user.is_staff = role in STAFF_ROLES
    user.is_superuser = (role == 'super_admin')
    user.save(update_fields=['role', 'is_staff', 'is_superuser'])

    from apps.audit_log.models import AuditLog
    AuditLog.objects.create(
        user=request.user,
        action='role_changed',
        target_type='User',
        target_id=user.id,
        description=(
            f"Rol o'zgartirildi: @{user.username} "
            f"(telegram_id={user.telegram_id or '-'}) {old_role} → {role}"
        ),
    )

    return Response(AdminUserSerializer(user).data)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_role_search(request):
    """
    GET /api/v1/admin/roles/search/?q=...

    Search any user by telegram_id or username (to attach a role to).
    """
    q = (request.query_params.get('q') or '').strip()
    if not q:
        return Response({'results': []})
    qs = User.objects.filter(
        Q(telegram_id__icontains=q)
        | Q(username__icontains=q.lstrip('@'))
        | Q(telegram_username__icontains=q.lstrip('@'))
    ).order_by('-created_at')[:20]
    return Response({'results': AdminUserSerializer(qs, many=True).data})


@api_view(['POST'])
@permission_classes([permissions.IsAuthenticated, IsSuperAdmin])
def admin_create_user(request):
    """
    POST /api/v1/admin/users/create/

    Super admin can pre-create a user record by Telegram ID (the platform
    is Telegram-only — no login/password anymore). The user then logs in
    with their own Telegram account.
    Body: { username, telegram_id, email?, role?, phone?, is_active? }
    """
    import uuid

    username = (request.data.get('username') or '').strip()
    telegram_id = (request.data.get('telegram_id') or '').strip()

    if not username:
        return Response({'detail': 'username majburiy'}, status=status.HTTP_400_BAD_REQUEST)
    if not telegram_id:
        return Response(
            {'detail': 'telegram_id majburiy — platforma faqat Telegram orqali ishlaydi'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if User.objects.filter(telegram_id=telegram_id).exists():
        return Response(
            {'detail': f"Bu telegram_id ({telegram_id}) allaqachon ro'yxatda"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Validate role against the model choices (same guard as admin_set_role)
    valid_roles = [r[0] for r in User._meta.get_field('role').choices]
    role = request.data.get('role', 'customer')
    if role not in valid_roles:
        return Response(
            {'detail': f"Noto'g'ri rol. Ruxsat etilganlar: {', '.join(valid_roles)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # super_admin faqat egaga (owner telegram_id) tegishli
    from .views import get_super_admin_telegram_id
    if role == 'super_admin' and telegram_id != get_super_admin_telegram_id():
        return Response(
            {'detail': 'super_admin roli faqat egaga (owner telegram_id) tegishli'},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Unique username/email guards (username unique=True, email unique=True)
    if User.objects.filter(username=username).exists():
        username = f'{username}_{uuid.uuid4().hex[:6]}'
    email = (request.data.get('email') or f'{telegram_id}@telegram.user').strip()
    if User.objects.filter(email=email).exists():
        email = f'{telegram_id}.{uuid.uuid4().hex[:6]}@telegram.user'

    user = User.objects.create(
        username=username,
        email=email,
        phone=(request.data.get('phone') or '').strip() or None,
        telegram_id=telegram_id,
        referral_code=uuid.uuid4().hex[:10].upper(),
        role=role,
        is_active=bool(request.data.get('is_active', True)),
    )

    from apps.audit_log.models import AuditLog
    AuditLog.objects.create(
        user=request.user,
        action='user_created',
        target_type='User',
        target_id=user.id,
        description=f"Yangi foydalanuvchi yaratildi: @{user.username} (tg_id={telegram_id}) roli: {user.role}",
    )

    return Response(AdminUserSerializer(user).data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated, IsAdmin])
def admin_user_profile(request, pk):
    """
    GET /api/v1/admin/users/<pk>/profile/

    Full customer profile for the admin panel — everything about one user
    in a single payload:
      - user            (AdminUserSerializer)
      - summary         KPI cards (total spent, orders by status, referrals)
      - orders          recent orders (service, package, status, price)
      - transactions    recent balance transactions (topup/purchase/...)
      - activity        monthly orders+revenue series + top services

    SECURITY: IsAdmin-guarded, same as the rest of the admin user API.
    """
    from apps.orders.models import Order
    from apps.payments.models import BalanceTransaction
    from django.db.models.functions import TruncMonth

    try:
        user = User.objects.get(pk=pk)
    except User.DoesNotExist:
        return Response({'detail': 'Foydalanuvchi topilmadi'}, status=status.HTTP_404_NOT_FOUND)

    orders_qs = Order.objects.filter(customer=user).select_related('service', 'package')
    paid = orders_qs.filter(payment_status='paid')

    total_spent = float(paid.aggregate(s=Sum('total_price'))['s'] or 0)
    total_orders = orders_qs.count()

    # ── Monthly activity (last 6 months) ──
    # Orders = ALL orders that month; revenue = only paid ones.
    today = timezone.now().date()
    # Aniq 6 oy orqaga: shu oyning 1-kunidan 5 oy oldingi oyning 1-kuni.
    _total_months = today.year * 12 + (today.month - 1) - 5
    months_ago = date(_total_months // 12, _total_months % 12 + 1, 1)
    monthly = (
        orders_qs.filter(created_at__date__gte=months_ago)
        .annotate(month=TruncMonth('created_at'))
        .values('month')
        .annotate(
            orders=Count('id'),
            revenue=Sum(Case(
                When(payment_status='paid', then=F('total_price')),
                default=Value(0),
                output_field=DecimalField(max_digits=15, decimal_places=2),
            )),
        )
        .order_by('month')
    )
    activity = []
    for m in monthly:
        activity.append({
            'label': m['month'].strftime('%b %y') if m['month'] else '—',
            'revenue': float(m['revenue'] or 0),
            'orders': m['orders'],
        })

    # ── Top services by order count ──
    top_services = list(
        orders_qs
        .values('service__name')
        .annotate(count=Count('id'), revenue=Sum('total_price'))
        .order_by('-count')[:5]
    )

    # ── Recent orders ──
    recent_orders = [
        {
            'id': o.id,
            'order_number': o.order_number,
            'service_name': o.service.name if o.service else '',
            'package_name': o.package.name if o.package else '',
            'status': o.status,
            'payment_status': o.payment_status,
            'payment_method': o.payment_method,
            'total_price': float(o.total_price),
            'created_at': o.created_at.isoformat(),
        }
        for o in orders_qs[:20]
    ]

    # ── Recent balance transactions ──
    recent_transactions = [
        {
            'id': t.id,
            'tx_type': t.tx_type,
            'amount': float(t.amount),
            'status': t.status,
            'description': t.description,
            'created_at': t.created_at.isoformat(),
        }
        for t in BalanceTransaction.objects.filter(user=user)[:20]
    ]

    # ── Referral data ──
    referral_earnings = float(BalanceTransaction.objects.filter(
        user=user, tx_type='cashback', status='completed'
    ).aggregate(s=Sum('amount'))['s'] or 0)
    first_order = orders_qs.order_by('created_at').first()
    last_order = orders_qs.order_by('-created_at').first()

    summary = {
        'total_spent': total_spent,
        'total_orders': total_orders,
        'completed_orders': orders_qs.filter(status='completed').count(),
        'pending_orders': orders_qs.filter(status='pending').count(),
        'processing_orders': orders_qs.filter(status='processing').count(),
        'cancelled_orders': orders_qs.filter(status='cancelled').count(),
        'avg_order_price': round(total_spent / total_orders, 2) if total_orders else 0,
        'referrals_count': user.referrals.count(),
        'referral_earnings': referral_earnings,
        'cashback_balance': float(user.cashback_balance or 0),
        'first_order_at': first_order.created_at.isoformat() if first_order else None,
        'last_order_at': last_order.created_at.isoformat() if last_order else None,
        'top_service': top_services[0]['service__name'] if top_services else None,
    }

    return Response({
        'user': AdminUserSerializer(user).data,
        'summary': summary,
        'orders': recent_orders,
        'transactions': recent_transactions,
        'activity': {
            'monthly': activity,
            'top_services': top_services,
        },
    })


urlpatterns = [
    path('telegram-sessions/', admin_telegram_sessions, name='admin-telegram-sessions'),
    path('users/', AdminUserListView.as_view(), name='admin-users-list'),
    path('users/create/', admin_create_user, name='admin-users-create'),
    path('users/analytics/', admin_users_analytics, name='admin-users-analytics'),
    path('users/<int:pk>/profile/', admin_user_profile, name='admin-users-profile'),
    path('users/<int:pk>/', AdminUserDetailView.as_view(), name='admin-users-detail'),
    path('users/<int:pk>/fragment-sync/', admin_fragment_sync_user, name='admin-users-fragment-sync'),
    path('users/fragment-sync-all/', admin_fragment_sync_all, name='admin-users-fragment-sync-all'),
    path('users/fragment-sync-status/', admin_fragment_sync_status, name='admin-users-fragment-sync-status'),
    path('roles/', admin_role_holders, name='admin-role-holders'),
    path('roles/set/', admin_set_role, name='admin-set-role'),
    path('roles/search/', admin_role_search, name='admin-role-search'),
    path('analytics/', admin_analytics_dashboard, name='admin-analytics'),
    path('crm/stats/', admin_crm_stats, name='admin-crm-stats'),
    path('crm/reset-stats/', admin_reset_sales_stats, name='admin-crm-reset-stats'),
    path('referrals/stats/', admin_referral_stats, name='admin-referral-stats'),
]
