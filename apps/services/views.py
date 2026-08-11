from rest_framework import generics, permissions, filters, viewsets
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import Count, Min, Q, Prefetch
from .models import Category, Service, Package, ServiceField
from .serializers import (
    CategorySerializer, ServiceListSerializer, ServiceDetailSerializer,
    ServiceWriteSerializer, PackageWriteSerializer, ServiceFieldWriteSerializer,
    AdminServiceDetailSerializer,
)
from apps.users.permissions import IsAdmin

# N+1 ni yo'qotish: list'larda count'lar bitta SQL so'rovda annotate qilinadi,
# serializer esa annotatsiyani o'qiydi (fallback: count()).


class CategoryListView(generics.ListAPIView):
    queryset = Category.objects.filter(is_active=True).annotate(
        services_count=Count('services', filter=Q(services__is_active=True), distinct=True)
    )
    serializer_class = CategorySerializer
    permission_classes = [permissions.AllowAny]


class ServiceListView(generics.ListAPIView):
    queryset = (
        Service.objects.filter(is_active=True)
        .select_related('category')
        .annotate(
            min_price=Min('packages__price', filter=Q(packages__is_active=True)),
            packages_count=Count('packages', filter=Q(packages__is_active=True), distinct=True),
        )
    )
    serializer_class = ServiceListSerializer
    permission_classes = [permissions.AllowAny]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['category']
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'created_at']


class ServiceDetailView(generics.RetrieveAPIView):
    queryset = Service.objects.filter(is_active=True).select_related('category').prefetch_related(
        Prefetch(
            'packages',
            queryset=Package.objects.filter(is_active=True).order_by('order_index', 'id'),
            to_attr='_active_packages',
        ),
        Prefetch('fields', queryset=ServiceField.objects.all().order_by('order_index', 'id')),
    )
    serializer_class = ServiceDetailSerializer
    permission_classes = [permissions.AllowAny]
    lookup_field = 'slug'


# Admin Views
class AdminCategoryViewSet(viewsets.ModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]


class AdminServiceViewSet(viewsets.ModelViewSet):
    queryset = Service.objects.all()
    serializer_class = ServiceWriteSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    search_fields = ['name', 'description']

    def get_serializer_class(self):
        # retrieve/update javobida BARCHA paketlar va maydonlar qaytadi —
        # admin tahrirlash formasi to'liq ma'lumot oladi (N+1 yo'q: prefetch).
        if self.action in ('retrieve', 'update', 'partial_update'):
            from .serializers import AdminServiceDetailSerializer
            return AdminServiceDetailSerializer
        return ServiceWriteSerializer

    def get_queryset(self):
        qs = Service.objects.all()
        if self.action in ('retrieve', 'update', 'partial_update'):
            qs = qs.prefetch_related('packages', 'fields')
        return qs


class AdminPackageViewSet(viewsets.ModelViewSet):
    queryset = Package.objects.all()
    serializer_class = PackageWriteSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]


class AdminServiceFieldViewSet(viewsets.ModelViewSet):
    queryset = ServiceField.objects.all()
    serializer_class = ServiceFieldWriteSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
