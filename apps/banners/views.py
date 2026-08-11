from rest_framework import generics, permissions, viewsets
from .models import Banner
from .serializers import BannerSerializer
from apps.users.permissions import IsAdmin


class BannerListView(generics.ListAPIView):
    queryset = Banner.objects.filter(is_active=True)
    serializer_class = BannerSerializer
    permission_classes = [permissions.AllowAny]


class AdminBannerViewSet(viewsets.ModelViewSet):
    queryset = Banner.objects.all()
    serializer_class = BannerSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
