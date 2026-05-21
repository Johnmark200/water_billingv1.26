import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def load_env_file(env_path):
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def env_list(name, default=''):
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(',') if item.strip()]


def database_config_from_url(url):
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in {'postgres', 'postgresql'}:
        engine = 'django.db.backends.postgresql'
    elif scheme == 'mysql':
        engine = 'django.db.backends.mysql'
    else:
        raise ValueError(f'Unsupported database scheme: {scheme}')

    config = {
        'ENGINE': engine,
        'NAME': unquote(parsed.path.lstrip('/')),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or ''),
    }

    if engine == 'django.db.backends.mysql':
        config['OPTIONS'] = {'charset': 'utf8mb4'}

    return config


load_env_file(BASE_DIR / '.env')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'change-me-in-production')
DEBUG = env_bool('DEBUG', True)

allowed_hosts = env_list('ALLOWED_HOSTS', 'localhost,127.0.0.1')
render_hostname = os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()
if render_hostname and render_hostname not in allowed_hosts:
    allowed_hosts.append(render_hostname)
ALLOWED_HOSTS = allowed_hosts

csrf_trusted_origins = env_list('CSRF_TRUSTED_ORIGINS', '')
if render_hostname:
    render_origin = f'https://{render_hostname}'
    if render_origin not in csrf_trusted_origins:
        csrf_trusted_origins.append(render_origin)
CSRF_TRUSTED_ORIGINS = csrf_trusted_origins

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'whitenoise.runserver_nostatic',
    'whitenoise',
    'billing',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'billing.middleware.ActiveAccountMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'waterbilling_project.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'billing.context_processors.billing_app_context',
            ],
        },
    },
]

WSGI_APPLICATION = 'waterbilling_project.wsgi.application'

use_sqlite = env_bool('USE_SQLITE', False)
database_engine = os.getenv('DB_ENGINE', 'postgresql').strip().lower()
database_url = os.getenv('DATABASE_URL', '').strip()


if use_sqlite:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        },
    }
elif database_url:
    DATABASES = {
        'default': database_config_from_url(database_url),
    }
elif database_engine == 'postgresql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('DB_NAME', 'waterbilling'),
            'USER': os.getenv('DB_USER', 'postgres'),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', 'localhost'),
            'PORT': os.getenv('DB_PORT', '5432'),
        },
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.getenv('DB_NAME', 'waterbilling_v1.26'),
            'USER': os.getenv('DB_USER', 'root'),
            'PASSWORD': os.getenv('DB_PASSWORD', ''),
            'HOST': os.getenv('DB_HOST', '127.0.0.1'),
            'PORT': os.getenv('DB_PORT', '3306'),
            'OPTIONS': {
                'charset': 'utf8mb4',
            },
        },
    }

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', not DEBUG)
SESSION_COOKIE_SECURE = env_bool('SESSION_COOKIE_SECURE', not DEBUG)
CSRF_COOKIE_SECURE = env_bool('CSRF_COOKIE_SECURE', not DEBUG)

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


EMAIL_DELIVERY_PROVIDER = 'smtp'
EMAIL_API_TIMEOUT = int(os.getenv('EMAIL_API_TIMEOUT', '10'))
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '').strip()
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '').strip()
EMAIL_USE_TLS = env_bool('EMAIL_USE_TLS', True)
EMAIL_USE_SSL = env_bool('EMAIL_USE_SSL', False)
EMAIL_TIMEOUT = EMAIL_API_TIMEOUT
DEFAULT_FROM_NAME = os.getenv('DEFAULT_FROM_NAME', 'Tabuan Waterbilling')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER)
SERVER_EMAIL = DEFAULT_FROM_EMAIL


SENDGRID_API_KEY = os.getenv('SENDGRID_API_KEY', '').strip()
SENDGRID_FROM_EMAIL = DEFAULT_FROM_EMAIL
SENDGRID_FROM_NAME = os.getenv('SENDGRID_FROM_NAME', DEFAULT_FROM_NAME)

SMS_DELIVERY_PROVIDER = os.getenv('SMS_DELIVERY_PROVIDER', 'sms_api_ph').strip()
SMS_API_TIMEOUT = int(os.getenv('SMS_API_TIMEOUT', '10'))
SMS_API_RETRY_ATTEMPTS = int(os.getenv('SMS_API_RETRY_ATTEMPTS', '2'))
SMS_API_PH_ENDPOINT = os.getenv('SMS_API_PH_ENDPOINT', 'https://dashboard.philsms.com/api/v3/').strip()
SMS_API_PH_API_KEY = os.getenv('SMS_API_PH_API_KEY', '').strip()
SMS_API_PH_RECIPIENT_FIELD = os.getenv('SMS_API_PH_RECIPIENT_FIELD', 'recipient').strip()
SMS_API_PH_MESSAGE_FIELD = os.getenv('SMS_API_PH_MESSAGE_FIELD', 'message').strip()
SMS_API_PH_SENDER_ID = os.getenv('SMS_API_PH_SENDER_ID', 'TABUANWATER').strip()[:11]
SMS_API_PH_MESSAGE_TYPE = os.getenv('SMS_API_PH_MESSAGE_TYPE', 'plain').strip()

PAYMONGO_SECRET_KEY = os.getenv('PAYMONGO_SECRET_KEY', '').strip()
PAYMONGO_API_TIMEOUT = int(os.getenv('PAYMONGO_API_TIMEOUT', '10'))
PAYMONGO_BASE_URL = os.getenv('PAYMONGO_BASE_URL', 'https://api.paymongo.com/v1').strip()
PAYMONGO_EWALLET_TYPE = os.getenv('PAYMONGO_EWALLET_TYPE', 'gcash').strip()

GOOGLE_OAUTH_CLIENT_ID = os.getenv('GOOGLE_OAUTH_CLIENT_ID', '').strip()
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv('GOOGLE_OAUTH_CLIENT_SECRET', '').strip()
GOOGLE_OAUTH_REDIRECT_PATH = os.getenv('GOOGLE_OAUTH_REDIRECT_PATH', '/login/google/callback/').strip()
