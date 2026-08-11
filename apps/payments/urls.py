from django.urls import path
from . import views
from . import balance_views

urlpatterns = [
    path('init/', views.PaymentInitView.as_view(), name='payment-init'),
    path('providers/', views.PaymentProviderListView.as_view(), name='payment-providers'),

    # Balance top-up (direct, no external providers)
    path('balance/topup/', balance_views.BalanceTopUpInitView.as_view(), name='balance-topup'),
    path('balance/topup/<int:tx_id>/status/', balance_views.BalanceTopUpStatusView.as_view(), name='balance-topup-status'),
    path('balance/history/', balance_views.BalanceTransactionHistoryView.as_view(), name='balance-history'),
]
