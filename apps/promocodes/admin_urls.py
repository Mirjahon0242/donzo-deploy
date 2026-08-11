from django.urls import path
from rest_framework import generics, permissions, filters, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from django_filters.rest_framework import DjangoFilterBackend
from decimal import Decimal

from .models import PromoCode
from .serializers import PromoCodeSerializer, PromoCodeValidateSerializer
from apps.users.permissions import IsAdmin


class AdminPromoCodeListView(generics.ListCreateAPIView):
    queryset = PromoCode.objects.all().order_by('-created_at')
    serializer_class = PromoCodeSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    search_fields = ['code', 'description']
    filterset_fields = ['is_active', 'discount_type']


class AdminPromoCodeDetailView(generics.RetrieveUpdateDestroyAPIView):
    queryset = PromoCode.objects.all()
    serializer_class = PromoCodeSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]


urlpatterns = [
    path('promocodes/', AdminPromoCodeListView.as_view(), name='admin-promocodes-list'),
    path('promocodes/<int:pk>/', AdminPromoCodeDetailView.as_view(), name='admin-promocodes-detail'),
]
