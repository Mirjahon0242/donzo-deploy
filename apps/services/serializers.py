from rest_framework import serializers
from .models import Category, Service, ServiceField, Package


class PackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = ['id', 'service', 'name', 'amount_label', 'price', 'currency', 'is_active', 'order_index']
        read_only_fields = ['id']


class ServiceFieldSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceField
        fields = ['id', 'service', 'field_name', 'field_label', 'field_type', 'is_required', 'validation_regex', 'order_index']
        read_only_fields = ['id']


class ServiceListSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    packages_count = serializers.SerializerMethodField()
    min_price = serializers.IntegerField(read_only=True)  # annotated in ServiceListView

    class Meta:
        model = Service
        fields = ['id', 'name', 'slug', 'category', 'category_name', 'image_url', 'description', 'is_active', 'packages_count', 'min_price']

    def get_packages_count(self, obj):
        # N+1 dan qochish: ServiceListView 'packages_count' ni annotate qiladi.
        # Annotatsiya bo'lmasa (masalan, test/standalone serializer) fallback count().
        val = getattr(obj, 'packages_count', None)
        if val is not None:
            return val
        return obj.packages.filter(is_active=True).count()


class ServiceDetailSerializer(serializers.ModelSerializer):
    category_name = serializers.CharField(source='category.name', read_only=True)
    packages = serializers.SerializerMethodField()
    fields = ServiceFieldSerializer(many=True, read_only=True)

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'slug', 'category', 'category_name',
            'image_url', 'description', 'instruction_text',
            'is_active', 'packages', 'fields', 'created_at', 'updated_at',
        ]

    def get_packages(self, obj):
        # ServiceDetailView prefetch qilgan paketlardan foydalanamiz (N+1 yo'q),
        # prefetch bo'lmasa fallback filter.
        pkgs = getattr(obj, '_active_packages', None)
        if pkgs is None:
            pkgs = obj.packages.filter(is_active=True).order_by('order_index', 'id')
        return PackageSerializer(pkgs, many=True).data


class ServiceWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ['id', 'name', 'slug', 'category', 'image_url', 'description', 'instruction_text', 'is_active', 'allowed_operators']
        read_only_fields = ['id']


class AdminServiceDetailSerializer(serializers.ModelSerializer):
    """Admin tahrirlash uchun to'liq xizmat: BARCHA paketlar va forma maydonlari.

    ServiceDetailSerializer'dan farqi — faqat faol paketlarni emas, hammasini
    qaytaradi (admin nofaol paketni ham ko'rib/yoqishi mumkin). Paketlar va
    maydonlar alohida admin endpointlar orqali saqlanadi (read-only bu yerda).
    """
    category_name = serializers.CharField(source='category.name', read_only=True)
    packages = PackageSerializer(many=True, read_only=True)
    fields = ServiceFieldSerializer(many=True, read_only=True)

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'slug', 'category', 'category_name',
            'image_url', 'description', 'instruction_text', 'is_active',
            'allowed_operators', 'packages', 'fields',
            'created_at', 'updated_at',
        ]


class CategorySerializer(serializers.ModelSerializer):
    services_count = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ['id', 'name', 'slug', 'order_index', 'is_active', 'services_count']
        read_only_fields = ['id']

    def get_services_count(self, obj):
        # N+1 dan qochish: CategoryListView 'services_count' ni annotate qiladi.
        val = getattr(obj, 'services_count', None)
        if val is not None:
            return val
        return obj.services.filter(is_active=True).count()


class PackageWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = '__all__'


class ServiceFieldWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceField
        fields = '__all__'
