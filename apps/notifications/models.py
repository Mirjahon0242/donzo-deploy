from django.db import models


class Notification(models.Model):
    EVENT_TYPES = [
        ('order_completed', 'Buyurtma tugallandi'),
        ('order_cancelled', 'Buyurtma bekor qilindi'),
        ('order_pending', 'Buyurtma kutilmoqda'),
        ('order_processing', 'Buyurtma bajarilmoqda'),
        ('payment_received', "To'lov qabul qilindi"),
    ]

    CHANNELS = [
        ('telegram', 'Telegram'),
        ('email', 'Email'),
        ('sms', 'SMS'),
    ]

    event_type = models.CharField(max_length=100, choices=EVENT_TYPES)
    channel = models.CharField(max_length=20, choices=CHANNELS)
    template_text = models.TextField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notifications'
        verbose_name = 'Notification Template'
        verbose_name_plural = 'Notification Templates'

    def __str__(self):
        return f"{self.get_event_type_display()} - {self.get_channel_display()}"


class BroadcastMessage(models.Model):
    """
    A global announcement sent by an admin to ALL online users via the
    'global_users' WebSocket group. Kept so the admin panel can show the
    broadcast history (who sent what, when).
    """
    LEVELS = [
        ('info', 'Info'),
        ('success', 'Success'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ]

    title = models.CharField(max_length=200)
    message = models.TextField()
    level = models.CharField(max_length=20, choices=LEVELS, default='info')
    created_by = models.ForeignKey(
        'users.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='broadcasts',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'notification_broadcasts'
        verbose_name = 'Broadcast Message'
        verbose_name_plural = 'Broadcast Messages'
        ordering = ['-created_at']

    def __str__(self):
        return self.title
