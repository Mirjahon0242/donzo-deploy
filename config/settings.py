"""
Django settings for TOPUP HUB project.

SECURITY NOTICE:
Before deploying to production:
  1. Set DJANGO_SECRET_KEY to a long random value
  2. Set DEBUG=False
  3. Set ALLOWED_HOSTS to your domain
  4. Configure HTTPS via a reverse proxy (nginx/Caddy)
  5. NEVER store real payment secrets in the database — use environment variables
"""

import os
import sys
from pathlib import Path
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security: Secret Key ──
# Must be a long random value in production.
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(50))"
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'dev-secret-key-change-in-production')

# ── Security: Debug Mode ──
DEBUG = os.getenv('DEBUG', 'True').lower() == 'true'

# Hard-fail in production when the default dev secret key is used — a known
# SECRET_KEY lets anyone forge JWT tokens and sessions (full account takeover).
if not DEBUG and SECRET_KEY == 'dev-secret-key-change-in-production':
    raise RuntimeError(
        "DJANGO_SECRET_KEY must be set to a secure random value when DEBUG=False. "
        "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(50))\""
    )
if SECRET_KEY == 'dev-secret-key-change-in-production':
    import warnings
    warnings.warn(
        "DJANGO_SECRET_KEY is using the default dev value! "
        "Set a secure random key in .env for production."
    )

# ── Security: Allowed Hosts ──
# In DEBUG, add subdomain wildcards for the public tunnels so the API answers
# regardless of the tunnel hostname:
#   .trycloudflare.com  — quick tunnels (change on every restart)
#   .ngrok-free.app     — ngrok static/assigned domain (PERMANENT backend URL)
# NOTE: Django subdomain wildcards use a LEADING DOT ('.trycloudflare.com'
# matches any subdomain) — '*.trycloudflare.com' would be treated as a literal
# hostname and never match.
# This wildcard is intentionally DEBUG-only: with DEBUG=False the env list is
# used verbatim (no wildcard), avoiding host-header injection in production.
ALLOWED_HOSTS = os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
if DEBUG:
    ALLOWED_HOSTS = ALLOWED_HOSTS + ['.trycloudflare.com', '.ngrok-free.app', '.ngrok.app']

# When running behind a public tunnel (cloudflared) every request arrives via
# 127.0.0.1, so rate throttling would key on ONE IP for ALL users (shared 20/min
# budget → false 429s). NUM_PROXIES lets DRF read the real client IP from the
# X-Forwarded-For header. Use 0 locally without a tunnel.
NUM_PROXIES = int(os.getenv('NUM_PROXIES', '1') if os.getenv('NUM_PROXIES') else ('1' if not DEBUG else '0'))

# ── Security: HTTPS & HSTS (enabled when DEBUG=False) ──
# Test runner: HTTPS majburlash o'chiriladi — Django test client http
# so'rovlar yuboradi va 301'lar barcha testlarni buzardi.
TESTING = 'test' in sys.argv or (sys.argv and 'pytest' in sys.argv[0])
if not DEBUG and not TESTING:
    # Force HTTPS redirect
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'True').lower() == 'true'
    # Trust the X-Forwarded-Proto header set by the reverse proxy / tunnel
    # so SECURE_SSL_REDIRECT works behind nginx / cloudflared.
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    # HTTP Strict Transport Security
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Secure cookies
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    CSRF_COOKIE_HTTPONLY = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    CSRF_COOKIE_SAMESITE = 'Lax'
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_REFERRER_POLICY = 'no-referrer'
    X_FRAME_OPTIONS = 'DENY'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    'drf_yasg',

    # JWT token blacklist (logout / refresh rotation)
    'rest_framework_simplejwt.token_blacklist',

    # Local apps
    'apps.users',
    'apps.services',
    'apps.orders',
    'apps.payments',
    'apps.banners',
    'apps.audit_log',
    'apps.notifications',
    'apps.settings_app',
    'apps.promocodes',
    'apps.cardpay',
    'apps.security',
    'channels',
    'apps.ws',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    # Maintenance mode gate — 503 for the public API while enabled
    'apps.settings_app.middleware.MaintenanceModeMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.postgresql'),
        'NAME': os.getenv('DB_NAME', 'topup_hub'),
        'USER': os.getenv('DB_USER', 'postgres'),
        'PASSWORD': os.getenv('DB_PASSWORD', 'postgres'),
        'HOST': os.getenv('DB_HOST', 'localhost'),
        'PORT': os.getenv('DB_PORT', '5432'),
        # Neon kabi cloud DB'lar SSL talab qiladi (DB_SSLMODE=require).
        # Lokal PostgreSQL uchun 'prefer' xavfsiz (SSL bo'lmasa plain ga tushadi).
        'OPTIONS': {'sslmode': os.getenv('DB_SSLMODE', 'prefer')},
    }
}

