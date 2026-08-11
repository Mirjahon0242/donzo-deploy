from django.contrib import admin
from .models import Banner


@admin.register(Banner)
class BannerAdmin(admin.ModelAdmin):
    list_display = ['type', 'image_url', 'is_active', 'start_date', 'end_date']
    list_filter = ['type', 'is_active']
