"""
Maintenance-mode middleware.

When the `maintenance_mode` Setting is enabled (admin panel -> Kalitlar), the
public API responds 503 "Xizmat vaqtincha ishlamayapti". Health checks and the
staff admin endpoints stay reachable so operators can still turn it back off
and so uptime monitors keep working.
"""

import json
import logging

from django.http import HttpResponse

logger = logging.getLogger(__name__)

# Paths that are ALWAYS allowed during maintenance (operational safety):
#  - /health/            — uptime monitors / frontend health checks
#  - /api/v1/admin/...   — staff panels must stay open so admins can disable it
ALLOWED_PREFIXES = ('/health/', '/api/v1/admin/')


class MaintenanceModeMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Only gate the public API — the Django admin, static files, swagger
        # and staff endpoints stay available.
        if path.startswith('/api/v1/') and not path.startswith(ALLOWED_PREFIXES):
            try:
                from .models import Setting
                maintenance = Setting.get_setting('maintenance_mode', 'False')
                if str(maintenance).strip().lower() in ('true', '1', 'yes', 'on'):
                    # JSON 503 so the frontend can show a clean maintenance screen
                    # (we are already inside the /api/v1/ branch above).
                    body = json.dumps({
                        'detail': "Xizmat vaqtincha ishlamayapti. "
                                  "Iltimos, keyinroq urinib ko'ring.",
                        'maintenance': True,
                    }).encode('utf-8')
                    return HttpResponse(
                        body,
                        status=503,
                        content_type='application/json',
                    )
            except Exception:
                # Never break the request because of a settings lookup failure —
                # fail open so a DB hiccup does not take the site down.
                logger.exception("MaintenanceModeMiddleware lookup failed")
        return self.get_response(request)
