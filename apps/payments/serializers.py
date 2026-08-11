from rest_framework import serializers
from .models import Payment, BalanceTransaction


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = ['id', 'order', 'provider', 'transaction_id', 'amount', 'status', 'created_at']
        read_only_fields = ['id', 'created_at']


class PaymentInitSerializer(serializers.Serializer):
    order_id = serializers.IntegerField()
    provider = serializers.ChoiceField(choices=['balance'])


class PaymentCallbackSerializer(serializers.Serializer):
    """Generic callback serializer - each provider has its own format."""
    transaction_id = serializers.CharField()
    status = serializers.ChoiceField(choices=['success', 'failed'])
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    raw_data = serializers.JSONField(default=dict, required=False)


# ====== Balance Top-Up ======


class BalanceTopUpSerializer(serializers.Serializer):
    """Initiate a balance top-up request (admin approval flow)."""
    # min 1 000 so'm, max 100 000 000 so'm — matches the frontend caps and
    # prevents absurd pending-request spam.
    amount = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=1000, max_value=100_000_000)
    # Client-generated unique key for the current top-up attempt. If the same
    # key is re-sent (network retry / double-click), the server returns the
    # original result WITHOUT crediting the balance a second time.
    idempotency_key = serializers.CharField(
        required=False, allow_blank=True, max_length=64
    )


class BalanceTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BalanceTransaction
        fields = '__all__'
        read_only_fields = ['id', 'balance_before', 'balance_after', 'created_at']
