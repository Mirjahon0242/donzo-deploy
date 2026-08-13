"""
Admin panel views for card payment monitoring (DONZO).

Mounted under /api/v1/admin/cardpay/... — admin only.
"""
from decimal import Decimal

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.users.permissions import IsAdmin
from apps.settings_app.models import Setting
from .models import CardPaymentMessage, CardTopupRequest, PaymentCard, SuspiciousPayment
from . import services


def _user_dict(u):
    if u is None:
        return None
    return {
        'id': u.id,
        'username': u.username,
        'telegram_username': u.telegram_username,
        'telegram_id': u.telegram_id,
        'balance': str(u.balance),
    }


def _req_dict(r):
    return {
        'id': r.id,
        'user': _user_dict(r.user),
        'requested_amount': str(r.requested_amount),
        'unique_amount': str(r.unique_amount),
        'expires_at': r.expires_at.isoformat(),
        'status': r.status,
        'paid_at': r.paid_at.isoformat() if r.paid_at else None,
        'created_at': r.created_at.isoformat(),
        'is_expired': r.is_expired,
    }


def _msg_dict(m):
    return {
        'id': m.id,
        'chat_id': m.chat_id,
        'message_id': m.message_id,
        'parsed_amounts': m.parsed_amounts,
        'raw_text': (m.raw_text or '')[:300],
        'received_at': m.received_at.isoformat(),
        'outcome': m.outcome,
    }


def _sp_dict(sp):
    return {
        'id': sp.id,
        'user': _user_dict(sp.user),
        'amount': str(sp.amount),
        'status': sp.status,
        'note': sp.note,
        'created_at': sp.created_at.isoformat(),
        'decided_at': sp.decided_at.isoformat() if sp.decided_at else None,
        'decided_by': sp.decided_by.username if sp.decided_by else None,
        'request_id': sp.topup_request_id,
    }


class CardpaySettingsView(APIView):
    """GET/PUT /api/v1/admin/cardpay/settings/ — payment monitor config."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    KEYS = [
        services.K_MONITOR_CHAT, services.K_REPORT_CHAT, services.K_LIMIT,
        services.K_TIMEOUT, services.K_OFFSET, services.K_CARD_NUMBER,
        services.K_CARD_HOLDER, services.K_ENABLED,
    ]

    def get(self, request):
        return Response(services.get_settings())

    def put(self, request):
        data = dict(request.data or {})
        for key in self.KEYS:
            if key in data:
                Setting.set_setting(key, str(data[key]))
        return Response(services.get_settings())


def _clean_data(request):
    """request.data → plain dict (QueryDict values are lists — normalize)."""
    data = request.data
    if data is None:
        return {}
    if hasattr(data, 'dict'):
        return data.dict()
    if isinstance(data, dict):
        return data
    return {}


def _to_decimal(value, default=0):
    try:
        if value in (None, ''):
            return Decimal(default)
        return Decimal(str(value).strip())
    except Exception:
        return Decimal(default)


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value in (None, ''):
        return default
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on', 'ha')
    return bool(value)


def _card_dict(c):
    """Serializable PaymentCard view (with usage % for progress bars)."""
    def _pct(used, limit):
        if not limit:
            return None
        return round(min(100, float(used) / float(limit) * 100), 1)
    return {
        'id': c.id,
        'card_number': c.card_number,
        'card_tail': c.card_tail,
        'card_holder': c.card_holder,
        'bank_name': c.bank_name,
        'enabled': c.enabled,
        'is_active': c.is_active,
        'max_amount': str(c.max_amount),
        'max_transfers': c.max_transfers,
        'total_amount': str(c.total_amount),
        'transfers_count': c.transfers_count,
        'amount_usage_pct': _pct(c.total_amount, c.max_amount),
        'transfer_usage_pct': _pct(c.transfers_count, c.max_transfers),
        'is_exhausted': c.is_exhausted,
        'auto_reset_daily': c.auto_reset_daily,
        'period_started_at': c.period_started_at.isoformat(),
        'last_switch_at': c.last_switch_at.isoformat() if c.last_switch_at else None,
        'order_index': c.order_index,
        'created_at': c.created_at.isoformat(),
    }


class PaymentCardListView(APIView):
    """GET/POST /api/v1/admin/cardpay/cards/ — card registry + create."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        cards = PaymentCard.objects.all()
        return Response({
            'cards': [_card_dict(c) for c in cards],
            'active_card': _card_dict(cards.filter(is_active=True).first()) if cards.filter(is_active=True).exists() else None,
            'settings': services.get_settings(),
        })

    def post(self, request):
        data = _clean_data(request)
        number = str(data.get('card_number') or '').strip().replace(' ', '')
        if len(number) < 12:
            return Response({'detail': 'Karta raqami noto‘g‘ri (kamida 12 raqam)'}, status=status.HTTP_400_BAD_REQUEST)
        if PaymentCard.objects.filter(card_number=number).exists():
            return Response({'detail': 'Bu karta raqami allaqachon qo‘shilgan'}, status=status.HTTP_400_BAD_REQUEST)

        make_active = _to_bool(data.get('is_active')) or not PaymentCard.objects.filter(enabled=True).exists()
        card = PaymentCard.objects.create(
            card_number=number,
            card_holder=str(data.get('card_holder') or '').strip(),
            bank_name=str(data.get('bank_name') or '').strip(),
            enabled=_to_bool(data.get('enabled'), True),
            is_active=make_active,
            max_amount=_to_decimal(data.get('max_amount')),
            max_transfers=_to_int(data.get('max_transfers')),
            auto_reset_daily=_to_bool(data.get('auto_reset_daily'), True),
            order_index=_to_int(data.get('order_index')),
        )
        if make_active:
            # Save-time guard keeps only one active — re-persist to be safe.
            PaymentCard.objects.filter(is_active=True).exclude(pk=card.pk).update(is_active=False)
        services._audit('card_created', request.user, f"Yangi karta qo‘shildi: ***{card.card_tail} ({card.card_holder or '—'})")
        return Response(_card_dict(card), status=status.HTTP_201_CREATED)


