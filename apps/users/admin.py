from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ['username', 'email', 'role', 'is_active', 'balance', 'created_at']
    list_filter = ['role', 'is_active', 'is_blacklisted']
    search_fields = ['username', 'email', 'phone', 'telegram_username']
    ordering = ['-created_at']

    fieldsets = BaseUserAdmin.fieldsets + (
        ('Profile', {
            'fields': ('phone', 'telegram_id', 'telegram_username', 'role',
                       'balance', 'cashback_balance', 'referral_code',
                       'referred_by', 'is_blacklisted', 'notes'),
        }),
    )
