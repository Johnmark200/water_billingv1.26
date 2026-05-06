import os
from pathlib import Path

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
        os.environ[key.strip()] = value


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


load_env_file(BASE_DIR / '.env')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'change-me-in-production')
DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'testserver']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'billing',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
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

database_engine = os.getenv('DB_ENGINE', 'mysql').strip().lower()


DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
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

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard'
LOGOUT_REDIRECT_URL = 'login'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


EMAIL_DELIVERY_PROVIDER = 'smtp'
EMAIL_API_TIMEOUT = 10
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_HOST_USER = 'johnmarkomale200@gmail.com'
EMAIL_HOST_PASSWORD = 'tgmhhrhucisdnsey'
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = EMAIL_API_TIMEOUT
DEFAULT_FROM_NAME = os.getenv('DEFAULT_FROM_NAME', 'Tabuan Waterbilling')
DEFAULT_FROM_EMAIL = EMAIL_HOST_USER
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Kept for compatibility with the communications dashboard, even though SMTP
# is the active email transport configured above.
SENDGRID_API_KEY = ''
SENDGRID_FROM_EMAIL = DEFAULT_FROM_EMAIL
SENDGRID_FROM_NAME = os.getenv('SENDGRID_FROM_NAME', DEFAULT_FROM_NAME)

SMS_DELIVERY_PROVIDER = os.getenv('SMS_DELIVERY_PROVIDER', 'sms_api_ph').strip()
SMS_API_TIMEOUT = int(os.getenv('SMS_API_TIMEOUT', '10'))
SMS_API_RETRY_ATTEMPTS = int(os.getenv('SMS_API_RETRY_ATTEMPTS', '2'))
SMS_API_PH_ENDPOINT = os.getenv('SMS_API_PH_ENDPOINT', 'https://dashboard.philsms.com/api/v3/').strip()
SMS_API_PH_API_KEY = os.getenv('SMS_API_PH_API_KEY', '2740|qVaTjIWJ3RUenF0FZO2TMnUS9v8eGsntFPPi5y6Qf959ac95').strip()
SMS_API_PH_RECIPIENT_FIELD = os.getenv('SMS_API_PH_RECIPIENT_FIELD', 'recipient').strip()
SMS_API_PH_MESSAGE_FIELD = os.getenv('SMS_API_PH_MESSAGE_FIELD', 'message').strip()
SMS_API_PH_SENDER_ID = os.getenv('SMS_API_PH_SENDER_ID', 'TABUANWATER').strip()[:11]
SMS_API_PH_MESSAGE_TYPE = os.getenv('SMS_API_PH_MESSAGE_TYPE', 'plain').strip()

PAYMONGO_SECRET_KEY = 'sk_test_t8mWkPe3mexXU9qwoR7pU4xa'
PAYMONGO_API_TIMEOUT = 10
PAYMONGO_BASE_URL = 'https://api.paymongo.com/v1'
PAYMONGO_EWALLET_TYPE = 'gcash'