class PaymentCardDetailView(APIView):
    """GET/PATCH/DELETE /api/v1/admin/cardpay/cards/<id>/"""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def _get(self, pk):
        try:
            return PaymentCard.objects.get(pk=pk)
        except PaymentCard.DoesNotExist:
            return None

    def get(self, request, pk):
        card = self._get(pk)
        if card is None:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        return Response(_card_dict(card))

    def patch(self, request, pk):
        card = self._get(pk)
        if card is None:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        data = _clean_data(request)
        if 'card_number' in data:
            number = str(data['card_number'] or '').strip().replace(' ', '')
            if len(number) < 12:
                return Response({'detail': 'Karta raqami noto‘g‘ri'}, status=status.HTTP_400_BAD_REQUEST)
            if PaymentCard.objects.filter(card_number=number).exclude(pk=card.pk).exists():
                return Response({'detail': 'Bu karta raqami allaqachon mavjud'}, status=status.HTTP_400_BAD_REQUEST)
            card.card_number = number
        for f in ('card_holder', 'bank_name'):
            if f in data:
                setattr(card, f, str(data[f] or '').strip())
        if 'enabled' in data:
            card.enabled = _to_bool(data['enabled'])
        if 'is_active' in data and _to_bool(data['is_active']):
            card.is_active = True
        if 'max_amount' in data:
            card.max_amount = _to_decimal(data['max_amount'])
        if 'max_transfers' in data:
            card.max_transfers = _to_int(data['max_transfers'])
        if 'auto_reset_daily' in data:
            card.auto_reset_daily = _to_bool(data['auto_reset_daily'])
        if 'order_index' in data:
            card.order_index = _to_int(data['order_index'])
        card.save()
        return Response(_card_dict(card))

    def delete(self, request, pk):
        card = self._get(pk)
        if card is None:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        was_active = card.is_active
        card.delete()
        if was_active:
            # Keep the platform usable: activate the next enabled card.
            services.rotate_active_card()
        services._audit('card_deleted', request.user, f"Karta o‘chirildi: ***{card.card_tail}")
        return Response({'detail': 'Karta o‘chirildi'}, status=status.HTTP_200_OK)


class PaymentCardActivateView(APIView):
    """POST /api/v1/admin/cardpay/cards/<id>/activate/ — make a card active."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        try:
            card = PaymentCard.objects.get(pk=pk)
        except PaymentCard.DoesNotExist:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        if not card.enabled:
            card.enabled = True
        card.is_active = True
        card.save()
        services._audit('card_activated', request.user, f"Karta faollashtirildi: ***{card.card_tail}")
        return Response(_card_dict(card))


class PaymentCardResetView(APIView):
    """POST /api/v1/admin/cardpay/cards/<id>/reset/ — reset usage counters."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request, pk):
        try:
            card = PaymentCard.objects.get(pk=pk)
        except PaymentCard.DoesNotExist:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        card.total_amount = 0
        card.transfers_count = 0
        card.period_started_at = timezone.now()
        card.save(update_fields=['total_amount', 'transfers_count', 'period_started_at', 'updated_at'])
        services._audit('card_counters_reset', request.user, f"Karta hisoblagichlari tiklandi: ***{card.card_tail}")
        return Response(_card_dict(card))


