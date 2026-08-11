"""
Main URL Configuration for TOPUP HUB
"""
from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from rest_framework import permissions
from drf_yasg.views import get_schema_view
from drf_yasg import openapi
from apps.ws.views import health_check, api_root

# SECURITY: the interactive API schema documents EVERY endpoint including
# the /admin/ panel — it must never be publicly reachable. It is only
# served when DEBUG=True; in production it is not mounted at all.
if settings.DEBUG:
    schema_view = get_schema_view(
        openapi.Info(
            title="DONZO API",
            default_version='v1',
            description="API for gaming top-up platform",
        ),
        public=True,
        permission_classes=[permissions.AllowAny],
    )
else:
    schema_view = None

api_v1_patterns = [
    path('auth/', include('apps.users.urls')),
    path('categories/', include('apps.services.categories_urls')),
    path('services/', include('apps.services.urls')),
    path('orders/', include('apps.orders.urls')),
    path('payments/', include('apps.payments.urls')),
    path('banners/', include('apps.banners.urls')),
    path('promocodes/', include('apps.promocodes.urls')),
    # Admin endpoints
    path('admin/', include('apps.users.admin_urls')),
    path('admin/', include('apps.orders.admin_urls')),
    path('admin/', include('apps.services.admin_urls')),
    path('admin/', include('apps.banners.admin_urls')),
    path('admin/', include('apps.audit_log.urls')),    path('export/', include('apps.payments.export_urls')),
    path('admin/', include('apps.settings_app.urls')),
    path('admin/', include('apps.notifications.urls')),
    path('admin/', include('apps.promocodes.admin_urls')),
    path('admin/', include('apps.payments.admin_payments_urls')),
    # Card payment monitor (user client verification)
    path('admin/', include('apps.cardpay.urls')),
    # Security / anti-fraud center
    path('admin/', include('apps.security.urls')),
    # WebSocket metrics
    path('admin/', include('apps.ws.urls')),
]

urlpatterns = [
    # Friendly root index — visiting the backend host (public tunnel) shows a
    # clean JSON index instead of Django's raw 404 page.
    path('', api_root, name='api-root'),
    path('admin/', admin.site.urls),
    # Public health-check for uptime monitors (database + config status)
    path('health/', health_check, name='health'),
    path('api/v1/', include(api_v1_patterns)),

]

# Swagger/ReDoc only in DEBUG (production never exposes the API schema).
if settings.DEBUG and schema_view is not None:
    urlpatterns += [
        path('swagger/', schema_view.with_ui('swagger', cache_timeout=0), name='schema-swagger-ui'),
        path('redoc/', schema_view.with_ui('redoc', cache_timeout=0), name='schema-redoc'),
    ]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
