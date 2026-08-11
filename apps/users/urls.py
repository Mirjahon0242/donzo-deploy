from django.urls import path, include
from rest_framework_simplejwt.views import TokenRefreshView
from . import views

urlpatterns = [
    # FRAGMENT LOGIN: username → Fragment API ma'lumoti → user id → JWT.
    path('fragment-login/', views.fragment_login, name='auth-fragment-login'),
    # BOT ORQALI TASDIQLASH KODI: username → bot kod yuboradi → kodni kiritish → JWT.
    path('login-code/', views.request_login_code, name='auth-login-code'),
    path('login-code/verify/', views.verify_login_code, name='auth-login-code-verify'),
    # DEMO MODE (dev uchun): rol bo'yicha avtomatik demo-foydalanuvchi.
    path('demo-login/', views.demo_login, name='auth-demo-login'),
    path('profile/', views.ProfileView.as_view(), name='auth-profile'),
    path('profile/sync-fragment/', views.ProfileSyncFragmentView.as_view(), name='auth-profile-sync-fragment'),
    path('profile/device-info/', views.DeviceInfoView.as_view(), name='auth-profile-device-info'),
    path('logout/', views.LogoutView.as_view(), name='auth-logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    # Referral system
    path('', include('apps.users.referral_urls')),
]
