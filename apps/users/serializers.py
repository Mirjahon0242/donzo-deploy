from rest_framework import serializers
from .models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'phone', 'first_name', 'last_name',
            'telegram_id', 'telegram_username', 'language_code', 'avatar_url',
            'is_telegram_premium', 'fragment_synced_at',
            'role', 'is_active', 'balance', 'cashback_balance',
            'referral_code', 'referred_by', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'role', 'balance', 'cashback_balance', 'referral_code',
            'created_at', 'updated_at',
        ]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=6)

    class Meta:
        model = User
        fields = ['username', 'email', 'phone', 'password']

    def create(self, validated_data):
        password = validated_data.pop('password')
        import uuid
        validated_data['referral_code'] = uuid.uuid4().hex[:10].upper()
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()


class TelegramAuthSerializer(serializers.Serializer):
    telegram_id = serializers.CharField()
    telegram_username = serializers.CharField(required=False, allow_blank=True)
    username = serializers.CharField(required=False)
    first_name = serializers.CharField(required=False, allow_blank=True)
    last_name = serializers.CharField(required=False, allow_blank=True)
    photo_url = serializers.CharField(required=False, allow_blank=True)
    language_code = serializers.CharField(required=False, allow_blank=True)
    auth_date = serializers.CharField(required=False, allow_blank=True)
    hash = serializers.CharField(required=False, allow_blank=True)


class ProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['username', 'email', 'phone', 'first_name', 'last_name']


class AdminUserSerializer(serializers.ModelSerializer):
    orders_count = serializers.SerializerMethodField()
    referrals_count = serializers.SerializerMethodField()
    last_login = serializers.DateTimeField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'phone', 'first_name', 'last_name',
            'telegram_id', 'telegram_username', 'language_code', 'avatar_url',
            'is_telegram_premium', 'fragment_synced_at',
            'role', 'is_active', 'balance', 'cashback_balance',
            'referral_code', 'is_blacklisted', 'notes', 'created_at', 'updated_at',
            'orders_count', 'referrals_count', 'last_login',
            # Anti-fraud metadata (admin panelda ko'rinadi)
            'last_ip', 'last_ip_location', 'last_location', 'last_user_agent',
            'last_platform', 'last_language', 'last_timezone', 'last_seen_at',
            'geo_lat', 'geo_lng', 'geo_source',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'orders_count', 'referrals_count', 'last_login']

    def get_orders_count(self, obj):
        # Prefer the annotated value (set on the admin list queryset) to avoid N+1.
        annotated = getattr(obj, '_orders_count', None)
        if annotated is not None:
            return annotated
        return obj.orders.count()

    def get_referrals_count(self, obj):
        annotated = getattr(obj, '_referrals_count', None)
        if annotated is not None:
            return annotated
        return obj.referrals.count()
