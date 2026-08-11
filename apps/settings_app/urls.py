from django.urls import path
from . import views

urlpatterns = [
    path('settings/', views.AdminSettingView.as_view(), name='admin-settings'),
    path('settings/write-env/', views.WriteEnvFileView.as_view(), name='admin-settings-write-env'),
    path('bot-status/', views.BotStatusView.as_view(), name='admin-bot-status'),
    path('fragment-status/', views.FragmentStatusView.as_view(), name='admin-fragment-status'),
    path('fragment-sync/', views.FragmentPriceSyncView.as_view(), name='admin-fragment-sync'),
]
