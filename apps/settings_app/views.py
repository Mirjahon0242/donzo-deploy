import os
from pathlib import Path
from datetime import datetime, timezone as dt_timezone
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import SiteSetting, Setting
from apps.users.permissions import IsAdmin
from apps.users.models import User
from bot_stats import is_bot_running, read_bot_stats, read_supervisor_log


class BotStatusView(APIView):
    """
    GET /api/v1/admin/bot-status/

    Returns the Telegram bot health panel data:
      • running / uptime / last_heartbeat / restarts
      • token_status (valid/invalid + getMe username)
      • polling_errors (recent getUpdates failures: 409/NetworkError/...)
      • messages_sent / updates_handled / per-command counters
      • telegram-linked + total user counts
      • bot config (username, web app URL, token prefix)
      • tail of the supervisor log
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        stats = read_bot_stats()
        running = is_bot_running()

        telegram_users = User.objects.filter(
            telegram_id__isnull=False
        ).exclude(telegram_id='').count()

        # Uptime in seconds since started_at (for the UI's live clock).
        uptime_seconds = None
        started = stats.get('started_at')
        if started:
            try:
                dt = datetime.fromisoformat(started)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=dt_timezone.utc)
                uptime_seconds = max(0, int((timezone.now() - dt).total_seconds()))
            except (ValueError, TypeError):
                uptime_seconds = None

        # Heartbeat age — computed from the already-read stats (no 2nd read).
        heartbeat_age_seconds = None
        last_heartbeat = stats.get('last_heartbeat')
        if last_heartbeat:
            try:
                hb = datetime.fromisoformat(last_heartbeat)
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=dt_timezone.utc)
                heartbeat_age_seconds = max(0, int((timezone.now() - hb).total_seconds()))
            except (ValueError, TypeError):
                heartbeat_age_seconds = None

        # Merge DB token config with the bot's getMe validation result.
        # Normalize to None when the bot has never validated, so the UI can
        # show "Tekshirilmagan" instead of a misleading "invalid" state.
        token_status = stats.get('token_status') or None
        token_configured = bool((Setting.get_setting('telegram_bot_token', '') or '').strip())

        return Response({
            'running': running,
            'stats': {
                'started_at': started,
                'uptime_seconds': uptime_seconds,
                'last_activity': stats.get('last_activity'),
                'last_heartbeat': last_heartbeat,
                'heartbeat_age_seconds': heartbeat_age_seconds,
                'restarts': stats.get('restarts', 0),
                'messages_sent': stats.get('messages_sent', 0),
                'updates_handled': stats.get('updates_handled', 0),
                'commands': stats.get('commands', {}),
                'token_status': token_status,
                'polling_errors': stats.get('polling_errors', []),
            },
            'token_configured': token_configured,
            'telegram_users': telegram_users,
            'total_users': User.objects.count(),
            'config': {
                'bot_username': Setting.get_setting('telegram_bot_username', ''),
                'web_app_url': Setting.get_setting('web_app_url', ''),
                'support': Setting.get_setting('support_telegram', ''),
                'token_prefix': (Setting.get_setting('telegram_bot_token', '') or '')[:12],
            },
            'supervisor_log': read_supervisor_log(30),
            'server_now': timezone.now().isoformat(),
        })


class AdminSettingView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        """Get all settings."""
        return Response(SiteSetting.get_all())

    def put(self, request):
        """Update settings."""
        from apps.users.models import Role
        data = dict(request.data)
        # SECURITY: super_admin_telegram_id is the crown jewel — whoever
        # controls it auto-becomes Super Admin on next login. Only the
        # current Super Admin may change it.
        if 'super_admin_telegram_id' in data and request.user.role != Role.SUPER_ADMIN:
            return Response(
                {'detail': "super_admin_telegram_id ni faqat Super Admin o'zgartira oladi"},
                status=status.HTTP_403_FORBIDDEN,
            )
        SiteSetting.update(data)
        return Response({
            'status': 'ok',
            'settings': SiteSetting.get_all(),
        })


ENV_KEY_MAP = {
    'django_secret_key': 'DJANGO_SECRET_KEY',
    'debug': 'DEBUG',
    'allowed_hosts': 'ALLOWED_HOSTS',
    'cors_allowed_origins': 'CORS_ALLOWED_ORIGINS',
    'db_name': 'DB_NAME',
    'db_user': 'DB_USER',
    'db_password': 'DB_PASSWORD',
    'db_host': 'DB_HOST',
    'db_port': 'DB_PORT',
    'click_merchant_id': 'CLICK_MERCHANT_ID',
    'click_secret_key': 'CLICK_SECRET_KEY',
    'payme_merchant_id': 'PAYME_MERCHANT_ID',
    'payme_secret_key': 'PAYME_SECRET_KEY',
    'uzum_merchant_id': 'UZUM_MERCHANT_ID',
    'uzum_secret_key': 'UZUM_SECRET_KEY',
    'telegram_bot_token': 'TELEGRAM_BOT_TOKEN',
    'telegram_bot_username': 'TELEGRAM_BOT_USERNAME',
    'web_app_url': 'WEB_APP_URL',
    'fragment_api_base_url': 'FRAGMENT_API_BASE_URL',
    'fragment_api_key': 'FRAGMENT_API_KEY',
}


class FragmentStatusView(APIView):
    """
    GET /api/v1/admin/fragment-status/

    Fragment API (Telegram Stars & Premium auto-delivery, fragment-api.uz)
    holat paneli:
      • configured (API key o'rnatilganmi)
      • api_reachable (health)
      • wallet (loyiha hamyoni balansi, live)
      • stars_prices / premium_prices (jonli narxlar)
      • user_info (ixtiyoriy username — getInfo)
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def get(self, request):
        from concurrent.futures import ThreadPoolExecutor

        from apps.services import fragment_api

        username = request.query_params.get('username', '')
        api_key_configured = fragment_api.configured()

        def _safe(fn, *args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                return {'error': str(exc)}

        # Admin panel hech qachon osilib qolmasligi uchun barcha Fragment
        # API so'rovlari PARALLEL bajariladi (har biri qisqa timeout bilan):
        #   wallet_balance, wallet_calculate, premium_pricing, stars_pricing
        # Eng ko'p o'tkaziladigan vaqt = eng sekin so'rov vaqti, yig'indi emas.
        with ThreadPoolExecutor(max_workers=4) as pool:
            f_wallet = pool.submit(
                lambda: _safe(fragment_api.get_wallet_balance, timeout=8))
            f_calc = pool.submit(
                lambda: _safe(fragment_api.get_wallet_calculate, timeout=8))
            f_premium = pool.submit(
                lambda: _safe(fragment_api.get_premium_pricing, timeout=8))
            f_stars = pool.submit(
                lambda: _safe(fragment_api.get_stars_price,
                              fragment_api.MIN_STARS_AMOUNT, timeout=8))

            wallet = f_wallet.result()
            wallet_calculate = f_calc.result()
            premium = f_premium.result()
            stars = f_stars.result()

        user_info = None
        if username:
            user_info = fragment_api.get_info(username, timeout=8)

        # check_health() yana bitta stars/pricing so'rovi yuboradi — o'rniga
        # parallel olingan stars narxini qayta ishlatamiz (ortiqcha round-trip yo'q).
        api_reachable = isinstance(stars, dict) and 'error' not in stars

        return Response({
            'configured': api_key_configured,
            'base_url': fragment_api.get_base_url(),
            'api_reachable': api_reachable,
            'wallet': wallet,
            'wallet_calculate': wallet_calculate,
            'stars_prices': stars,
            'premium_prices': premium,
            'user_info': user_info,
            'price_sync': {
                'enabled': Setting.get_setting('fragment_price_sync_enabled', 'True'),
                'rate': Setting.get_setting('fragment_usd_uzs_rate', '12800'),
                'margin_percent': Setting.get_setting('fragment_price_margin_percent', '15'),
                'last_sync': Setting.get_setting('fragment_last_price_sync', ''),
                'last_result': Setting.get_setting('fragment_last_sync_result', ''),
            },
            'server_now': timezone.now().isoformat(),
        })


class FragmentPriceSyncView(APIView):
    """
    POST /api/v1/admin/fragment-sync/

    Telegram Premium/Stars paketlari narxlarini Fragment API jonli narxlari
    bilan hoziroq sinxronlaydi (kunlik intervalni chetlab o'tadi). Qaytaradi:
        {status, updated, skipped, errors, details, result}
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request):
        from apps.services.fragment_price_sync import sync_fragment_prices

        try:
            # Qisqa timeout — admin panel hech qachon osilib qolmasligi uchun
            # (fragment API sekin yoki o'chiq bo'lsa ham 8 soniyada javob).
            result = sync_fragment_prices(force=True, timeout=8)
            ok = result.get('synced', result.get('errors', 0) == 0)
            return Response({
                'status': 'ok' if ok else 'partial',
                **result,
            })
        except Exception as exc:
            return Response({
                'status': 'error',
                'result': f"Sinxronlashda xatolik: {exc}",
                'updated': 0, 'skipped': 0, 'errors': 1, 'details': [],
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class WriteEnvFileView(APIView):
    """Write selected server settings from DB into the .env file.

    Only settings listed in ENV_KEY_MAP are written.  Settings that are
    empty in the DB are written as empty values (the app will fall back
    to its defaults).
    """
    permission_classes = [permissions.IsAuthenticated, IsAdmin]

    def post(self, request):
        from .models import Setting

        try:
            env_path = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))) / '.env'

            # Read existing .env content (preserve comments and non-managed keys)
            existing_lines = []
            if env_path.exists():
                existing_lines = env_path.read_text(encoding='utf-8').splitlines()

            # Build a dict of existing env values (track which lines are managed)
            managed_keys = set(ENV_KEY_MAP.values())
            kept_lines = []
            for line in existing_lines:
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    kept_lines.append(line)
                    continue
                eq_pos = stripped.find('=')
                if eq_pos == -1:
                    kept_lines.append(line)
                    continue
                key = stripped[:eq_pos].strip()
                if key in managed_keys:
                    continue  # will be replaced
                kept_lines.append(line)

            # Read managed values from DB
            new_lines = list(kept_lines)
            if new_lines and new_lines[-1].strip():
                new_lines.append('')
            for setting_key, env_key in ENV_KEY_MAP.items():
                value = Setting.get_setting(setting_key, '')
                new_lines.append(f'{env_key}={value}')
            new_lines.append('')

            env_path.write_text('\n'.join(new_lines), encoding='utf-8')

            return Response({
                'status': 'ok',
                'message': '.env fayliga yozildi. O\'zgarishlar kuchga kirishi uchun serverni qayta ishga tushiring.',
                'written_keys': list(ENV_KEY_MAP.values()),
            })
        except Exception as e:
            return Response({
                'status': 'error',
                'message': f'.env fayliga yozishda xatolik: {str(e)}',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