class CardpayRequestsView(APIView):
    """GET /api/v1/admin/cardpay/requests/?status=pending|paid|expired|cancelled|all"""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = CardTopupRequest.objects.select_related('user', 'balance_tx')
        st = request.query_params.get('status', 'all')
        if st and st != 'all':
            qs = qs.filter(status=st)
        qs = qs.order_by('-created_at')[:200]
        data = [_req_dict(r) for r in qs]
        counts = {c: CardTopupRequest.objects.filter(status=c).count()
                  for c in ('pending', 'paid', 'expired', 'cancelled')}
        return Response({'counts': counts, 'results': data})


class CardpayMessagesView(APIView):
    """GET /api/v1/admin/cardpay/messages/?outcome=...&limit=50"""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = CardPaymentMessage.objects.all()
        outcome = request.query_params.get('outcome')
        if outcome:
            qs = qs.filter(outcome=outcome)
        limit = min(int(request.query_params.get('limit', 50)), 200)
        qs = qs.order_by('-received_at')[:limit]
        return Response({'results': [_msg_dict(m) for m in qs]})


class SuspiciousListView(APIView):
    """GET /api/v1/admin/cardpay/suspicious/?status=pending|approved|rejected|all"""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        qs = SuspiciousPayment.objects.select_related('user', 'decided_by', 'topup_request')
        st = request.query_params.get('status', 'pending')
        if st and st != 'all':
            qs = qs.filter(status=st)
        data = [_sp_dict(sp) for sp in qs.order_by('-created_at')[:200]]
        counts = {c: SuspiciousPayment.objects.filter(status=c).count()
                  for c in ('pending', 'approved', 'rejected')}
        return Response({'counts': counts, 'results': data})


class SuspiciousActionView(APIView):
    """POST /api/v1/admin/cardpay/suspicious/<id>/approve/ | /reject/"""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request, pk, action=None):
        action = action or self.kwargs.get('action')
        try:
            sp = SuspiciousPayment.objects.get(pk=pk)
        except SuspiciousPayment.DoesNotExist:
            return Response({'detail': 'Topilmadi'}, status=status.HTTP_404_NOT_FOUND)
        if action == 'approve':
            result = services.approve_suspicious(sp.id, request.user)
        else:
            result = services.reject_suspicious(
                sp.id, request.user, note=request.data.get('note', '') or '')
        if not result.get('ok'):
            return Response({'detail': result.get('detail', 'Xatolik')},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(result)


class UserClientStatusView(APIView):
    """
    GET /api/v1/admin/cardpay/userclient/status/

    Telethon user client login holati: authorized, phone/username (masked by
    design — no secrets), worker heartbeat.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        from . import user_client_auth
        return Response(user_client_auth.get_status())


class UserClientAuthStartView(APIView):
    """
    POST /api/v1/admin/cardpay/userclient/start/  {phone}

    Telegram akkauntga kod yuboradi (send_code_request).
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'telegram_auth'  # reuse: 20/min per IP

    def post(self, request):
        from . import user_client_auth
        phone = (request.data.get('phone') or '').strip()
        result = user_client_auth.start_phone(phone)
        code = status.HTTP_200_OK if result.get('ok') else status.HTTP_400_BAD_REQUEST
        return Response(result, status=code)


class UserClientAuthVerifyView(APIView):
    """
    POST /api/v1/admin/cardpay/userclient/verify/  {code}

    Tasdiqlash kodi bilan sign_in. 2FA bo'lsa needs_password=true qaytadi.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    # Brute-force protection on the code itself.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'telegram_code_login'  # reuse: 10/min per IP

    def post(self, request):
        from . import user_client_auth
        code = (request.data.get('code') or '').strip()
        result = user_client_auth.verify_code(code)
        # needs_password xato emas — kod TO'G'RI, keyingi qadam (2FA parol).
        # 400 bo'lsa frontend catch'ga tushib 'kod tekshirilmadi' degan noto'g'ri
        # xabar ko'rsatardi — 200 qaytarib oqimni aniq qilamiz.
        code_ = status.HTTP_200_OK if (result.get('ok') or result.get('needs_password')) else status.HTTP_400_BAD_REQUEST
        return Response(result, status=code_)


class UserClientAuthPasswordView(APIView):
    """POST /api/v1/admin/cardpay/userclient/password/  {password} — 2FA step."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'telegram_code_login'  # 10/min per IP

    def post(self, request):
        from . import user_client_auth
        password = request.data.get('password') or ''
        result = user_client_auth.verify_password(password)
        code = status.HTTP_200_OK if result.get('ok') else status.HTTP_400_BAD_REQUEST
        return Response(result, status=code)


class UserClientLogoutView(APIView):
    """POST /api/v1/admin/cardpay/userclient/logout/ — sessionni o'chiradi."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request):
        from . import user_client_auth
        return Response(user_client_auth.logout())


class UserClientDetailView(APIView):
    """GET /api/v1/admin/cardpay/userclient/detail/

    One call for the admin "User Client" page: login status + cardpay
    settings + API credentials (masked) + supervisor log tail.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        from . import user_client_auth

        def _mask(v: str) -> str:
            v = v or ''
            if len(v) <= 4:
                return '••••' if v else ''
            return v[:2] + '•' * (len(v) - 4) + v[-2:]

        api_id = (Setting.get_setting('telegram_api_id', '') or '').strip()
        api_hash = (Setting.get_setting('telegram_api_hash', '') or '').strip()
        status_ = user_client_auth.get_status()
        return Response({
            'status': status_,
            'settings': services.get_settings(),
            'api_id': _mask(api_id),
            'api_id_set': bool(api_id),
            'api_hash_set': bool(api_hash),
            'log': user_client_auth.read_supervisor_log(40),
        })


class UserClientMonitorCheckView(APIView):
    """POST /api/v1/admin/cardpay/userclient/monitor-check/

    Sessiya bilan hozirgi monitor chatni topishga harakat qiladi —
    "Monitor chat topilmadi" muammosini panelda jonli tekshirish.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request):
        from . import user_client_auth
        return Response(user_client_auth.resolve_monitor_chat())


class UserClientRestartView(APIView):
    """POST /api/v1/admin/cardpay/userclient/restart/

    Worker jarayonini o'ldiradi — supervisor uni yangi sessiya bilan
    qayta ishga tushiradi (~5s ichida).
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request):
        from . import user_client_auth
        user_client_auth._restart_worker()
        return Response({'ok': True, 'detail': 'Worker qayta ishga tushirilmoqda (supervisor ~5s)'})


