from django.urls import path
from rest_framework import generics, permissions, filters, status
from rest_framework.response import Response
from django_filters.rest_framework import DjangoFilterBackend

from .models import Payment, BalanceTransaction
from .serializers import PaymentSerializer, BalanceTransactionSerializer
from apps.users.permissions import IsAdmin
from . import balance_views


class AdminPaymentListView(generics.ListAPIView):
    queryset = Payment.objects.all().select_related('order').order_by('-created_at')
    serializer_class = PaymentSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['provider', 'status']
    search_fields = ['transaction_id', 'order__order_number']


urlpatterns = [
    path('payments/', AdminPaymentListView.as_view(), name='admin-payments-list'),
    # Balance top-up requests (pending → approve/reject) — the balance is
    # only credited after admin approval (manual transfer flow).
    path('balance-topups/', balance_views.AdminBalanceTopUpListView.as_view(), name='admin-balance-topups-list'),
    path('balance-topups/<int:pk>/approve/', balance_views.AdminBalanceTopUpActionView.as_view(), name='admin-balance-topup-approve', kwargs={'action': 'approve'}),
    path('balance-topups/<int:pk>/reject/', balance_views.AdminBalanceTopUpActionView.as_view(), name='admin-balance-topup-reject', kwargs={'action': 'reject'}),
]
