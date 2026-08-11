from django.contrib import admin
from .models import Setting


@admin.register(Setting)
class SettingAdmin(admin.ModelAdmin):
    list_display = ['key', 'value', 'is_encrypted', 'updated_at']
    search_fields = ['key', 'value']
    list_filter = ['is_encrypted']
