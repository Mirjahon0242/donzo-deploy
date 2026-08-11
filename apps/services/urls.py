from django.urls import path
from . import views

urlpatterns = [
    # Public
    path('', views.ServiceListView.as_view(), name='service-list'),
    path('<slug:slug>/', views.ServiceDetailView.as_view(), name='service-detail'),
]