class UserClientApiKeysView(APIView):
    """GET/PUT /api/v1/admin/cardpay/userclient/api-keys/

    telegram_api_id / telegram_api_hash (my.telegram.org dan) — user client
    login uchun zarur. GET qaytaradi: faqat o'rnatilganligi (masked).
    PUT {telegram_api_id, telegram_api_hash} — yangilaydi.
    """

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        api_id = (Setting.get_setting('telegram_api_id', '') or '').strip()
        api_hash = (Setting.get_setting('telegram_api_hash', '') or '').strip()
        return Response({
            'telegram_api_id_set': bool(api_id),
            'telegram_api_hash_set': bool(api_hash),
            'telegram_api_id_masked': (api_id[:2] + '••••' + api_id[-2:]) if len(api_id) > 4 else ('••••' if api_id else ''),
        })

    def put(self, request):
        api_id = (request.data.get('telegram_api_id') or '').strip()
        api_hash = (request.data.get('telegram_api_hash') or '').strip()
        if api_id:
            Setting.set_setting('telegram_api_id', api_id)
        if api_hash:
            Setting.set_setting('telegram_api_hash', api_hash)
        return Response({'ok': True, 'detail': 'API kalitlar saqlandi'})


class CardpayStatusView(APIView):
    """GET /api/v1/admin/cardpay/status/ — listener heartbeat + today summary."""

    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        from django.db.models import Sum
        today = timezone.now().date()
        paid_today = CardTopupRequest.objects.filter(status='paid', paid_at__date=today)
        total_today = paid_today.aggregate(t=Sum('unique_amount'))['t'] or 0
        pending = CardTopupRequest.objects.filter(status='pending').count()
        suspicious_pending = SuspiciousPayment.objects.filter(status='pending').count()
        return Response({
            'enabled': services.get_settings()['enabled'],
            'listener_online': services._user_client_online(),
            'today': {
                'paid': paid_today.count(),
                'total': str(total_today),
                'pending': pending,
                'suspicious_pending': suspicious_pending,
            },
            'server_now': timezone.now().isoformat(),
        })
