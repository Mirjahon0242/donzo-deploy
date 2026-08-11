from rest_framework import serializers
from .models import AuditLog


class AuditLogSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True, default='')

    class Meta:
        model = AuditLog
        fields = ['id', 'user', 'username', 'action', 'target_type', 'target_id', 'description', 'created_at']
        read_only_fields = ['id', 'created_at']
