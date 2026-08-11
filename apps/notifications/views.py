# -*- coding: utf-8 -*-
"""Admin endpoints for global broadcast notifications (via WebSocket)."""
import logging

from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, serializers
from rest_framework.throttling import ScopedRateThrottle
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from .models import BroadcastMessage
from apps.audit_log.models import AuditLog
from apps.users.permissions import IsAdmin

logger = logging.getLogger(__name__)
User = get_user_model()


class BroadcastSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BroadcastMessage
        fields = ['id', 'title', 'message', 'level', 'created_by_name', 'created_at']
        read_only_fields = ['created_by_name', 'created_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.username
        return '—'


class BroadcastListCreateView(generics.ListCreateAPIView):
    """GET history / POST a new broadcast (admin only)."""
    serializer_class = BroadcastSerializer
    permission_classes = [permissions.IsAuthenticated, IsAdmin]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'admin'

    def get_queryset(self):
        # NOTE: pagination is intentionally disabled by slicing — the frontend
        # handles both a plain list and a paginated {results:[...]} shape.
        return BroadcastMessage.objects.select_related('created_by').all()[:50]

    def perform_create(self, serializer):
        broadcast = serializer.save(created_by=self.request.user)
        # Push to ALL online users via the 'global_users' WebSocket group
        try:
            channel_layer = get_channel_layer()
            async_to_sync(channel_layer.group_send)(
                'global_users',
                {
                    'type': 'notification',
                    'title': broadcast.title,
                    'message': broadcast.message,
                    'level': broadcast.level,
                },
            )
        except Exception as exc:
            logger.warning(f"Broadcast WS send failed: {exc}")
        # Audit trail
        try:
            AuditLog.objects.create(
                user=self.request.user,
                action='notification_broadcast',
                target_type='BroadcastMessage',
                target_id=broadcast.id,
                description=f"Global bildirishnoma: {broadcast.title}",
            )
        except Exception:
            logger.exception("Failed to write broadcast audit log")


class RecentBroadcastsView(generics.ListAPIView):
    """
    GET /api/v1/admin/notifications/recent/

    Customer-facing: any authenticated user can fetch the last broadcasts
    (e.g. last 3 days) so announcements are visible even to users who were
    OFFLINE when the WebSocket push was sent. The mini-app fetches this on
    load and shows a dismissible banner.
    """
    serializer_class = BroadcastSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        from django.utils import timezone
        from datetime import timedelta
        cutoff = timezone.now() - timedelta(days=3)
        return (
            BroadcastMessage.objects.select_related('created_by')
            .filter(created_at__gte=cutoff)[:5]
        )
