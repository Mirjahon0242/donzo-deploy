import logging
import re

from rest_framework import serializers
from .models import Order

logger = logging.getLogger(__name__)


class OrderCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'service', 'package', 'field_values',
            'customer_name', 'customer_telegram', 'total_price', 'status',
        ]
        read_only_fields = ['id', 'order_number', 'total_price', 'status']

    def validate(self, attrs):
        """
        SECURITY: Validate that:
        1. The package belongs to the selected service
        2. field_values match the service's required fields
        3. Regex patterns are enforced server-side
        """
        service = attrs.get('service')
        package = attrs.get('package')
        field_values = attrs.get('field_values') or {}

        # SECURITY: field_values must be a dict (never a list/string) and
        # bounded in size — a 10MB JSON blob would be a cheap DoS vector and
        # could blow up the DB row.
        if not isinstance(field_values, dict):
            raise serializers.ValidationError({'field_values': 'field_values obyekt bo\'lishi kerak'})
        if len(field_values) > 50:
            raise serializers.ValidationError({'field_values': 'Juda ko\'p maydon yuborildi'})
        for fk, fv in field_values.items():
            if not isinstance(fk, str) or len(fk) > 100:
                raise serializers.ValidationError({'field_values': 'Noto\'g\'ri maydon nomi'})
            # Scalars allowed (str/int/float/bool), bounded length to avoid
            # huge JSON blobs; nested dicts/lists rejected.
            if isinstance(fv, (dict, list)) or isinstance(fv, bool) is False and not isinstance(fv, (str, int, float)):
                raise serializers.ValidationError({'field_values': f"'{fk}' qiymati noto'g'ri turda"})
            if isinstance(fv, str) and len(fv) > 500:
                raise serializers.ValidationError({'field_values': f"'{fk}' qiymati juda uzun"})

        # ── Telegram Premium: username'ni avtomatik to'ldirish ──
        # Login paytida saqlangan foydalanuvchining o'z Telegram username'i
        # order.field_values.username ga yoziladi — foydalanuvchi qo'lda
        # kiritishi shart emas, faqat to'lov qiladi. Agar frontend allaqachon
        # yuborgan bo'lsa, uniki ustun. Fragment fulfillment shu username
        # orqali yetkazib beradi. (Faqat dict bo'lganda ishlaydi — malformed
        # field_values oldin xato bilan rad etiladi, 500 emas.)
        if service and service.slug == 'telegram-premium' and isinstance(field_values, dict):
            request = self.context.get('request')
            fv = dict(field_values)
            if not str(fv.get('username', '') or '').strip():
                if request and request.user.is_authenticated:
                    tg_username = (request.user.telegram_username or '').strip()
                    if tg_username:
                        fv['username'] = tg_username
                        attrs['field_values'] = fv
                        field_values = fv

        # ── Package-Service consistency check ──
        if service and package:
            if package.service_id != service.id:
                raise serializers.ValidationError({
                    'package': "Tanlangan paket ushbu xizmatga tegishli emas"
                })
            # Deactivated packages must not be orderable
            if not package.is_active:
                raise serializers.ValidationError({
                    'package': "Tanlangan paket hozirda faol emas"
                })

        # ── Server-side field validation ──
        # NOTE: `field_values is not None` (not truthiness) — an EMPTY dict
        # must still trigger the required-field check below, otherwise a user
        # could skip every required field by sending {}.
        if service and field_values is not None:
            # Load service fields from the database
            from apps.services.models import ServiceField
            service_fields = ServiceField.objects.filter(service=service)

            errors = {}
            for sf in service_fields:
                value = field_values.get(sf.field_name, '')

                # Check required fields
                if sf.is_required and not value:
                    errors[sf.field_name] = f"{sf.field_label} majburiy maydon"
                    continue

                # Skip further validation if value is empty and not required
                if not value:
                    continue

                # Validate regex pattern server-side
                if sf.validation_regex:
                    try:
                        if not re.match(sf.validation_regex, str(value)):
                            errors[sf.field_name] = f"{sf.field_label} noto'g'ri formatda"
                    except re.error:
                        logger.warning(f"Invalid regex for field {sf.field_name}: {sf.validation_regex}")

                # Validate select options server-side
                if sf.field_type == 'select' and sf.options:
                    if str(value) not in [str(opt) for opt in sf.options]:
                        errors[sf.field_name] = f"{sf.field_label} uchun noto'g'ri qiymat"

            if errors:
                raise serializers.ValidationError(errors)

        return attrs

    def create(self, validated_data):
        # Set customer if authenticated
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['customer'] = request.user

        # Set price from package
        package = validated_data.get('package')
        if package:
            validated_data['total_price'] = package.price

        return super().create(validated_data)


class OrderListSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source='service.name', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'service', 'service_name',
            'package', 'package_name', 'customer_name', 'customer_telegram',
            'status', 'total_price', 'payment_status', 'created_at',
        ]


class OrderDetailSerializer(serializers.ModelSerializer):
    service_name = serializers.CharField(source='service.name', read_only=True)
    package_name = serializers.CharField(source='package.name', read_only=True)
    # Used by the 'Qayta buyurtma' (reorder) button on the frontend to open
    # the service page pre-filled with the original order's data.
    service_slug = serializers.CharField(source='service.slug', read_only=True)
    package_id = serializers.IntegerField(source='package.id', read_only=True)
    package_amount_label = serializers.CharField(source='package.amount_label', read_only=True)

    class Meta:
        model = Order
        fields = '__all__'


class OrderStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[
        'pending', 'processing', 'completed', 'cancelled'
    ])
    cancel_reason = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, max_length=500
    )
