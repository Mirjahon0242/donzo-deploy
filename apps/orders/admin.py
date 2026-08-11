from django.contrib import admin
from .models import Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['order_number', 'service', 'customer_name', 'status', 'payment_status', 'total_price', 'created_at']
    list_filter = ['status', 'payment_status', 'payment_method']
    search_fields = ['order_number', 'customer_name', 'customer_telegram']
    readonly_fields = ['order_number', 'created_at', 'updated_at']
    ordering = ['-created_at']
