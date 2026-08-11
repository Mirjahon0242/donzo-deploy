from django.urls import path
from . import views

urlpatterns = [
    path('notifications/', views.BroadcastListCreateView.as_view(), name='admin-notifications'),
    path('notifications/recent/', views.RecentBroadcastsView.as_view(), name='recent-broadcasts'),
]