# Use SQLite unless a PostgreSQL DB_NAME is configured. SQLite works in
# BOTH debug and production (DEBUG=False) — a small shop can run hardened
# settings (HTTPS/HSTS/secure cookies) on the single-file DB. Set DB_* env
# vars to switch to PostgreSQL without touching code.
if not os.getenv('DB_NAME'):
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        # Several processes share the DB (daphne, bot, user_client, tests):
        # WAL lets readers never block writers and a longer busy timeout
        # avoids transient "database is locked" errors.
        'OPTIONS': {
            'timeout': 20,
            'init_command': 'PRAGMA journal_mode=WAL;',
        },
    }

# Cloud deploy (Render/Neon): bitta DATABASE_URL env yetarli —
# postgresql://user:pass@host:port/dbname  (Neon dashboard'dan nusxalanadi).
if os.getenv('DATABASE_URL'):
    from urllib.parse import unquote, urlparse
    _u = urlparse(os.getenv('DATABASE_URL'))
    DATABASES['default'] = {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': (_u.path or '').lstrip('/') or 'donzo',
        'USER': unquote(_u.username or ''),
        'PASSWORD': unquote(_u.password or ''),
        'HOST': _u.hostname or '',
        'PORT': str(_u.port or 5432),
        'OPTIONS': {'sslmode': os.getenv('DB_SSLMODE', 'require')},
    }

AUTH_USER_MODEL = 'users.User'

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'uz'
TIME_ZONE = 'Asia/Tashkent'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# CORS
CORS_ALLOW_ALL_ORIGINS = os.getenv('CORS_ALLOW_ALL_ORIGINS', str(DEBUG)).lower() == 'true'
CORS_ALLOWED_ORIGINS = os.getenv('CORS_ALLOWED_ORIGINS', 'http://localhost:3000,http://localhost:3002,http://localhost:8000').split(',')
# Public-tunnel origins are allowed while DEBUG (hostnames change on every
# restart; ngrok-free.app is the permanent backend domain). In production these
# regexes are empty — use explicit origins.
if DEBUG:
    CORS_ALLOWED_ORIGIN_REGEXES = [
        r'^https://.*\.trycloudflare\.com$',
        r'^https://.*\.ngrok-free\.app$',
        r'^https://.*\.ngrok\.app$',
    ]
else:
    CORS_ALLOWED_ORIGIN_REGEXES = []

# REST Framework
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_FILTER_BACKENDS': [
        'django_filters.rest_framework.DjangoFilterBackend',
        'rest_framework.filters.SearchFilter',
        'rest_framework.filters.OrderingFilter',
    ],
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        # Generous defaults for the public catalogue (services/categories/
        # banners are fetched on every page load and are NOT the brute-force
        # surface). Sensitive endpoints use the strict scoped rates below.
        'anon': '1000/hour',
        'user': '10000/hour',
        # Scoped rate limits (applied on specific views via ScopedRateThrottle)
        'fragment_login': '20/min',  # Fragment login (username bo'yicha brute-force guard)
        'telegram_auth': '20/min',   # Telegram login/WebApp auth (brute-force guard)
        'telegram_code_login': '10/min',  # Bot one-time-code login (brute-force guard)
        'telegram_send_code': '10/min',   # Auto send-code (per Telegram user session)
        'login_code': '10/min',            # Bot orqali tasdiqlash kodi so'rash (brute-force guard)
        'login_code_verify': '20/min',     # Kodni tekshirish (10 ta urinish/min yetarli)
        'fragment_sync': '6/min',    # Web App ochilganda Fragment force-sync (API'ni spam qilmaydi)
        'payments': '10/min',        # Payment init / balance top-up
        'cardpay_status': '60/min',  # Card-payment status polling (frontend polls every 5s)
        'admin': '200/hour',         # Admin panel endpoints
        'order_create': '20/min',    # Order creation
    },
}

# JWT — hardened: shorter access tokens, rotating refresh tokens that are
# blacklisted after use, so a stolen refresh token cannot be replayed forever.
# The frontend must save the NEW refresh token returned by /token/refresh/
# (api.ts already does this) or the rotated token is lost and the user logs out.
SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(hours=8),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# ── Channels / WebSocket ──
ASGI_APPLICATION = 'config.asgi.application'

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}

# Swagger
SWAGGER_SETTINGS = {
    'SECURITY_DEFINITIONS': {
        'Bearer': {
            'type': 'apiKey',
            'name': 'Authorization',
            'in': 'header'
        }
    }
}
