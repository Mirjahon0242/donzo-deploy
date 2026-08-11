from django.contrib import admin
from .models import Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['event_type', 'channel', 'is_active', 'created_at']
    list_filter = ['event_type', 'channel', 'is_active']
