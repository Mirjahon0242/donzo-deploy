from django.contrib import admin
from .models import Category, Service, ServiceField, Package


class ServiceFieldInline(admin.TabularInline):
    model = ServiceField
    extra = 1


class PackageInline(admin.TabularInline):
    model = Package
    extra = 1


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'order_index', 'is_active']
    prepopulated_fields = {'slug': ('name',)}


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug', 'category', 'is_active', 'created_at']
    list_filter = ['category', 'is_active']
    search_fields = ['name', 'description']
    prepopulated_fields = {'slug': ('name',)}
    inlines = [ServiceFieldInline, PackageInline]


@admin.register(ServiceField)
class ServiceFieldAdmin(admin.ModelAdmin):
    list_display = ['service', 'field_name', 'field_label', 'field_type', 'is_required']


@admin.register(Package)
class PackageAdmin(admin.ModelAdmin):
    list_display = ['service', 'name', 'price', 'currency', 'is_active']
    list_filter = ['currency', 'is_active']
