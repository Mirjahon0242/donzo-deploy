from django.urls import path

from . import views

urlpatterns = [
    path('security/dashboard/', views.SecurityDashboardView.as_view(), name='admin-security-dashboard'),
    path('security/incidents/', views.IncidentListView.as_view(), name='admin-security-incidents'),
    path('security/incidents/<int:pk>/', views.IncidentDetailView.as_view(), name='admin-security-incident-detail'),
    path('security/incidents/<int:pk>/<str:action>/', views.IncidentActionView.as_view(), name='admin-security-incident-action'),
    path('security/cases/', views.CaseListView.as_view(), name='admin-security-cases'),
    path('security/cases/<int:pk>/<str:action>/', views.CaseActionView.as_view(), name='admin-security-case-action'),
    path('security/profiles/', views.UserRiskProfileView.as_view(), name='admin-security-profiles'),
    path('security/profiles/<int:user_id>/<str:action>/', views.UserRiskActionView.as_view(), name='admin-security-profile-action'),
    path('security/settings/', views.SecuritySettingsView.as_view(), name='admin-security-settings'),
    path('security/copilot/', views.AiCopilotView.as_view(), name='admin-security-copilot'),
]
