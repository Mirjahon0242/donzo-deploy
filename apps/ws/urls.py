from django.urls import path

from . import views

urlpatterns = [
    path('ws/metrics/', views.ws_metrics, name='ws-metrics'),
]
