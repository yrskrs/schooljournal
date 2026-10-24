import os
from pathlib import Path
from dotenv import load_dotenv

# Завантаження змінних оточення з .env
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('SECRET_KEY', 'default-unsecure-key-change-in-production')
DEBUG = os.getenv('DEBUG', 'False').lower() in ('true', '1', 't')
SCHOOL_REGISTRATION_ENABLED = os.getenv('SCHOOL_REGISTRATION_ENABLED', 'True').lower() in ('true', '1', 't')
INITIAL_SETUP_ENABLED = os.getenv('INITIAL_SETUP_ENABLED', 'True').lower() in ('true', '1', 't')
INITIAL_SETUP_TOKEN = os.getenv('INITIAL_SETUP_TOKEN', '').strip()
# Recovery is explicitly enabled on the server, never inferred from demo names.
INITIAL_SETUP_RECOVERY_USER_ID = int(os.getenv('INITIAL_SETUP_RECOVERY_USER_ID', '0'))

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h.strip()]
ALLOW_PRIVATE_NETWORK_HOSTS = os.getenv('ALLOW_PRIVATE_NETWORK_HOSTS', 'True').lower() in ('true', '1')
LOCAL_SERVER_PROFILE_ENABLED = os.getenv('LOCAL_SERVER_PROFILE_ENABLED', '0') == '1'
LOCAL_SCHOOLTEST_PORT = int(os.getenv('LOCAL_SCHOOLTEST_PORT', '8080'))
LOCAL_SCHOOLWORK_PORT = int(os.getenv('LOCAL_SCHOOLWORK_PORT', '8000'))

csrf_origins = os.getenv('CSRF_TRUSTED_ORIGINS', '')
if csrf_origins:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in csrf_origins.split(',') if o.strip()]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Модульні застосунки системи шкільного журналу
    'apps.accounts',
    'apps.schools',
    'apps.curriculum',
    'apps.journals',
    'apps.importers',
    'apps.portal',
]

MIDDLEWARE = [
    'config.middleware.PrivateNetworkHostMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'config.middleware.LegacySessionCookieMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'apps.journals.services.network_connections.NetworkContextMiddleware',
    'apps.accounts.middleware.InitialSetupMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'apps.portal.site_info.site_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# База даних
db_engine = os.getenv('DB_ENGINE', 'django.db.backends.sqlite3')
if 'postgresql' in db_engine:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('POSTGRES_DB', 'schooljournal_db'),
            'USER': os.getenv('POSTGRES_USER', 'journal_user'),
            'PASSWORD': os.getenv('POSTGRES_PASSWORD', 'journal_secure_password_123'),
            'HOST': os.getenv('POSTGRES_HOST', 'db'),
            'PORT': os.getenv('POSTGRES_PORT', '5432'),
            'CONN_MAX_AGE': 60,
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }

# Кастомна модель користувача
AUTH_USER_MODEL = 'accounts.User'

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
        'OPTIONS': {'min_length': 8},
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Локалізація та час (Україна)
LANGUAGE_CODE = 'uk'
TIME_ZONE = 'Europe/Kyiv'
USE_I18N = True
USE_TZ = True

# Статичні та медіа файли
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [
    BASE_DIR / 'static',
]

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'portal:dashboard'
LOGOUT_REDIRECT_URL = 'login'

# Обмеження на завантаження файлів (10MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10485760
FILE_UPLOAD_MAX_MEMORY_SIZE = 10485760

# Безпека за зворотним проксі Nginx
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'False').lower() in ('true', '1')
SESSION_COOKIE_NAME = 'schooljournal_sessionid'
CSRF_COOKIE_NAME = 'schooljournal_csrftoken'
CSRF_COOKIE_SECURE = os.getenv('CSRF_COOKIE_SECURE', 'False').lower() in ('true', '1')
SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'False').lower() in ('true', '1')

# Server-only, optional peer-to-peer roster integration.
import json as _roster_json
ROSTER_BACKEND = 'apps.journals.services.roster_backend'
ROSTER_SYNC_TOKEN = os.getenv("ROSTER_SYNC_TOKEN", "")
ROSTER_PEERS = _roster_json.loads(os.getenv("ROSTER_PEERS", "[]"))
ROSTER_SYNC_INTERVAL = int(os.getenv("ROSTER_SYNC_INTERVAL", "30"))
ROSTER_SCHOOL_ID = int(os.getenv("ROSTER_SCHOOL_ID", "0"))
ROSTER_YEAR_ID = int(os.getenv("ROSTER_YEAR_ID", "0"))
