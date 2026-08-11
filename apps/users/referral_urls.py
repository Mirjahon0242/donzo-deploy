from django.urls import path
from . import referral_views

urlpatterns = [
    path('referrals/', referral_views.my_referrals, name='my-referrals'),
    path('referrals/stats/', referral_views.referral_stats, name='referral-stats'),
    path('referrals/claim-bonus/', referral_views.claim_referral_bonus, name='claim-referral-bonus'),
    path('referrals/apply-code/', referral_views.apply_referral_code, name='apply-referral-code'),
]
