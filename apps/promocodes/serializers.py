from rest_framework import serializers
from decimal import Decimal
from .models import PromoCode, PromoCodeUsage


class PromoCodeSerializer(serializers.ModelSerializer):
    is_valid_str = serializers.SerializerMethodField()
    usage_count = serializers.SerializerMethodField()

    class Meta:
        model = PromoCode
        fields = '__all__'
        read_only_fields = ['current_uses', 'created_at', 'updated_at']

    def get_is_valid_str(self, obj):
        valid, msg = obj.is_valid()
        return msg if not valid else 'Faol'

    def get_usage_count(self, obj):
        return obj.usages.count()


class PromoCodeValidateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    order_amount = serializers.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'))


class PromoCodeUsageSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromoCodeUsage
        fields = '__all__'
