from django.urls import path

from . import views

urlpatterns = [
    # PaymentCard registry (multi-card limits + auto-rotation)
    path('cardpay/cards/', views.PaymentCardListView.as_view(), name='admin-cardpay-cards'),
    path('cardpay/cards/<int:pk>/', views.PaymentCardDetailView.as_view(), name='admin-cardpay-card-detail'),
    path('cardpay/cards/<int:pk>/activate/', views.PaymentCardActivateView.as_view(), name='admin-cardpay-card-activate'),
    path('cardpay/cards/<int:pk>/reset/', views.PaymentCardResetView.as_view(), name='admin-cardpay-card-reset'),
    path('cardpay/settings/', views.CardpaySettingsView.as_view(), name='admin-cardpay-settings'),
    path('cardpay/requests/', views.CardpayRequestsView.as_view(), name='admin-cardpay-requests'),
    path('cardpay/messages/', views.CardpayMessagesView.as_view(), name='admin-cardpay-messages'),
    path('cardpay/suspicious/', views.SuspiciousListView.as_view(), name='admin-cardpay-suspicious'),
    path('cardpay/suspicious/<int:pk>/approve/', views.SuspiciousActionView.as_view(), name='admin-cardpay-suspicious-approve', kwargs={'action': 'approve'}),
    path('cardpay/suspicious/<int:pk>/reject/', views.SuspiciousActionView.as_view(), name='admin-cardpay-suspicious-reject', kwargs={'action': 'reject'}),
    path('cardpay/status/', views.CardpayStatusView.as_view(), name='admin-cardpay-status'),
    # User client (Telethon) admin-panel login — phone/code/2FA wizard
    path('cardpay/userclient/status/', views.UserClientStatusView.as_view(), name='admin-userclient-status'),
    path('cardpay/userclient/start/', views.UserClientAuthStartView.as_view(), name='admin-userclient-start'),
    path('cardpay/userclient/verify/', views.UserClientAuthVerifyView.as_view(), name='admin-userclient-verify'),
    path('cardpay/userclient/password/', views.UserClientAuthPasswordView.as_view(), name='admin-userclient-password'),
    path('cardpay/userclient/logout/', views.UserClientLogoutView.as_view(), name='admin-userclient-logout'),
    path('cardpay/userclient/detail/', views.UserClientDetailView.as_view(), name='admin-userclient-detail'),
    path('cardpay/userclient/monitor-check/', views.UserClientMonitorCheckView.as_view(), name='admin-userclient-monitor-check'),
    path('cardpay/userclient/restart/', views.UserClientRestartView.as_view(), name='admin-userclient-restart'),
    path('cardpay/userclient/api-keys/', views.UserClientApiKeysView.as_view(), name='admin-userclient-api-keys'),
]
