import base64
import json
import re
import socket
import time
from datetime import date, timedelta
from decimal import Decimal
from email.utils import formataddr
from urllib import error, parse, request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, send_mail
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.html import escape

from .models import (
    AreaRate,
    AuditLog,
    BillingRecord,
    Consumer,
    ConsumerProfile,
    DisconnectionRecord,
    MeterReading,
    Notification,
    Payment,
    PaymentAllocation,
    PaymentArrangement,
    SMSBlast,
    SystemSettings,
)


User = get_user_model()


def normalize_location_name(value):
    return re.sub(r'\s+', ' ', str(value or '').strip())


def get_effective_rate_assignment(consumer, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    default_rate = system_settings.rate_per_m3
    default_assignment = {
        'rate': default_rate,
        'category': 'default',
        'location_name': 'Default rate',
        'label': f'Default rate - PHP {default_rate:,.2f}/m³',
    }
    if consumer is None:
        return default_assignment

    for category, raw_value in (
        (AreaRate.Categories.HOUSEHOLD, consumer.household_code),
        (AreaRate.Categories.VILLAGE, consumer.village),
        (AreaRate.Categories.BARANGAY, consumer.barangay),
    ):
        location_name = normalize_location_name(raw_value)
        if not location_name:
            continue
        area_rate = (
            AreaRate.objects.filter(
                category=category,
                location_name__iexact=location_name,
                is_active=True,
            )
            .order_by('-updated_at', '-id')
            .first()
        )
        if area_rate is not None:
            return {
                'rate': area_rate.rate_per_m3,
                'category': area_rate.category,
                'location_name': area_rate.location_name,
                'label': f'{area_rate.get_category_display()}: {area_rate.location_name} - PHP {area_rate.rate_per_m3:,.2f}/m³',
            }

    return default_assignment


def get_effective_rate_per_m3(consumer, system_settings=None):
    return get_effective_rate_assignment(consumer, system_settings=system_settings)['rate']


def log_audit_action(user, action, target='', details=''):
    profile = ConsumerProfile.objects.filter(user=user).first() if getattr(user, 'is_authenticated', False) else None
    role = profile.role if profile else ''
    return AuditLog.objects.create(
        user=user if getattr(user, 'is_authenticated', False) else None,
        role=role,
        action=action,
        target=str(target or ''),
        details=str(details or ''),
    )


def _truncate_response_text(value, limit=400):
    if not value:
        return ''
    value = value.strip()
    return value if len(value) <= limit else f'{value[:limit]}...'


def email_provider_name():
    return (getattr(settings, 'EMAIL_DELIVERY_PROVIDER', 'console') or 'console').lower()


def sms_provider_name():
    return (getattr(settings, 'SMS_DELIVERY_PROVIDER', 'twilio') or 'twilio').lower()


def sms_provider_label(provider=None):
    provider = provider or sms_provider_name()
    return 'SMS API PH' if provider == 'sms_api_ph' else provider.title()


def normalize_phone_number(value):
    if not value:
        return ''

    stripped = str(value).strip()
    digits = ''.join(character for character in stripped if character.isdigit())
    if stripped.startswith('+'):
        return f'+{digits}'
    return digits


def is_e164_phone_number(value):
    return bool(re.fullmatch(r'\+[1-9]\d{7,14}', normalize_phone_number(value)))


def normalize_email_address(value):
    return (value or '').strip()


def is_valid_email_address(value):
    email_address = normalize_email_address(value)
    if not email_address:
        return False

    try:
        validate_email(email_address)
    except ValidationError:
        return False
    return True


def _looks_like_sendgrid_api_key(value):
    return bool(value) and value.startswith('SG.') and len(value) > 20


def _looks_like_twilio_account_sid(value):
    return bool(value) and value.startswith('AC') and len(value) == 34


def _looks_like_twilio_api_key_sid(value):
    return bool(value) and value.startswith('SK') and len(value) == 34


def _looks_like_twilio_oauth_client_id(value):
    return bool(value) and value.startswith('OQ')


def _twilio_auth_mode():
    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    api_key_sid = getattr(settings, 'TWILIO_API_KEY_SID', '')
    api_key_secret = getattr(settings, 'TWILIO_API_KEY_SECRET', '')

    if _looks_like_twilio_account_sid(account_sid) and api_key_sid and api_key_secret:
        return 'api_key'
    if _looks_like_twilio_account_sid(account_sid) and auth_token:
        return 'account'
    return ''


def _twilio_configuration_error():
    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    api_key_sid = getattr(settings, 'TWILIO_API_KEY_SID', '')
    api_key_secret = getattr(settings, 'TWILIO_API_KEY_SECRET', '')
    phone_number = getattr(settings, 'TWILIO_PHONE_NUMBER', '')

    if _looks_like_twilio_api_key_sid(account_sid):
        return (
            'TWILIO_ACCOUNT_SID currently contains an API Key SID (SK...). '
            'Put your real Account SID (AC...) in TWILIO_ACCOUNT_SID and move the SK value into TWILIO_API_KEY_SID.'
        )
    if _looks_like_twilio_oauth_client_id(auth_token):
        return (
            'TWILIO_AUTH_TOKEN currently contains an OAuth Client ID (OQ...). '
            'OAuth app credentials do not send SMS. Use your Twilio Auth Token, or use TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET.'
        )
    if api_key_secret and not api_key_sid:
        return 'TWILIO_API_KEY_SECRET is set, but TWILIO_API_KEY_SID is missing.'
    if api_key_sid and not _looks_like_twilio_api_key_sid(api_key_sid):
        return 'TWILIO_API_KEY_SID must start with SK.'
    if api_key_sid and not api_key_secret:
        return 'TWILIO_API_KEY_SECRET is missing for the configured TWILIO_API_KEY_SID.'
    if account_sid and not _looks_like_twilio_account_sid(account_sid):
        return 'TWILIO_ACCOUNT_SID must start with AC.'
    if phone_number and not is_e164_phone_number(phone_number):
        return 'TWILIO_PHONE_NUMBER must be in E.164 format, for example +15551234567.'
    if not phone_number:
        return 'TWILIO_PHONE_NUMBER is missing.'
    return (
        'Add either TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_PHONE_NUMBER, or '
        'TWILIO_ACCOUNT_SID + TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET + TWILIO_PHONE_NUMBER.'
    )


def _sms_api_ph_configuration_error():
    api_key = getattr(settings, 'SMS_API_PH_API_KEY', '')
    endpoint = getattr(settings, 'SMS_API_PH_ENDPOINT', '')
    recipient_field = getattr(settings, 'SMS_API_PH_RECIPIENT_FIELD', 'recipient')
    message_field = getattr(settings, 'SMS_API_PH_MESSAGE_FIELD', 'message')
    sender_id = getattr(settings, 'SMS_API_PH_SENDER_ID', '')
    message_type = getattr(settings, 'SMS_API_PH_MESSAGE_TYPE', 'plain')

    if not endpoint:
        return 'SMS_API_PH_ENDPOINT is missing.'
    if not api_key:
        return 'SMS_API_PH_API_KEY is missing.'
    if '|' not in api_key:
        return 'SMS_API_PH_API_KEY should be the PhilSMS API token from the Developers page.'
    if not recipient_field:
        return 'SMS_API_PH_RECIPIENT_FIELD is missing.'
    if not message_field:
        return 'SMS_API_PH_MESSAGE_FIELD is missing.'
    if not sender_id:
        return 'SMS_API_PH_SENDER_ID is missing.'
    if len(sender_id) > 11:
        return 'SMS_API_PH_SENDER_ID must be 11 characters or fewer.'
    if message_type not in {'plain', 'unicode'}:
        return 'SMS_API_PH_MESSAGE_TYPE must be plain or unicode.'
    return ''


def _sms_api_retry_attempts():
    raw_value = getattr(settings, 'SMS_API_RETRY_ATTEMPTS', 2)
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 2


def _should_retry_sms_transport_error(exc):
    reason = getattr(exc, 'reason', exc)
    reason_text = str(reason or exc).lower()
    return (
        isinstance(exc, (TimeoutError, socket.timeout))
        or isinstance(reason, socket.gaierror)
        or 'getaddrinfo failed' in reason_text
        or 'temporary failure in name resolution' in reason_text
        or 'timed out' in reason_text
        or 'connection reset' in reason_text
    )


def _describe_sms_transport_error(exc, endpoint, timeout_seconds):
    provider_label = sms_provider_label('sms_api_ph')
    parsed_endpoint = parse.urlparse(endpoint)
    target = parsed_endpoint.netloc or endpoint
    reason = getattr(exc, 'reason', exc)
    reason_text = _truncate_response_text(str(reason or exc))
    lowered_reason = reason_text.lower()

    if isinstance(reason, socket.gaierror) or 'getaddrinfo failed' in lowered_reason:
        return (
            f'Unable to reach {provider_label} at {target} because DNS lookup failed. '
            'Check the server internet connection, DNS settings, or SMS_API_PH_ENDPOINT.'
        )
    if isinstance(exc, (TimeoutError, socket.timeout)) or 'timed out' in lowered_reason:
        return (
            f'{provider_label} did not respond within {timeout_seconds} seconds at {target}. '
            'The request timed out before the provider returned a result.'
        )
    return f'Unable to reach {provider_label} at {target}: {reason_text}'


def _parse_sms_api_ph_response(response_body):
    """
    Parse SMS API Philippines response to determine actual delivery status.
    Returns: (status, message_id, error_message)
    """
    try:
        response_data = json.loads(response_body)
        
        # Different SMS API providers may return different status values
        api_status = (response_data.get('status') or response_data.get('state') or 'unknown').lower()
        message_id = response_data.get('message_id') or response_data.get('id') or response_data.get('sms_id')
        error_msg = response_data.get('message') or response_data.get('error') or response_data.get('error_message')
        
        # Normalize status
        if api_status in ('error', 'failed', 'rejected'):
            return 'failed', message_id, error_msg or f'API returned status: {api_status}'
        elif api_status in ('success', 'sent', 'queued', 'pending'):
            # Both "pending" and "queued" are acceptable - they will be delivered
            return 'sent', message_id, None
        else:
            # Unknown status - log it but assume success
            return 'sent', message_id, f'Unexpected API status: {api_status}'
            
    except (json.JSONDecodeError, AttributeError, TypeError):
        # Can't parse response - assume it failed
        return None, None, f'Invalid JSON response: {_truncate_response_text(response_body)}'


def _email_configuration_error():
    provider = email_provider_name()
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '')

    if provider == 'sendgrid':
        api_key = getattr(settings, 'SENDGRID_API_KEY', '')
        sendgrid_from_email = getattr(settings, 'SENDGRID_FROM_EMAIL', '') or from_email

        if not sendgrid_from_email:
            return 'SENDGRID_FROM_EMAIL is missing.'
        if not is_valid_email_address(sendgrid_from_email):
            return 'SENDGRID_FROM_EMAIL must be a valid email address.'
        if not api_key:
            return 'SENDGRID_API_KEY is missing.'
        if not _looks_like_sendgrid_api_key(api_key):
            return 'SENDGRID_API_KEY does not look valid. SendGrid API keys normally start with SG.'
        return ''

    if provider == 'smtp':
        if not getattr(settings, 'EMAIL_HOST', ''):
            return 'EMAIL_HOST is missing.'
        if not getattr(settings, 'EMAIL_HOST_USER', ''):
            return 'EMAIL_HOST_USER is missing.'
        if not getattr(settings, 'EMAIL_HOST_PASSWORD', ''):
            return 'EMAIL_HOST_PASSWORD is missing.'
        if not from_email:
            return 'DEFAULT_FROM_EMAIL is missing.'
        if not is_valid_email_address(from_email):
            return 'DEFAULT_FROM_EMAIL must be a valid email address.'
        return ''

    return 'EMAIL_DELIVERY_PROVIDER must be set to sendgrid or smtp for live email delivery.'


def is_email_delivery_configured():
    return not bool(_email_configuration_error())


def is_sms_delivery_configured():
    provider = sms_provider_name()
    if provider == 'twilio':
        return bool(_twilio_auth_mode() and getattr(settings, 'TWILIO_PHONE_NUMBER', ''))
    if provider == 'sms_api_ph':
        return not bool(_sms_api_ph_configuration_error())
    return False


def get_delivery_configuration_summary():
    email_provider = email_provider_name()
    sms_provider = sms_provider_name()

    if email_provider == 'smtp':
        email_env_keys = [
            'EMAIL_DELIVERY_PROVIDER',
            'DEFAULT_FROM_EMAIL',
            'EMAIL_HOST',
            'EMAIL_PORT',
            'EMAIL_HOST_USER',
            'EMAIL_HOST_PASSWORD',
            'EMAIL_USE_TLS',
        ]
    else:
        email_env_keys = [
            'EMAIL_DELIVERY_PROVIDER',
            'DEFAULT_FROM_EMAIL',
            'SENDGRID_FROM_EMAIL',
            'SENDGRID_FROM_NAME',
            'SENDGRID_API_KEY',
        ]

    if sms_provider == 'sms_api_ph':
        sms_setup_note = (
            'Use your PhilSMS API token from the Developers page. Requests are sent with the '
            'Authorization: Bearer header to /sms/send.'
        )
        sms_error = _sms_api_ph_configuration_error()
        sms_env_keys = [
            'SMS_DELIVERY_PROVIDER',
            'SMS_API_PH_ENDPOINT',
            'SMS_API_PH_API_KEY',
            'SMS_API_PH_RECIPIENT_FIELD',
            'SMS_API_PH_MESSAGE_FIELD',
            'SMS_API_PH_SENDER_ID',
            'SMS_API_PH_MESSAGE_TYPE',
        ]
    else:
        sms_setup_note = (
            'Use your Twilio Account SID (AC...) plus either an Auth Token or an API Key SID/Secret. '
            'Do not use OAuth Client ID/Client Secret values for SMS delivery.'
        )
        sms_error = _twilio_configuration_error() if sms_provider == 'twilio' else ''
        sms_env_keys = [
            'SMS_DELIVERY_PROVIDER',
            'TWILIO_ACCOUNT_SID',
            'TWILIO_AUTH_TOKEN or',
            'TWILIO_API_KEY_SID',
            'TWILIO_API_KEY_SECRET',
            'TWILIO_PHONE_NUMBER',
        ]

    return {
        'env_file_path': str(settings.BASE_DIR / '.env'),
        'email_provider': email_provider,
        'email_provider_label': email_provider.title(),
        'email_configured': is_email_delivery_configured(),
        'email_setup_note': (
            'Use either a SendGrid API key that starts with SG. or a working SMTP mailbox/app password. '
            'The sender email must also be valid and authorized by your provider.'
        ),
        'email_error': _email_configuration_error(),
        'email_env_keys': email_env_keys,
        'sms_provider': sms_provider,
        'sms_provider_label': 'SMS API PH' if sms_provider == 'sms_api_ph' else sms_provider.title(),
        'sms_configured': is_sms_delivery_configured(),
        'sms_setup_note': sms_setup_note,
        'sms_error': sms_error,
        'sms_env_keys': sms_env_keys,
    }


def create_in_app_notification(recipient, title, message, notification_type, **related_objects):
    return Notification.objects.create(
        recipient=recipient,
        title=title,
        message=message,
        notification_type=notification_type,
        channel=Notification.Channels.IN_APP,
        status=Notification.Statuses.SENT,
        is_read=False,
        **related_objects,
    )


def get_role_users(roles):
    user_ids = set(ConsumerProfile.objects.filter(role__in=roles).values_list('user_id', flat=True))
    users = list(User.objects.filter(id__in=user_ids))
    if ConsumerProfile.Roles.ADMIN in roles:
        admin_users = User.objects.filter(Q(is_superuser=True) | Q(is_staff=True)).exclude(id__in=user_ids)
        users.extend(admin_users)
    return users


def notify_roles(roles, title, message, notification_type, **related_objects):
    for user in get_role_users(roles):
        create_in_app_notification(user, title, message, notification_type, **related_objects)


def _consumer_email(consumer):
    if consumer.profile and consumer.profile.email:
        return consumer.profile.email
    if consumer.portal_user and consumer.portal_user.email:
        return consumer.portal_user.email
    return ''


def _consumer_phone(consumer):
    if consumer.contact_number:
        return consumer.contact_number
    if consumer.profile and consumer.profile.contact:
        return consumer.profile.contact
    return ''


def _log_outbound_notification(channel, title, message, status, consumer=None, response_message='', **related_objects):
    return Notification.objects.create(
        consumer=consumer,
        title=title,
        message=message,
        notification_type=related_objects.pop('notification_type', Notification.Types.GENERAL),
        channel=channel,
        status=status,
        response_message=response_message,
        is_read=True,
        **related_objects,
    )


def send_user_security_otp(user, channel, otp_code):
    profile = ConsumerProfile.objects.filter(user=user).first()
    title = 'Password Change OTP'
    message = f'Your Tabuan Water Billing System verification code is {otp_code}. It expires in 10 minutes.'

    if channel == 'email':
        email_address = normalize_email_address((user.email or (profile.email if profile else '')).strip())
        if not email_address or not is_valid_email_address(email_address):
            return _log_outbound_notification(
                Notification.Channels.EMAIL,
                title,
                message,
                Notification.Statuses.FAILED,
                recipient=user,
                response_message='No valid email address is configured for this account.',
                notification_type=Notification.Types.GENERAL,
            )

        config_error = _email_configuration_error()
        if config_error:
            return _log_outbound_notification(
                Notification.Channels.EMAIL,
                title,
                message,
                Notification.Statuses.FAILED,
                recipient=user,
                response_message=config_error,
                notification_type=Notification.Types.GENERAL,
            )

        try:
            provider = email_provider_name()
            if provider == 'sendgrid':
                response_message = _send_via_sendgrid(email_address, title, message)
            elif provider == 'smtp':
                response_message = _send_via_smtp(email_address, title, message)
            else:
                raise ValueError(
                    'EMAIL_DELIVERY_PROVIDER is set to console. Change it to sendgrid or smtp in the .env file for live email delivery.'
                )
            status = Notification.Statuses.SENT
        except Exception as exc:  # pragma: no cover - environment-specific
            status = Notification.Statuses.FAILED
            response_message = _truncate_response_text(str(exc))

        return _log_outbound_notification(
            Notification.Channels.EMAIL,
            title,
            message,
            status,
            recipient=user,
            response_message=response_message,
            notification_type=Notification.Types.GENERAL,
        )

    if channel == 'sms':
        phone_number = normalize_phone_number(profile.contact if profile else '')
        if not phone_number or not is_e164_phone_number(phone_number):
            return _log_outbound_notification(
                Notification.Channels.SMS,
                title,
                message,
                Notification.Statuses.FAILED,
                recipient=user,
                response_message='No valid SMS contact number is configured for this account.',
                notification_type=Notification.Types.GENERAL,
            )

        provider = sms_provider_name()
        try:
            if provider == 'sms_api_ph':
                response_message = _send_via_sms_api_ph(phone_number, message)
                status = Notification.Statuses.SENT
            elif provider == 'twilio':
                test_result = send_test_sms(phone_number, message)
                response_message = test_result.response_message
                status = test_result.status
            else:
                raise ValueError(
                    f'Unsupported SMS provider "{provider}". Set SMS_DELIVERY_PROVIDER to sms_api_ph or twilio.'
                )
        except Exception as exc:  # pragma: no cover - environment-specific
            status = Notification.Statuses.FAILED
            response_message = _truncate_response_text(str(exc))

        return _log_outbound_notification(
            Notification.Channels.SMS,
            title,
            message,
            status,
            recipient=user,
            response_message=response_message,
            notification_type=Notification.Types.GENERAL,
        )

    return _log_outbound_notification(
        Notification.Channels.IN_APP,
        title,
        message,
        Notification.Statuses.FAILED,
        recipient=user,
        response_message='Unsupported OTP delivery channel.',
        notification_type=Notification.Types.GENERAL,
    )


def _send_via_sendgrid(email_address, subject, message):
    api_key = getattr(settings, 'SENDGRID_API_KEY', '')
    from_email = getattr(settings, 'SENDGRID_FROM_EMAIL', '') or getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    from_name = getattr(settings, 'SENDGRID_FROM_NAME', 'Water Billing System')

    if not api_key or not from_email:
        raise ValueError('SendGrid settings are incomplete. Add SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in the .env file.')

    payload = json.dumps(
        {
            'personalizations': [{'to': [{'email': email_address}]}],
            'from': {'email': from_email, 'name': from_name},
            'subject': subject,
            'content': [{'type': 'text/plain', 'value': message}],
        }
    ).encode()
    sendgrid_request = request.Request(
        'https://api.sendgrid.com/v3/mail/send',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    with request.urlopen(sendgrid_request, timeout=getattr(settings, 'EMAIL_API_TIMEOUT', 10)) as response:  # pragma: no cover - network-dependent
        response_body = response.read().decode()

    return _truncate_response_text(response_body) or f'SendGrid accepted the email for {email_address}.'


def _send_via_sendgrid_html(email_address, subject, plain_message, html_message):
    api_key = getattr(settings, 'SENDGRID_API_KEY', '')
    from_email = getattr(settings, 'SENDGRID_FROM_EMAIL', '') or getattr(settings, 'DEFAULT_FROM_EMAIL', '')
    from_name = getattr(settings, 'SENDGRID_FROM_NAME', 'Water Billing System')

    if not api_key or not from_email:
        raise ValueError('SendGrid settings are incomplete. Add SENDGRID_API_KEY and SENDGRID_FROM_EMAIL in the .env file.')

    payload = json.dumps(
        {
            'personalizations': [{'to': [{'email': email_address}]}],
            'from': {'email': from_email, 'name': from_name},
            'subject': subject,
            'content': [
                {'type': 'text/plain', 'value': plain_message},
                {'type': 'text/html', 'value': html_message},
            ],
        }
    ).encode()
    sendgrid_request = request.Request(
        'https://api.sendgrid.com/v3/mail/send',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    with request.urlopen(sendgrid_request, timeout=getattr(settings, 'EMAIL_API_TIMEOUT', 10)) as response:  # pragma: no cover - network-dependent
        response_body = response.read().decode()

    return _truncate_response_text(response_body) or f'SendGrid accepted the email for {email_address}.'


def _send_via_smtp(email_address, subject, message):
    provider_configured = is_email_delivery_configured()
    if not provider_configured:
        raise ValueError(
            'SMTP email settings are incomplete. Add EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, and DEFAULT_FROM_EMAIL in the .env file.'
        )

    send_mail(
        subject,
        message,
        formataddr(
            (
                getattr(settings, 'DEFAULT_FROM_NAME', 'Tabuan Waterbilling'),
                getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@waterbilling.local'),
            )
        ),
        [email_address],
        fail_silently=False,
    )
    return f'SMTP email sent to {email_address}.'


def _send_via_smtp_html(email_address, subject, plain_message, html_message):
    provider_configured = is_email_delivery_configured()
    if not provider_configured:
        raise ValueError(
            'SMTP email settings are incomplete. Add EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, and DEFAULT_FROM_EMAIL in the .env file.'
        )

    email = EmailMultiAlternatives(
        subject,
        plain_message,
        formataddr(
            (
                getattr(settings, 'DEFAULT_FROM_NAME', 'Tabuan Waterbilling'),
                getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@waterbilling.local'),
            )
        ),
        [email_address],
    )
    email.attach_alternative(html_message, 'text/html')
    email.send(fail_silently=False)
    return f'SMTP email sent to {email_address}.'


def _send_via_sms_api_ph(phone_number, message):
    config_error = _sms_api_ph_configuration_error()
    if config_error:
        raise ValueError(config_error)

    recipient_field = getattr(settings, 'SMS_API_PH_RECIPIENT_FIELD', 'recipient')
    message_field = getattr(settings, 'SMS_API_PH_MESSAGE_FIELD', 'message')
    recipient = normalize_phone_number(phone_number).lstrip('+')
    endpoint = getattr(settings, 'SMS_API_PH_ENDPOINT', '').rstrip('/')
    if not endpoint.endswith('/sms/send'):
        endpoint = f'{endpoint}/sms/send'

    payload = json.dumps(
        {
            recipient_field: recipient,
            'sender_id': getattr(settings, 'SMS_API_PH_SENDER_ID', 'TABUANWATER')[:11],
            'type': getattr(settings, 'SMS_API_PH_MESSAGE_TYPE', 'plain'),
            message_field: message,
        }
    ).encode()
    sms_request = request.Request(
        endpoint,
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Authorization': f"Bearer {getattr(settings, 'SMS_API_PH_API_KEY', '')}",
            'User-Agent': 'TabuanWaterBilling/1.26 Django SMS Client',
        },
        method='POST',
    )

    timeout_seconds = getattr(settings, 'SMS_API_TIMEOUT', 10)
    retry_attempts = _sms_api_retry_attempts()
    for attempt in range(1, retry_attempts + 1):
        try:
            with request.urlopen(sms_request, timeout=timeout_seconds) as response:  # pragma: no cover - network-dependent
                body = response.read().decode()
            break
        except error.HTTPError as exc:  # pragma: no cover - network-dependent
            failure_body = _truncate_response_text(exc.read().decode())
            if failure_body:
                raise ValueError(f'{sms_provider_label("sms_api_ph")} rejected the request: {failure_body}') from exc
            raise ValueError(f'{sms_provider_label("sms_api_ph")} rejected the request with HTTP {exc.code}.') from exc
        except (error.URLError, TimeoutError, socket.timeout) as exc:  # pragma: no cover - network-dependent
            if attempt < retry_attempts and _should_retry_sms_transport_error(exc):
                time.sleep(min(attempt, 2))
                continue
            raise ValueError(_describe_sms_transport_error(exc, endpoint, timeout_seconds)) from exc

    # Parse and validate the response
    status, message_id, provider_error = _parse_sms_api_ph_response(body)

    if status == 'failed':
        raise ValueError(f'{sms_provider_label("sms_api_ph")} rejected the message: {provider_error or "Unknown provider error."}')
    if status is None:
        raise ValueError(f'{sms_provider_label("sms_api_ph")} returned an unreadable response. {provider_error or ""}'.strip())

    # Return success response with optional message ID
    if message_id:
        return f'{sms_provider_label("sms_api_ph")} accepted (ID: {message_id}) for {phone_number}'
    return f'{sms_provider_label("sms_api_ph")} accepted the SMS for {phone_number}'


def _paymongo_base_url():
    return (getattr(settings, 'PAYMONGO_BASE_URL', 'https://api.paymongo.com/v1') or '').rstrip('/')


def _paymongo_ewallet_type():
    return (getattr(settings, 'PAYMONGO_EWALLET_TYPE', 'gcash') or '').strip().lower()


def _paymongo_configuration_error():
    secret_key = getattr(settings, 'PAYMONGO_SECRET_KEY', '')
    base_url = _paymongo_base_url()
    ewallet_type = _paymongo_ewallet_type()

    if not base_url:
        return 'PAYMONGO_BASE_URL is missing.'
    if not secret_key:
        return 'PAYMONGO_SECRET_KEY is missing.'
    if not secret_key.startswith(('sk_test_', 'sk_live_')):
        return 'PAYMONGO_SECRET_KEY must start with sk_test_ or sk_live_.'
    if not ewallet_type:
        return 'PAYMONGO_EWALLET_TYPE is missing.'
    return ''


def is_paymongo_configured():
    return not bool(_paymongo_configuration_error())


def _paymongo_auth_header():
    token = f"{getattr(settings, 'PAYMONGO_SECRET_KEY', '')}:"
    return f"Basic {base64.b64encode(token.encode()).decode()}"


def _paymongo_url(path):
    if str(path).startswith(('http://', 'https://')):
        return path
    return f"{_paymongo_base_url()}/{str(path).lstrip('/')}"


def _format_paymongo_error(body):
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return _truncate_response_text(body)

    errors = payload.get('errors') or []
    if errors:
        messages = []
        for item in errors:
            detail = item.get('detail') or item.get('message') or item.get('code')
            if detail:
                messages.append(str(detail))
        if messages:
            return _truncate_response_text('; '.join(messages))

    return _truncate_response_text(body)


def _paymongo_request(endpoint, method='GET', payload=None):
    config_error = _paymongo_configuration_error()
    if config_error:
        raise ValueError(config_error)

    data = json.dumps(payload).encode() if payload is not None else None
    paymongo_request = request.Request(
        _paymongo_url(endpoint),
        data=data,
        headers={
            'Authorization': _paymongo_auth_header(),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method=method,
    )

    try:
        with request.urlopen(paymongo_request, timeout=getattr(settings, 'PAYMONGO_API_TIMEOUT', 10)) as response:  # pragma: no cover - network-dependent
            body = response.read().decode()
    except error.HTTPError as exc:  # pragma: no cover - network-dependent
        body = exc.read().decode()
        raise ValueError(_format_paymongo_error(body) or str(exc)) from exc

    return json.loads(body) if body else {}


def _paymongo_amount(amount):
    return int((amount * Decimal('100')).quantize(Decimal('1')))


def _paymongo_billing_details(consumer):
    billing = {
        'name': consumer.full_name,
    }
    email_address = normalize_email_address(_consumer_email(consumer))
    phone_number = normalize_phone_number(_consumer_phone(consumer))
    if email_address:
        billing['email'] = email_address
    if phone_number:
        billing['phone'] = phone_number
    return billing


def _extract_paymongo_redirect_url(payment_intent):
    attributes = payment_intent.get('attributes') or {}
    next_action = attributes.get('next_action') or {}

    candidates = [
        next_action.get('redirect_url') if isinstance(next_action, dict) else '',
        next_action.get('url') if isinstance(next_action, dict) else '',
        (next_action.get('redirect') or {}).get('url') if isinstance(next_action, dict) else '',
        (attributes.get('redirect') or {}).get('url') if isinstance(attributes.get('redirect'), dict) else '',
        attributes.get('redirect_url'),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return ''


def _paymongo_payment_resources(payment_intent):
    attributes = payment_intent.get('attributes') or {}
    payments = attributes.get('payments') or []
    if isinstance(payments, list):
        return payments
    return []


def _paymongo_payment_status(payment_resource):
    return (payment_resource.get('attributes') or {}).get('status') if isinstance(payment_resource, dict) else ''


def _latest_paymongo_payment(payment_intent):
    attributes = payment_intent.get('attributes') or {}
    latest_payment = attributes.get('latest_payment')
    if isinstance(latest_payment, dict):
        return latest_payment

    payments = _paymongo_payment_resources(payment_intent)
    for payment_resource in payments:
        if _paymongo_payment_status(payment_resource) in {'paid', 'succeeded'}:
            return payment_resource
    return payments[-1] if payments else {}


def create_paymongo_ewallet_payment(payment, success_url, cancel_url, ewallet_type=None):
    ewallet_type = (ewallet_type or _paymongo_ewallet_type()).strip().lower()
    if ewallet_type not in {'gcash', 'paymaya'}:
        raise ValueError('Choose a supported PayMongo wallet: GCash or Maya.')
    covered_month = payment.display_covered_month
    billing_label = covered_month.strftime('%B %Y') if covered_month else 'Water bill'
    intent_payload = {
        'data': {
            'attributes': {
                'amount': _paymongo_amount(payment.amount_paid),
                'currency': 'PHP',
                'payment_method_allowed': [ewallet_type],
                'payment_method_options': {},
                'description': f'Tabuan Water Billing payment for {payment.consumer.full_name}',
                'statement_descriptor': 'TABUAN WATER',
                'metadata': {
                    'payment_id': str(payment.id),
                    'consumer_id': str(payment.consumer_id),
                    'billing_id': str(payment.billing_id or ''),
                    'billing_month': billing_label,
                    'cancel_url': cancel_url,
                },
            }
        }
    }
    intent = _paymongo_request('payment_intents', method='POST', payload=intent_payload).get('data') or {}
    intent_id = intent.get('id')
    if not intent_id:
        raise ValueError('PayMongo did not return a Payment Intent ID.')

    method_payload = {
        'data': {
            'attributes': {
                'type': ewallet_type,
                'billing': _paymongo_billing_details(payment.consumer),
            }
        }
    }
    payment_method = _paymongo_request('payment_methods', method='POST', payload=method_payload).get('data') or {}
    payment_method_id = payment_method.get('id')
    if not payment_method_id:
        raise ValueError('PayMongo did not return a Payment Method ID.')

    attach_payload = {
        'data': {
            'attributes': {
                'payment_method': payment_method_id,
                'return_url': success_url,
            }
        }
    }
    attached_intent = _paymongo_request(
        f'payment_intents/{intent_id}/attach',
        method='POST',
        payload=attach_payload,
    ).get('data') or {}
    redirect_url = _extract_paymongo_redirect_url(attached_intent)
    if not redirect_url:
        raise ValueError('PayMongo did not return an e-wallet redirect URL.')

    return {
        'intent': intent,
        'payment_method': payment_method,
        'attached_intent': attached_intent,
        'redirect_url': redirect_url,
    }


def retrieve_paymongo_payment_intent(payment_intent_id):
    return _paymongo_request(f'payment_intents/{payment_intent_id}').get('data') or {}


def extract_paymongo_transaction_details(payment_intent):
    attributes = payment_intent.get('attributes') or {}
    latest_payment = _latest_paymongo_payment(payment_intent)
    latest_payment_attributes = latest_payment.get('attributes') or {}
    latest_payment_id = latest_payment.get('id') or attributes.get('latest_payment') or ''
    allowed_methods = attributes.get('payment_method_allowed') or []
    payment_source = latest_payment_attributes.get('source') or {}
    payment_method_details = latest_payment_attributes.get('payment_method_details') or {}
    payment_method_type = (
        payment_source.get('type')
        or payment_method_details.get('type')
        or (allowed_methods[0] if allowed_methods else '')
    )

    return {
        'intent_id': payment_intent.get('id', ''),
        'intent_status': attributes.get('status', ''),
        'payment_id': latest_payment_id if isinstance(latest_payment_id, str) else '',
        'payment_status': latest_payment_attributes.get('status', ''),
        'payment_method_type': Payment.normalize_online_channel(payment_method_type),
        'amount': attributes.get('amount'),
        'amount_received': attributes.get('amount_received'),
        'paid_at': latest_payment_attributes.get('paid_at') or latest_payment_attributes.get('created_at'),
        'last_payment_error': attributes.get('last_payment_error') or {},
    }


def paymongo_intent_is_paid(payment_intent):
    attributes = payment_intent.get('attributes') or {}
    if attributes.get('status') in {'succeeded', 'paid'}:
        return True

    for payment_resource in _paymongo_payment_resources(payment_intent):
        if _paymongo_payment_status(payment_resource) in {'paid', 'succeeded'}:
            return True
    return False


def _professional_plain_message(title, intro, details, total_label='', total_value='', footer_note=''):
    lines = [
        'Tabuan Water Billing System',
        'System v1.26 | 2026 | Created by Omale J Ohn',
        '',
        title,
        intro,
        '',
        'Details:',
    ]
    for label, value in details:
        lines.append(f'- {label}: {value}')
    if total_label and total_value:
        lines.extend(['', f'{total_label}: {total_value}'])
    if footer_note:
        lines.extend(['', footer_note])
    lines.extend(['', 'Thank you for Choosing Tabuan Water Billing.'])
    return '\n'.join(lines)


def _professional_email_html(title, intro, details, total_label='', total_value='', footer_note=''):
    detail_rows = ''.join(
        (
            '<tr>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;">{escape(label)}</td>'
            f'<td style="padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:right;color:#111827;font-weight:600;">{escape(str(value))}</td>'
            '</tr>'
        )
        for label, value in details
    )
    total_block = ''
    if total_label and total_value:
        total_block = (
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:24px;">'
            '<tr><td></td>'
            '<td width="260" style="background:#dc2626;color:#ffffff;padding:14px 18px;text-align:center;'
            'font-size:18px;font-weight:800;letter-spacing:.04em;">'
            f'{escape(total_label)} &nbsp; {escape(str(total_value))}'
            '</td></tr></table>'
        )
    footer_html = f'<p style="margin:18px 0 0;color:#6b7280;font-size:14px;">{escape(footer_note)}</p>' if footer_note else ''
    return f'''
<!doctype html>
<html>
<body style="margin:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;">
  <div style="max-width:680px;margin:0 auto;padding:28px 14px;">
    <div style="background:#ffffff;border:1px solid #e5e7eb;box-shadow:0 18px 40px rgba(17,24,39,.12);">
      <div style="height:9px;background:#dc2626;"></div>
      <div style="padding:34px 42px 24px;">
        <div style="text-align:center;font-size:20px;font-weight:800;letter-spacing:.08em;color:#111827;">TABUAN WATER BILLING</div>
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:30px;">
          <tr>
            <td style="vertical-align:top;">
              <div style="font-size:24px;font-weight:900;letter-spacing:.02em;">{escape(title)}</div>
              <p style="margin:10px 0 0;color:#4b5563;line-height:1.5;">{escape(intro)}</p>
            </td>
            <td width="180" style="vertical-align:top;text-align:right;color:#6b7280;font-size:13px;line-height:1.7;">
              <strong style="color:#111827;">System v1.26</strong><br>
              2026<br>
              Tabuan, Bayawan City
            </td>
          </tr>
        </table>
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="margin-top:28px;border-collapse:collapse;">
          <thead>
            <tr>
              <th align="left" style="padding:10px 12px;border-bottom:2px solid #dc2626;color:#dc2626;font-size:12px;text-transform:uppercase;">Description</th>
              <th align="right" style="padding:10px 12px;border-bottom:2px solid #dc2626;color:#dc2626;font-size:12px;text-transform:uppercase;">Value</th>
            </tr>
          </thead>
          <tbody>{detail_rows}</tbody>
        </table>
        {total_block}
        {footer_html}
        <div style="margin-top:34px;padding:18px 0;border-top:1px solid #e5e7eb;border-bottom:1px solid #e5e7eb;text-align:center;font-size:22px;font-style:italic;">
          Thank you for choosing Tabuan Water Billing.
        </div>
      </div>
      <div style="background:#dc2626;color:#ffffff;padding:18px 42px;font-size:12px;">
        System v1.26 | 2026 | Created by Omale J. Ohn
      </div>
    </div>
  </div>
</body>
</html>
'''


def _professional_notification_messages(title, intro, details, total_label='', total_value='', footer_note=''):
    return (
        _professional_plain_message(title, intro, details, total_label, total_value, footer_note),
        _professional_email_html(title, intro, details, total_label, total_value, footer_note),
    )


def send_email_notification(consumer, subject, message, notification_type, html_message=None, **related_objects):
    email_address = normalize_email_address(_consumer_email(consumer))
    if not email_address:
        return _log_outbound_notification(
            Notification.Channels.EMAIL,
            subject,
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message='No email address is configured for this consumer.',
            notification_type=notification_type,
            **related_objects,
        )
    if not is_valid_email_address(email_address):
        return _log_outbound_notification(
            Notification.Channels.EMAIL,
            subject,
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message='Recipient email address is not valid.',
            notification_type=notification_type,
            **related_objects,
        )

    config_error = _email_configuration_error()
    if config_error:
        return _log_outbound_notification(
            Notification.Channels.EMAIL,
            subject,
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message=config_error,
            notification_type=notification_type,
            **related_objects,
        )

    provider = email_provider_name()
    try:
        if provider == 'sendgrid':
            if html_message:
                response_message = _send_via_sendgrid_html(email_address, subject, message, html_message)
            else:
                response_message = _send_via_sendgrid(email_address, subject, message)
        elif provider == 'smtp':
            if html_message:
                response_message = _send_via_smtp_html(email_address, subject, message, html_message)
            else:
                response_message = _send_via_smtp(email_address, subject, message)
        else:
            raise ValueError(
                'EMAIL_DELIVERY_PROVIDER is set to console. Change it to sendgrid or smtp in the .env file for live email delivery.'
            )

        status = Notification.Statuses.SENT
    except Exception as exc:  # pragma: no cover - external transport errors are environment-specific
        status = Notification.Statuses.FAILED
        response_message = _truncate_response_text(str(exc))

    return _log_outbound_notification(
        Notification.Channels.EMAIL,
        subject,
        message,
        status,
        consumer=consumer,
        response_message=response_message,
        notification_type=notification_type,
        **related_objects,
    )


def send_sms_notification(consumer, message, notification_type, **related_objects):
    phone_number = normalize_phone_number(_consumer_phone(consumer))
    if not phone_number:
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message='No contact number is configured for this consumer.',
            notification_type=notification_type,
            **related_objects,
        )

    if not is_e164_phone_number(phone_number):
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message='Recipient phone number must be in E.164 format, for example +639171234567.',
            notification_type=notification_type,
            **related_objects,
        )

    provider = sms_provider_name()
    if provider == 'sms_api_ph':
        try:
            response_message = _send_via_sms_api_ph(phone_number, message)
            status = Notification.Statuses.SENT
        except error.HTTPError as exc:  # pragma: no cover - network-dependent
            status = Notification.Statuses.FAILED
            response_message = _truncate_response_text(exc.read().decode())
        except Exception as exc:  # pragma: no cover - network-dependent
            status = Notification.Statuses.FAILED
            response_message = _truncate_response_text(str(exc))

        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            status,
            consumer=consumer,
            response_message=response_message,
            notification_type=notification_type,
            **related_objects,
        )

    if provider != 'twilio':
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message=f'Unsupported SMS provider "{provider}". Set SMS_DELIVERY_PROVIDER to sms_api_ph or twilio.',
            notification_type=notification_type,
            **related_objects,
        )

    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    api_key_sid = getattr(settings, 'TWILIO_API_KEY_SID', '')
    api_key_secret = getattr(settings, 'TWILIO_API_KEY_SECRET', '')
    from_number = normalize_phone_number(getattr(settings, 'TWILIO_PHONE_NUMBER', ''))

    if not is_e164_phone_number(from_number):
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message='Twilio sender number must be in E.164 format, for example +15551234567.',
            notification_type=notification_type,
            **related_objects,
        )

    auth_mode = _twilio_auth_mode()
    if not auth_mode:
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message=_twilio_configuration_error(),
            notification_type=notification_type,
            **related_objects,
        )

    endpoint = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    payload = parse.urlencode({'From': from_number, 'To': phone_number, 'Body': message}).encode()
    auth_username = api_key_sid if auth_mode == 'api_key' else account_sid
    auth_password = api_key_secret if auth_mode == 'api_key' else auth_token
    auth_header = base64.b64encode(f'{auth_username}:{auth_password}'.encode()).decode()
    twilio_request = request.Request(
        endpoint,
        data=payload,
        headers={
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )

    try:
        with request.urlopen(twilio_request, timeout=getattr(settings, 'SMS_API_TIMEOUT', 10)) as response:  # pragma: no cover - network-dependent
            body = response.read().decode()
        status = Notification.Statuses.SENT
        response_message = _truncate_response_text(body) or f'Twilio accepted the SMS for {phone_number}.'
    except error.HTTPError as exc:  # pragma: no cover - network-dependent
        status = Notification.Statuses.FAILED
        response_message = _truncate_response_text(exc.read().decode())
    except Exception as exc:  # pragma: no cover - network-dependent
        status = Notification.Statuses.FAILED
        response_message = _truncate_response_text(str(exc))

    return _log_outbound_notification(
        Notification.Channels.SMS,
        'SMS Notification',
        message,
        status,
        consumer=consumer,
        response_message=response_message,
        notification_type=notification_type,
        **related_objects,
    )


def send_test_email(email_address, subject, message):
    email_address = normalize_email_address(email_address)
    if not is_valid_email_address(email_address):
        return _log_outbound_notification(
            Notification.Channels.EMAIL,
            subject,
            message,
            Notification.Statuses.FAILED,
            response_message='Recipient email address is not valid.',
            notification_type=Notification.Types.ADMIN,
        )

    config_error = _email_configuration_error()
    if config_error:
        return _log_outbound_notification(
            Notification.Channels.EMAIL,
            subject,
            message,
            Notification.Statuses.FAILED,
            response_message=config_error,
            notification_type=Notification.Types.ADMIN,
        )

    provider = email_provider_name()
    try:
        if provider == 'sendgrid':
            response_message = _send_via_sendgrid(email_address, subject, message)
        else:
            response_message = _send_via_smtp(email_address, subject, message)
        status = Notification.Statuses.SENT
    except Exception as exc:  # pragma: no cover - environment-dependent
        status = Notification.Statuses.FAILED
        response_message = _truncate_response_text(str(exc))

    return _log_outbound_notification(
        Notification.Channels.EMAIL,
        subject,
        message,
        status,
        response_message=response_message,
        notification_type=Notification.Types.ADMIN,
    )


def send_test_sms(phone_number, message):
    phone_number = normalize_phone_number(phone_number)
    if not is_e164_phone_number(phone_number):
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Test',
            message,
            Notification.Statuses.FAILED,
            response_message='Recipient phone number must be in E.164 format, for example +639171234567.',
            notification_type=Notification.Types.ADMIN,
        )

    provider = sms_provider_name()
    if provider == 'sms_api_ph':
        config_error = _sms_api_ph_configuration_error()
        if config_error:
            return _log_outbound_notification(
                Notification.Channels.SMS,
                'SMS Test',
                message,
                Notification.Statuses.FAILED,
                response_message=config_error,
                notification_type=Notification.Types.ADMIN,
            )

        try:
            response_message = _send_via_sms_api_ph(phone_number, message)
            status = Notification.Statuses.SENT
        except error.HTTPError as exc:  # pragma: no cover - environment-dependent
            status = Notification.Statuses.FAILED
            response_message = _truncate_response_text(exc.read().decode())
        except Exception as exc:  # pragma: no cover - environment-dependent
            status = Notification.Statuses.FAILED
            response_message = _truncate_response_text(str(exc))

        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Test',
            message,
            status,
            response_message=response_message,
            notification_type=Notification.Types.ADMIN,
        )

    if provider != 'twilio':
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Test',
            message,
            Notification.Statuses.FAILED,
            response_message=f'Unsupported SMS provider "{provider}". Set SMS_DELIVERY_PROVIDER to sms_api_ph or twilio.',
            notification_type=Notification.Types.ADMIN,
        )

    config_error = _twilio_configuration_error()
    if config_error:
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Test',
            message,
            Notification.Statuses.FAILED,
            response_message=config_error,
            notification_type=Notification.Types.ADMIN,
        )

    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    api_key_sid = getattr(settings, 'TWILIO_API_KEY_SID', '')
    api_key_secret = getattr(settings, 'TWILIO_API_KEY_SECRET', '')
    from_number = normalize_phone_number(getattr(settings, 'TWILIO_PHONE_NUMBER', ''))
    auth_mode = _twilio_auth_mode()
    endpoint = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    payload = parse.urlencode({'From': from_number, 'To': phone_number, 'Body': message}).encode()
    auth_username = api_key_sid if auth_mode == 'api_key' else account_sid
    auth_password = api_key_secret if auth_mode == 'api_key' else auth_token
    auth_header = base64.b64encode(f'{auth_username}:{auth_password}'.encode()).decode()
    twilio_request = request.Request(
        endpoint,
        data=payload,
        headers={
            'Authorization': f'Basic {auth_header}',
            'Content-Type': 'application/x-www-form-urlencoded',
        },
    )

    try:
        with request.urlopen(twilio_request, timeout=getattr(settings, 'SMS_API_TIMEOUT', 10)) as response:  # pragma: no cover - environment-dependent
            body = response.read().decode()
        status = Notification.Statuses.SENT
        response_message = _truncate_response_text(body) or f'Twilio accepted the SMS for {phone_number}.'
    except error.HTTPError as exc:  # pragma: no cover - environment-dependent
        status = Notification.Statuses.FAILED
        response_message = _truncate_response_text(exc.read().decode())
    except Exception as exc:  # pragma: no cover - environment-dependent
        status = Notification.Statuses.FAILED
        response_message = _truncate_response_text(str(exc))

    return _log_outbound_notification(
        Notification.Channels.SMS,
        'SMS Test',
        message,
        status,
        response_message=response_message,
        notification_type=Notification.Types.ADMIN,
    )


def notify_consumer(
    consumer,
    title,
    message,
    notification_type,
    send_email=False,
    send_sms=False,
    email_html_message=None,
    sms_message=None,
    **related_objects,
):
    results = {}

    if consumer.portal_user:
        results['in_app'] = create_in_app_notification(
            consumer.portal_user,
            title,
            message,
            notification_type,
            consumer=consumer,
            **related_objects,
        )

    if send_email:
        results['email'] = send_email_notification(
            consumer,
            title,
            message,
            notification_type,
            html_message=email_html_message,
            **related_objects,
        )

    if send_sms:
        results['sms'] = send_sms_notification(consumer, sms_message or message, notification_type, **related_objects)

    return results


def month_start(value):
    if not value:
        return None
    return value.replace(day=1)


def add_months(base_month, months):
    normalized = month_start(base_month)
    month_index = normalized.month - 1 + months
    return date(normalized.year + month_index // 12, (month_index % 12) + 1, 1)


def build_reporting_month_choices(include_current=True):
    months = {
        month_start(item)
        for item in BillingRecord.objects.values_list('billing_month', flat=True)
        if item
    }
    months.update(
        month_start(item)
        for item in MeterReading.objects.values_list('billing_month', flat=True)
        if item
    )
    months.update(
        month_start(item)
        for item in Payment.objects.exclude(covered_month__isnull=True).values_list('covered_month', flat=True)
        if item
    )
    months.update(
        month_start(item)
        for item in Payment.objects.filter(covered_month__isnull=True, billing__isnull=True).values_list('payment_date', flat=True)
        if item
    )
    if include_current:
        months.add(month_start(timezone.localdate()))

    ordered_months = sorted((item for item in months if item), reverse=True)
    return [
        {
            'date': item,
            'value': item.strftime('%Y-%m'),
            'label': item.strftime('%B %Y'),
        }
        for item in ordered_months
    ]


def _billing_rank(record):
    return (
        record.total_amount or Decimal('0'),
        record.amount_paid or Decimal('0'),
        record.current_reading or Decimal('0'),
        record.created_at,
    )


def get_preferred_billing_records(queryset, limit=None):
    billings_by_month = {}
    for record in queryset.select_related('consumer').order_by('-billing_month', '-created_at'):
        billing_month = month_start(record.billing_month)
        key = (record.consumer_id, billing_month)
        current = billings_by_month.get(key)
        if current is None or _billing_rank(record) > _billing_rank(current):
            billings_by_month[key] = record

    ordered_records = sorted(
        billings_by_month.values(),
        key=lambda record: (month_start(record.billing_month), record.created_at),
        reverse=True,
    )
    return ordered_records[:limit] if limit else ordered_records


def get_consumer_monthly_billings(consumer, limit=None):
    if consumer is None:
        return []

    return get_preferred_billing_records(consumer.billings.all(), limit=limit)


def get_existing_billing_for_month(consumer, billing_month):
    target_month = month_start(billing_month)
    for record in get_consumer_monthly_billings(consumer):
        if month_start(record.billing_month) == target_month:
            return record
    return None


def get_consumer_billing_comparison(consumer):
    monthly_billings = get_consumer_monthly_billings(consumer, limit=2)
    current_billing = monthly_billings[0] if monthly_billings else None
    previous_billing = monthly_billings[1] if len(monthly_billings) > 1 else None
    return current_billing, previous_billing


def get_previous_arrears_balance(consumer, billing_month):
    target_month = month_start(billing_month)
    if consumer is None or target_month is None:
        return Decimal('0')

    arrears = Decimal('0')
    for billing in get_consumer_monthly_billings(consumer):
        billing_month_value = month_start(billing.billing_month)
        if billing_month_value and billing_month_value < target_month:
            arrears += billing.amount_due
    return arrears


def get_consumer_outstanding_balance(consumer, through_month=None):
    target_month = month_start(through_month) if through_month else None
    if consumer is None:
        return Decimal('0')

    outstanding = Decimal('0')
    for billing in get_consumer_monthly_billings(consumer):
        billing_month_value = month_start(billing.billing_month)
        if target_month and billing_month_value and billing_month_value > target_month:
            continue
        outstanding += billing.amount_due
    return outstanding


def get_unpaid_billing_records(consumer):
    if consumer is None:
        return []

    unpaid_billings = [billing for billing in get_consumer_monthly_billings(consumer) if billing.amount_due > 0]
    return sorted(
        unpaid_billings,
        key=lambda billing: (month_start(billing.billing_month) or date.max, billing.created_at, billing.id),
    )


def build_unpaid_billing_choices(consumer):
    return [
        (
            str(billing.id),
            f'{billing.billing_month:%B %Y} - PHP {billing.amount_due:,.2f}',
        )
        for billing in get_unpaid_billing_records(consumer)
    ]


def get_selected_billings_for_consumer(consumer, selected_ids):
    if consumer is None:
        return []

    unpaid_billings = {
        str(billing.id): billing
        for billing in get_unpaid_billing_records(consumer)
    }
    return [unpaid_billings[billing_id] for billing_id in selected_ids if billing_id in unpaid_billings]


def get_consumer_last_completed_payment(consumer):
    if consumer is None:
        return None

    return (
        Payment.objects.filter(consumer=consumer, status=Payment.Statuses.COMPLETED)
        .order_by('-payment_date', '-created_at', '-id')
        .first()
    )


def get_latest_approved_arrangement(consumer):
    if consumer is None:
        return None

    return (
        PaymentArrangement.objects.filter(
            consumer=consumer,
            status=PaymentArrangement.Statuses.APPROVED,
        )
        .order_by('-approved_at', '-created_at', '-id')
        .first()
    )


def get_statement_total_for_billing(billing):
    if billing is None:
        return Decimal('0')
    return (billing.previous_arrears or Decimal('0')) + (billing.total_amount or Decimal('0'))


def _payment_totals_by_month(consumer):
    totals = {}
    if consumer is None:
        return totals

    for payment in consumer.payments.exclude(status=Payment.Statuses.FAILED).select_related('billing'):
        covered_month = month_start(payment.display_covered_month)
        if covered_month is None:
            continue
        totals.setdefault(covered_month, Decimal('0'))
        totals[covered_month] += payment.amount_credited

    return totals


def get_next_payment_month(consumer):
    current_month = month_start(timezone.localdate())
    if consumer is None:
        return current_month

    monthly_billings = list(reversed(get_consumer_monthly_billings(consumer)))
    unsettled_billings = [billing for billing in monthly_billings if billing.amount_due > 0]
    if unsettled_billings:
        return month_start(unsettled_billings[-1].billing_month)

    payment_totals = _payment_totals_by_month(consumer)
    covered_months = set(payment_totals.keys())
    covered_months.update(
        month_start(billing.billing_month)
        for billing in monthly_billings
        if billing.amount_due <= 0
    )
    covered_months = {item for item in covered_months if item is not None}

    if covered_months:
        return add_months(max(covered_months), 1)

    if monthly_billings:
        return add_months(month_start(monthly_billings[-1].billing_month), 1)

    return current_month


def build_consumer_payment_month_choices(consumer, advance_months=6):
    if consumer is None:
        return []

    next_month = get_next_payment_month(consumer)
    has_outstanding_balance = get_consumer_outstanding_balance(consumer) > 0
    choices = []
    for offset in range(advance_months + 1):
        candidate = add_months(next_month, offset)
        label = candidate.strftime('%B %Y')
        if offset == 0 and has_outstanding_balance:
            label = f'{label} (Outstanding balance)'
        elif offset == 0:
            label = f'{label} (Next due)'
        elif offset > 0:
            label = f'{label} (Advance)'
        choices.append((candidate.strftime('%Y-%m'), label))
    return choices


def _billings_snapshot_payload(billings):
    return [
        {
            'billing_id': billing.id,
            'billing_month': billing.billing_month.isoformat() if billing.billing_month else '',
            'amount_due': str(billing.amount_due),
        }
        for billing in billings
    ]


def get_matching_approved_arrangement(consumer, selected_billings):
    snapshot = _billings_snapshot_payload(selected_billings)
    for arrangement in PaymentArrangement.objects.filter(
        consumer=consumer,
        status=PaymentArrangement.Statuses.APPROVED,
    ).order_by('-approved_at', '-created_at', '-id'):
        if arrangement.selected_billings == snapshot:
            return arrangement
    return None


def create_payment_arrangement(
    consumer,
    selected_billings,
    requested_by=None,
    requested_amount=None,
    outstanding_balance=None,
    notes='',
    status=PaymentArrangement.Statuses.PENDING,
    approved_by=None,
    payment=None,
):
    requested_amount = requested_amount if requested_amount is not None else sum(
        (billing.amount_due for billing in selected_billings),
        Decimal('0'),
    )
    outstanding_balance = outstanding_balance if outstanding_balance is not None else get_consumer_outstanding_balance(consumer)
    remaining_balance = outstanding_balance - requested_amount
    if remaining_balance < 0:
        remaining_balance = Decimal('0')

    arrangement = PaymentArrangement.objects.create(
        consumer=consumer,
        payment=payment,
        requested_by=requested_by if getattr(requested_by, 'is_authenticated', False) else None,
        approved_by=approved_by if getattr(approved_by, 'is_authenticated', False) else None,
        arrangement_type=PaymentArrangement.ArrangementTypes.SELECTIVE,
        status=status,
        selected_billings=_billings_snapshot_payload(selected_billings),
        requested_amount=requested_amount,
        outstanding_balance_snapshot=outstanding_balance,
        remaining_balance_snapshot=remaining_balance,
        notes=notes or '',
        approved_at=timezone.now() if status in {PaymentArrangement.Statuses.APPROVED, PaymentArrangement.Statuses.COMPLETED} else None,
    )
    return arrangement


def build_consumer_account_overview(consumer, refresh_status=False):
    if consumer is None:
        return {
            'unpaid_billings': [],
            'unpaid_cycles_count': 0,
            'outstanding_balance': Decimal('0'),
            'has_partial_balance': False,
            'approved_arrangement': None,
            'latest_arrangement': None,
            'warning_active': False,
            'warning_issued_at': None,
            'scheduled_disconnection_date': None,
            'days_until_disconnection': None,
            'last_payment': None,
            'monitoring_record': None,
            'countdown_active': False,
            'is_for_disconnection': False,
        }

    if refresh_status:
        refresh_consumer_account_status(consumer, send_notifications=False)
        consumer.refresh_from_db()

    unpaid_billings = get_unpaid_billing_records(consumer)
    latest_arrangement = consumer.payment_arrangements.order_by('-created_at').first()
    approved_arrangement = get_latest_approved_arrangement(consumer)
    monitoring_record = consumer.disconnection_records.exclude(
        status=DisconnectionRecord.Statuses.CANCELLED
    ).order_by('-created_at').first()
    last_payment = get_consumer_last_completed_payment(consumer)
    scheduled_date = consumer.disconnection_scheduled_for
    days_until_disconnection = None
    if scheduled_date:
        days_until_disconnection = (scheduled_date - timezone.localdate()).days

    return {
        'unpaid_billings': unpaid_billings,
        'unpaid_cycles_count': len(unpaid_billings),
        'outstanding_balance': sum((billing.amount_due for billing in unpaid_billings), Decimal('0')),
        'has_partial_balance': any(billing.amount_paid > 0 and billing.amount_due > 0 for billing in unpaid_billings),
        'approved_arrangement': approved_arrangement,
        'latest_arrangement': latest_arrangement,
        'warning_active': len(unpaid_billings) >= 3 and consumer.warning_issued_at is not None,
        'warning_issued_at': consumer.warning_issued_at,
        'scheduled_disconnection_date': scheduled_date,
        'days_until_disconnection': days_until_disconnection,
        'last_payment': last_payment,
        'monitoring_record': monitoring_record,
        'countdown_active': scheduled_date is not None and days_until_disconnection is not None and days_until_disconnection >= 0,
        'is_for_disconnection': consumer.account_status == Consumer.AccountStatuses.FOR_DISCONNECTION,
    }


def _warning_notice_message(consumer, unpaid_cycles_count, scheduled_date):
    return (
        f'Warning notice for {consumer.full_name}: your account has reached {unpaid_cycles_count} unpaid billing cycles. '
        f'Possible disconnection is scheduled within 15 days, on or after {scheduled_date:%B %d, %Y}, unless payment or an approved arrangement is recorded.'
    )


def _admin_disconnection_message(consumer, outstanding_balance, unpaid_cycles_count, last_payment_date, scheduled_date):
    account_number = f'ACC-{consumer.id:05d}'
    payment_label = last_payment_date.strftime('%B %d, %Y') if last_payment_date else 'No recorded payment'
    return (
        f'Consumer Name: {consumer.full_name}\n'
        f'Account Number: {account_number}\n'
        f'Outstanding Balance: PHP {outstanding_balance:,.2f}\n'
        f'Total Unpaid Months: {unpaid_cycles_count}\n'
        f'Last Payment Date: {payment_label}\n'
        f'Scheduled Disconnection Date: {scheduled_date:%B %d, %Y}'
    )


def refresh_consumer_account_status(consumer, send_notifications=True):
    if consumer is None:
        return None

    today = timezone.localdate()
    now = timezone.now()
    overview = build_consumer_account_overview(consumer, refresh_status=False)
    unpaid_billings = overview['unpaid_billings']
    unpaid_cycles_count = overview['unpaid_cycles_count']
    outstanding_balance = overview['outstanding_balance']
    has_partial_balance = overview['has_partial_balance']
    approved_arrangement = overview['approved_arrangement']
    last_payment = overview['last_payment']
    last_payment_date = last_payment.payment_date if last_payment else None
    latest_record = overview['monitoring_record']

    target_status = Consumer.AccountStatuses.ACTIVE
    warning_issued_at = consumer.warning_issued_at
    scheduled_date = consumer.disconnection_scheduled_for
    should_send_warning = False
    should_escalate = False

    if consumer.account_status == Consumer.AccountStatuses.DISCONNECTED and consumer.status == Consumer.Statuses.INACTIVE:
        target_status = Consumer.AccountStatuses.DISCONNECTED
    elif outstanding_balance <= 0:
        target_status = Consumer.AccountStatuses.ACTIVE
        warning_issued_at = None
        scheduled_date = None
    else:
        if unpaid_cycles_count >= 3:
            if warning_issued_at is None:
                warning_issued_at = now
                should_send_warning = True
            if scheduled_date is None:
                scheduled_date = today + timedelta(days=15)

            target_status = Consumer.AccountStatuses.DELINQUENT
            has_recent_payment_after_warning = (
                last_payment_date is not None
                and warning_issued_at is not None
                and last_payment_date >= warning_issued_at.date()
            )
            if scheduled_date and today >= scheduled_date and approved_arrangement is None and not has_recent_payment_after_warning:
                target_status = Consumer.AccountStatuses.FOR_DISCONNECTION
                should_escalate = True
        elif approved_arrangement is not None or has_partial_balance:
            target_status = Consumer.AccountStatuses.PARTIALLY_PAID
        else:
            target_status = Consumer.AccountStatuses.PENDING

    update_fields = []
    if consumer.account_status != target_status:
        consumer.account_status = target_status
        update_fields.append('account_status')
    if consumer.warning_issued_at != warning_issued_at:
        consumer.warning_issued_at = warning_issued_at
        update_fields.append('warning_issued_at')
    if consumer.disconnection_scheduled_for != scheduled_date:
        consumer.disconnection_scheduled_for = scheduled_date
        update_fields.append('disconnection_scheduled_for')
    if last_payment and consumer.last_payment_activity_at != last_payment.created_at:
        consumer.last_payment_activity_at = last_payment.created_at
        update_fields.append('last_payment_activity_at')
    if update_fields:
        consumer.save(update_fields=update_fields)

    active_statuses = {
        DisconnectionRecord.Statuses.MONITORING,
        DisconnectionRecord.Statuses.FOR_DISCONNECTION,
    }
    if unpaid_cycles_count >= 3 and outstanding_balance > 0:
        record_status = (
            DisconnectionRecord.Statuses.FOR_DISCONNECTION
            if target_status == Consumer.AccountStatuses.FOR_DISCONNECTION
            else DisconnectionRecord.Statuses.MONITORING
        )
        if latest_record is None or latest_record.status not in active_statuses:
            latest_record = DisconnectionRecord.objects.create(
                consumer=consumer,
                arrangement=approved_arrangement,
                status=record_status,
                outstanding_balance=outstanding_balance,
                unpaid_months_count=unpaid_cycles_count,
                warning_sent_at=warning_issued_at,
                scheduled_disconnection_date=scheduled_date,
                last_payment_date=last_payment_date,
                escalated_at=now if record_status == DisconnectionRecord.Statuses.FOR_DISCONNECTION else None,
            )
        else:
            record_updates = []
            if latest_record.arrangement_id != getattr(approved_arrangement, 'id', None):
                latest_record.arrangement = approved_arrangement
                record_updates.append('arrangement')
            if latest_record.status != record_status:
                latest_record.status = record_status
                record_updates.append('status')
            if latest_record.outstanding_balance != outstanding_balance:
                latest_record.outstanding_balance = outstanding_balance
                record_updates.append('outstanding_balance')
            if latest_record.unpaid_months_count != unpaid_cycles_count:
                latest_record.unpaid_months_count = unpaid_cycles_count
                record_updates.append('unpaid_months_count')
            if latest_record.warning_sent_at != warning_issued_at:
                latest_record.warning_sent_at = warning_issued_at
                record_updates.append('warning_sent_at')
            if latest_record.scheduled_disconnection_date != scheduled_date:
                latest_record.scheduled_disconnection_date = scheduled_date
                record_updates.append('scheduled_disconnection_date')
            if latest_record.last_payment_date != last_payment_date:
                latest_record.last_payment_date = last_payment_date
                record_updates.append('last_payment_date')
            if should_escalate and latest_record.escalated_at is None:
                latest_record.escalated_at = now
                record_updates.append('escalated_at')
            if record_updates:
                latest_record.save(update_fields=record_updates)
    elif latest_record is not None and latest_record.status in active_statuses:
        latest_record.status = DisconnectionRecord.Statuses.CANCELLED
        latest_record.notes = 'Monitoring cleared after the consumer balance was reduced below the warning threshold.'
        latest_record.save(update_fields=['status', 'notes', 'updated_at'])

    if send_notifications and should_send_warning and scheduled_date:
        system_settings = SystemSettings.load()
        warning_message = _warning_notice_message(consumer, unpaid_cycles_count, scheduled_date)
        warning_email = (
            '<p><strong>Warning Notice</strong></p>'
            f'<p>Your account has reached <strong>{unpaid_cycles_count} unpaid billing cycles</strong>.</p>'
            f'<p>Possible disconnection may begin on or after <strong>{scheduled_date:%B %d, %Y}</strong> if no payment or approved arrangement is recorded.</p>'
            '<p>Please contact the water office immediately.</p>'
        )
        notify_consumer(
            consumer,
            'Overdue account warning notice',
            warning_message,
            Notification.Types.ADMIN,
            send_email=system_settings.notify_by_email,
            send_sms=system_settings.notify_by_sms,
            email_html_message=warning_email,
            sms_message=warning_message,
        )
        log_audit_action(
            None,
            'Issued overdue warning notice',
            target=consumer.full_name,
            details=warning_message,
        )

    if send_notifications and should_escalate and scheduled_date:
        message = _admin_disconnection_message(
            consumer,
            outstanding_balance,
            unpaid_cycles_count,
            last_payment_date,
            scheduled_date,
        )
        notify_roles(
            {ConsumerProfile.Roles.ADMIN},
            'Disconnection monitoring list update',
            message,
            Notification.Types.ADMIN,
            consumer=consumer,
        )
        log_audit_action(
            None,
            'Escalated account for disconnection monitoring',
            target=consumer.full_name,
            details=message,
        )

    return consumer


def get_payments_received_for_month(selected_month, consumer=None, statuses=None):
    selected_month = month_start(selected_month)
    queryset = Payment.objects.filter(
        payment_date__year=selected_month.year,
        payment_date__month=selected_month.month,
    ).select_related('consumer', 'billing').order_by('-payment_date', '-created_at')

    if consumer is not None:
        queryset = queryset.filter(consumer=consumer)
    if statuses is not None:
        queryset = queryset.filter(status__in=statuses)

    return list(queryset)


def get_statement_payment_records(selected_month, consumer=None):
    selected_month = month_start(selected_month)
    queryset = Payment.objects.filter(
        Q(covered_month=selected_month)
        | Q(covered_month__isnull=True, billing__billing_month=selected_month)
        | Q(covered_month__isnull=True, billing__isnull=True, payment_date__year=selected_month.year, payment_date__month=selected_month.month)
    ).select_related('consumer', 'billing').order_by('-payment_date', '-created_at')

    if consumer is not None:
        queryset = queryset.filter(consumer=consumer)

    return list(queryset)


def build_consumer_chart_data(consumer, months=6):
    recent_billings = list(reversed(get_consumer_monthly_billings(consumer, limit=months))) if consumer else []
    status_counts = {
        BillingRecord.Statuses.PAID: 0,
        BillingRecord.Statuses.PENDING: 0,
        BillingRecord.Statuses.PARTIALLY_PAID: 0,
        BillingRecord.Statuses.OVERDUE: 0,
    }

    for billing in recent_billings:
        status_counts[billing.status] = status_counts.get(billing.status, 0) + 1

    outstanding_balance = sum((billing.amount_due for billing in recent_billings), Decimal('0'))
    total_billed = sum((billing.total_amount for billing in recent_billings), Decimal('0'))
    total_paid = sum((billing.amount_paid for billing in recent_billings), Decimal('0'))
    latest_billing = recent_billings[-1] if recent_billings else None

    return {
        'points': [
            {
                'label': billing.billing_month.strftime('%b %Y'),
                'usage': str(billing.usage_m3),
                'bill': str(get_statement_total_for_billing(billing)),
                'current_bill': str(billing.total_amount),
                'arrears': str(billing.previous_arrears),
                'paid': str(billing.amount_paid),
                'balance': str(billing.amount_due),
                'status': billing.status,
                'status_label': billing.get_status_display(),
            }
            for billing in recent_billings
        ],
        'status_counts': {
            'paid': status_counts.get(BillingRecord.Statuses.PAID, 0),
            'pending': status_counts.get(BillingRecord.Statuses.PENDING, 0),
            'partially_paid': status_counts.get(BillingRecord.Statuses.PARTIALLY_PAID, 0),
            'overdue': status_counts.get(BillingRecord.Statuses.OVERDUE, 0),
        },
        'summary': {
            'tracked_months': len(recent_billings),
            'total_billed': str(total_billed),
            'total_paid': str(total_paid),
            'outstanding_balance': str(outstanding_balance),
            'latest_usage': str(latest_billing.usage_m3 if latest_billing else Decimal('0')),
            'latest_bill': str(latest_billing.total_amount if latest_billing else Decimal('0')),
            'paid_ratio': round((status_counts.get(BillingRecord.Statuses.PAID, 0) / len(recent_billings)) * 100)
            if recent_billings
            else 0,
        },
    }


def build_system_monitoring_data(selected_month=None, months=6):
    billings = get_preferred_billing_records(BillingRecord.objects.all())
    target_month = month_start(selected_month)
    if not billings:
        return {
            'points': [],
            'status_counts': {
                'paid': 0,
                'pending': 0,
                'overdue': 0,
                'unpaid': 0,
            },
            'summary': {
                'tracked_months': 0,
                'latest_label': target_month.strftime('%B %Y') if target_month else 'No billing month',
                'latest_accounts': 0,
                'latest_usage': '0',
                'latest_billed': '0',
                'latest_collected': '0',
                'outstanding_balance': '0',
                'paid_ratio': 0,
            },
        }

    monthly_totals = {}
    for billing in billings:
        billing_month = month_start(billing.billing_month)
        snapshot = monthly_totals.setdefault(
            billing_month,
            {
                'month': billing_month,
                'usage': Decimal('0'),
                'bill': Decimal('0'),
                'paid': Decimal('0'),
                'balance': Decimal('0'),
                'accounts': 0,
                'paid_accounts': 0,
                'pending_accounts': 0,
                'overdue_accounts': 0,
            },
        )

        snapshot['usage'] += billing.usage_m3 or Decimal('0')
        snapshot['bill'] += billing.total_amount or Decimal('0')
        snapshot['balance'] += billing.amount_due or Decimal('0')
        snapshot['accounts'] += 1

        panel_status = resolve_billing_status(billing)
        if panel_status == BillingRecord.Statuses.PAID:
            snapshot['paid_accounts'] += 1
        elif panel_status == BillingRecord.Statuses.OVERDUE:
            snapshot['overdue_accounts'] += 1
        else:
            snapshot['pending_accounts'] += 1

    payment_totals = {}
    for payment in Payment.objects.filter(status=Payment.Statuses.COMPLETED).only('payment_date', 'amount_paid'):
        payment_month = month_start(payment.payment_date)
        payment_totals.setdefault(payment_month, Decimal('0'))
        payment_totals[payment_month] += payment.amount_paid or Decimal('0')

    for payment_month, total_paid in payment_totals.items():
        snapshot = monthly_totals.setdefault(
            payment_month,
            {
                'month': payment_month,
                'usage': Decimal('0'),
                'bill': Decimal('0'),
                'paid': Decimal('0'),
                'balance': Decimal('0'),
                'accounts': 0,
                'paid_accounts': 0,
                'pending_accounts': 0,
                'overdue_accounts': 0,
            },
        )
        snapshot['paid'] = total_paid

    available_months = sorted(monthly_totals.keys())
    if target_month is None:
        target_month = available_months[-1]

    if target_month not in monthly_totals:
        monthly_totals[target_month] = {
            'month': target_month,
            'usage': Decimal('0'),
            'bill': Decimal('0'),
            'paid': Decimal('0'),
            'balance': Decimal('0'),
            'accounts': 0,
            'paid_accounts': 0,
            'pending_accounts': 0,
            'overdue_accounts': 0,
        }
        available_months = sorted(monthly_totals.keys())

    eligible_months = [item for item in available_months if item <= target_month]
    if not eligible_months:
        eligible_months = [target_month]

    selected_months = eligible_months[-months:]
    ordered_snapshots = [monthly_totals[item] for item in selected_months]
    latest_snapshot = monthly_totals[target_month]

    return {
        'points': [
            {
                'label': snapshot['month'].strftime('%b %Y'),
                'usage': str(snapshot['usage']),
                'bill': str(snapshot['bill']),
                'paid': str(snapshot['paid']),
                'balance': str(snapshot['balance']),
                'accounts': snapshot['accounts'],
                'paid_accounts': snapshot['paid_accounts'],
                'pending_accounts': snapshot['pending_accounts'],
                'overdue_accounts': snapshot['overdue_accounts'],
            }
            for snapshot in ordered_snapshots
        ],
        'status_counts': {
            'paid': latest_snapshot['paid_accounts'],
            'pending': latest_snapshot['pending_accounts'],
            'overdue': latest_snapshot['overdue_accounts'],
            'unpaid': latest_snapshot['pending_accounts'] + latest_snapshot['overdue_accounts'],
        },
        'summary': {
            'tracked_months': len(ordered_snapshots),
            'latest_label': latest_snapshot['month'].strftime('%B %Y'),
            'latest_accounts': latest_snapshot['accounts'],
            'latest_usage': str(latest_snapshot['usage']),
            'latest_billed': str(latest_snapshot['bill']),
            'latest_collected': str(latest_snapshot['paid']),
            'outstanding_balance': str(latest_snapshot['balance']),
            'paid_ratio': round((latest_snapshot['paid_accounts'] / latest_snapshot['accounts']) * 100)
            if latest_snapshot['accounts']
            else 0,
        },
    }


def resolve_billing_status(billing):
    today = timezone.localdate()
    if (billing.total_amount or Decimal('0')) > 0 and (billing.amount_paid or Decimal('0')) >= (billing.total_amount or Decimal('0')):
        return BillingRecord.Statuses.PAID
    if billing.amount_due > 0 and billing.due_date and billing.due_date < today:
        return BillingRecord.Statuses.OVERDUE
    return BillingRecord.Statuses.PENDING


def apply_panel_billing_status(billing):
    resolved_status = resolve_billing_status(billing)
    billing.panel_status = resolved_status
    billing.panel_status_label = dict(BillingRecord.Statuses.choices).get(resolved_status, resolved_status.title())
    return billing


def build_settlement_snapshot(selected_month=None, consumer=None):
    queryset = BillingRecord.objects.all()
    target_month = month_start(selected_month)
    if target_month is not None:
        queryset = queryset.filter(billing_month=target_month)
    if consumer is not None:
        queryset = queryset.filter(consumer=consumer)

    pending_records = []
    overdue_records = []

    for billing in get_preferred_billing_records(queryset):
        billing = apply_panel_billing_status(billing)
        if billing.amount_due <= 0 or billing.panel_status == BillingRecord.Statuses.PAID:
            continue

        if billing.panel_status == BillingRecord.Statuses.OVERDUE:
            overdue_records.append(billing)
        else:
            pending_records.append(billing)

    pending_records.sort(
        key=lambda billing: (
            billing.due_date or date.max,
            month_start(billing.billing_month) or date.max,
            (billing.consumer.full_name or '').lower(),
        )
    )
    overdue_records.sort(
        key=lambda billing: (
            billing.due_date or date.max,
            month_start(billing.billing_month) or date.max,
            (billing.consumer.full_name or '').lower(),
        )
    )

    unsettled_records = sorted(
        overdue_records + pending_records,
        key=lambda billing: (
            0 if billing.panel_status == BillingRecord.Statuses.OVERDUE else 1,
            billing.due_date or date.max,
            month_start(billing.billing_month) or date.max,
            (billing.consumer.full_name or '').lower(),
        ),
    )

    return {
        'all_records': unsettled_records,
        'pending_records': pending_records,
        'overdue_records': overdue_records,
        'unsettled_count': len(unsettled_records),
        'pending_count': len(pending_records),
        'overdue_count': len(overdue_records),
        'pending_total': sum((billing.amount_due for billing in pending_records), Decimal('0')),
        'overdue_total': sum((billing.amount_due for billing in overdue_records), Decimal('0')),
        'unsettled_total': sum((billing.amount_due for billing in unsettled_records), Decimal('0')),
    }


def get_previous_reading_details(consumer, billing_month):
    target_month = month_start(billing_month)
    prior_reading = (
        MeterReading.objects.filter(consumer=consumer, billing_month__lt=target_month)
        .order_by('-billing_month', '-created_at')
        .first()
    )
    if prior_reading:
        return {
            'value': prior_reading.current_reading,
            'month': month_start(prior_reading.billing_month),
            'source': 'reading',
            'record': prior_reading,
        }

    prior_billings = get_preferred_billing_records(
        BillingRecord.objects.filter(consumer=consumer, billing_month__lt=target_month)
    )
    prior_billing = prior_billings[0] if prior_billings else None
    if prior_billing:
        return {
            'value': prior_billing.current_reading,
            'month': month_start(prior_billing.billing_month),
            'source': 'billing',
            'record': prior_billing,
        }

    return {
        'value': Decimal('0'),
        'month': None,
        'source': '',
        'record': None,
    }


def get_previous_reading_for_month(consumer, billing_month):
    return get_previous_reading_details(consumer, billing_month)['value']


@transaction.atomic
def rebuild_consumer_payment_allocations(consumer):
    if consumer is None:
        return []

    payments = list(
        Payment.objects.filter(consumer=consumer, status=Payment.Statuses.COMPLETED)
        .select_related('billing')
        .order_by('payment_date', 'created_at', 'id')
    )
    billings = sorted(
        get_consumer_monthly_billings(consumer),
        key=lambda billing: (month_start(billing.billing_month) or date.max, billing.created_at, billing.id),
    )

    if payments:
        PaymentAllocation.objects.filter(payment__in=payments).delete()

    arrangements_by_payment_id = {
        arrangement.payment_id: arrangement
        for arrangement in PaymentArrangement.objects.filter(payment__in=payments)
        if arrangement.payment_id
    }

    for billing in billings:
        billing.amount_paid = Decimal('0')

    allocations = []
    for payment in payments:
        remaining_credit = payment.amount_credited
        arrangement = arrangements_by_payment_id.get(payment.id)
        if arrangement and arrangement.selected_billings:
            selected_ids = [str(item.get('billing_id')) for item in arrangement.selected_billings if item.get('billing_id')]
            candidate_billings = [billing for billing in billings if str(billing.id) in selected_ids]
            candidate_billings.sort(
                key=lambda billing: selected_ids.index(str(billing.id)) if str(billing.id) in selected_ids else len(selected_ids)
            )
        else:
            covered_month = month_start(payment.display_covered_month)
            candidate_billings = [
                billing
                for billing in billings
                if covered_month is None or (month_start(billing.billing_month) and month_start(billing.billing_month) <= covered_month)
            ]

        for billing in candidate_billings:
            if remaining_credit <= 0:
                break

            balance = billing.amount_due
            if balance <= 0:
                continue

            applied = remaining_credit if remaining_credit <= balance else balance
            billing.amount_paid += applied
            remaining_credit -= applied
            allocations.append(PaymentAllocation(payment=payment, billing=billing, amount_applied=applied))

    if allocations:
        PaymentAllocation.objects.bulk_create(allocations)

    for billing in billings:
        billing.save(update_fields=['amount_paid', 'status'])

    refresh_consumer_account_status(consumer)

    return billings


def _sync_advance_payments_to_billing(billing, force_pending_status=False):
    Payment.objects.filter(
        consumer=billing.consumer,
        billing__isnull=True,
        covered_month=month_start(billing.billing_month),
    ).update(billing=billing)

    rebuild_consumer_payment_allocations(billing.consumer)
    billing.refresh_from_db()

    if force_pending_status:
        BillingRecord.objects.filter(pk=billing.pk).update(
            status=BillingRecord.Statuses.PENDING,
        )
        billing.status = BillingRecord.Statuses.PENDING
        return billing

    return billing


def create_or_update_billing_from_reading(reading, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    billing = get_existing_billing_for_month(reading.consumer, reading.billing_month)
    if billing is None:
        billing = BillingRecord(
            consumer=reading.consumer,
            billing_month=reading.billing_month,
            amount_paid=Decimal('0'),
        )

    billing.previous_reading = reading.previous_reading
    billing.current_reading = reading.current_reading
    billing.rate_per_m3 = get_effective_rate_per_m3(reading.consumer, system_settings=system_settings)
    billing.previous_arrears = get_previous_arrears_balance(reading.consumer, reading.billing_month)
    billing.billing_date = reading.reading_date
    billing.due_date = reading.reading_date + timedelta(days=system_settings.billing_due_days)
    billing.save()
    billing = _sync_advance_payments_to_billing(billing, force_pending_status=True)
    refresh_consumer_account_status(reading.consumer)
    return billing


def sync_existing_billings_with_settings(system_settings=None, consumer=None):
    system_settings = system_settings or SystemSettings.load()
    updated_records = 0

    queryset = BillingRecord.objects.all()
    if consumer is not None:
        queryset = queryset.filter(consumer=consumer)

    for billing in queryset:
        baseline_date = billing.billing_date or billing.billing_month
        expected_due_date = (
            baseline_date + timedelta(days=system_settings.billing_due_days)
            if baseline_date
            else billing.due_date
        )
        resolved_rate = get_effective_rate_per_m3(billing.consumer, system_settings=system_settings)
        should_update = billing.rate_per_m3 != resolved_rate
        should_update = should_update or bool(expected_due_date and billing.due_date != expected_due_date)

        if not should_update:
            continue

        billing.rate_per_m3 = resolved_rate
        billing.previous_arrears = get_previous_arrears_balance(billing.consumer, billing.billing_month)
        if expected_due_date:
            billing.due_date = expected_due_date
        billing.save()
        _sync_advance_payments_to_billing(billing)
        refresh_consumer_account_status(billing.consumer)
        updated_records += 1

    return updated_records


def handle_meter_reading_submission(form, submitted_by):
    system_settings = SystemSettings.load()
    consumer = form.cleaned_data['consumer']
    reading_date = form.cleaned_data['reading_date']
    billing_month = reading_date.replace(day=1)
    previous_reading = get_previous_reading_for_month(consumer, billing_month)
    existing_reading = MeterReading.objects.filter(consumer=consumer, billing_month=billing_month).first()
    if existing_reading is not None:
        raise ValidationError('A meter reading for this billing cycle already exists. Use the edit action to correct the submitted reading.')

    reading = MeterReading.objects.create(
        consumer=consumer,
        billing_month=billing_month,
        reading_date=reading_date,
        previous_reading=previous_reading,
        current_reading=form.cleaned_data['current_reading'],
        notes=form.cleaned_data.get('notes', ''),
        submitted_by=submitted_by,
    )
    billing = create_or_update_billing_from_reading(reading, system_settings)

    staff_message = (
        f'New meter reading for {consumer.full_name}: current reading {reading.current_reading}, '
        f'usage {reading.usage_m3} cubic meters.'
    )
    consumer_message, consumer_email_html = _professional_notification_messages(
        'Meter Reading Statement',
        f'Dear {consumer.full_name}, your latest meter reading has been posted and your billing record has been updated.',
        [
            ('Billing Month', f'{reading.billing_month:%B %Y}'),
            ('Previous Reading', f'{reading.previous_reading} m³'),
            ('Current Reading', f'{reading.current_reading} m³'),
            ('Usage', f'{reading.usage_m3} m³'),
            ('Current Bill', f'PHP {billing.current_bill_amount}'),
            ('Previous Arrears', f'PHP {billing.previous_arrears}'),
            ('Total Outstanding', f'PHP {billing.statement_total_snapshot}'),
            ('Due Date', f'{billing.due_date:%B %d, %Y}'),
        ],
        total_label='TOTAL DUE',
        total_value=f'PHP {billing.statement_total_snapshot}',
        footer_note='Please settle your bill on or before the due date to avoid penalties.',
    )
    consumer_sms_message = (
        f'Tabuan Water Billing: Reading posted for {reading.billing_month:%b %Y}. '
        f'Current bill PHP {billing.current_bill_amount}. Arrears PHP {billing.previous_arrears}. '
        f'Total outstanding PHP {billing.statement_total_snapshot}. '
        f'Due {billing.due_date:%b %d, %Y}.'
    )

    notify_roles(
        {ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY},
        'New meter reading submitted',
        staff_message,
        Notification.Types.READING,
        consumer=consumer,
        billing=billing,
        meter_reading=reading,
    )
    notify_consumer(
        consumer,
        'New meter reading recorded',
        consumer_message,
        Notification.Types.READING,
        send_email=system_settings.notify_by_email,
        send_sms=system_settings.notify_by_sms,
        email_html_message=consumer_email_html,
        sms_message=consumer_sms_message,
        billing=billing,
        meter_reading=reading,
    )
    send_billing_due_notification(billing, system_settings=system_settings)

    return reading, billing


def send_billing_due_notification(billing, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    message, email_html = _professional_notification_messages(
        'Statement of Account',
        f'Dear {billing.consumer.full_name}, your water bill for {billing.billing_month:%B %Y} is now available.',
        [
            ('Billing Month', f'{billing.billing_month:%B %Y}'),
            ('Previous Reading', f'{billing.previous_reading} m³'),
            ('Current Reading', f'{billing.current_reading} m³'),
            ('Consumption', f'{billing.usage_m3} m³'),
            ('Rate', f'PHP {billing.rate_per_m3} per m³'),
            ('Current Bill', f'PHP {billing.current_bill_amount}'),
            ('Previous Arrears', f'PHP {billing.previous_arrears}'),
            ('Service Charges', f'PHP {billing.service_charge}'),
            ('Penalties', f'PHP {billing.penalty_amount}'),
            ('Outstanding Balance', f'PHP {billing.statement_total_snapshot}'),
            ('Due Date', f'{billing.due_date:%B %d, %Y}'),
        ],
        total_label='TOTAL OUTSTANDING',
        total_value=f'PHP {billing.statement_total_snapshot}',
        footer_note='Payment is due on or before the due date shown above.',
    )
    sms_message = (
        f'Tabuan Water Billing: SOA for {billing.billing_month:%b %Y}. '
        f'Current bill PHP {billing.current_bill_amount}. Arrears PHP {billing.previous_arrears}. '
        f'Total outstanding PHP {billing.statement_total_snapshot}. Due {billing.due_date:%b %d, %Y}.'
    )
    return notify_consumer(
        billing.consumer,
        'Water bill due notice',
        message,
        Notification.Types.BILL_DUE,
        send_email=system_settings.notify_by_email,
        send_sms=system_settings.notify_by_sms,
        email_html_message=email_html,
        sms_message=sms_message,
        billing=billing,
    )


def send_payment_notification(payment, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    covered_month = payment.display_covered_month
    covered_label = covered_month.strftime('%B %Y') if covered_month else 'the selected billing month'
    message, email_html = _professional_notification_messages(
        'Payment Confirmation',
        f'Dear {payment.consumer.full_name}, this confirms the latest status of your water billing payment.',
        [
            ('Covered Month', covered_label),
            ('Payment Date', f'{payment.payment_date:%B %d, %Y}'),
            ('Payment Method', payment.display_payment_method),
            ('Payment Option', payment.get_payment_option_display()),
            ('Payment Status', payment.get_status_display()),
            ('Reference Number', payment.display_reference_number),
            ('Discount Applied', f'PHP {payment.discount_amount}'),
        ],
        total_label='AMOUNT PAID',
        total_value=f'PHP {payment.amount_paid}',
        footer_note='Please keep this confirmation for your records.',
    )
    sms_message = (
        f'Tabuan Water Billing: Payment for {covered_label} is {payment.get_status_display().lower()}. '
        f'{payment.get_payment_option_display()}. Amount PHP {payment.amount_paid}. '
        f'Ref: {payment.display_reference_number}'
    )
    return notify_consumer(
        payment.consumer,
        'Payment status update',
        message,
        Notification.Types.PAYMENT,
        send_email=system_settings.notify_by_email,
        send_sms=system_settings.notify_by_sms,
        email_html_message=email_html,
        sms_message=sms_message,
        payment=payment,
        billing=payment.billing,
    )


def update_payment_status(payment, new_status, system_settings=None):
    if new_status not in Payment.Statuses.values:
        raise ValueError('Invalid payment status.')

    previous_status = payment.status
    if previous_status != new_status:
        payment.status = new_status
        payment.save(update_fields=['status'])

    arrangement = getattr(payment, 'arrangement', None)
    if arrangement is not None:
        arrangement_status = arrangement.status
        if new_status == Payment.Statuses.COMPLETED:
            arrangement_status = PaymentArrangement.Statuses.COMPLETED
        elif new_status == Payment.Statuses.FAILED and arrangement.status == PaymentArrangement.Statuses.COMPLETED:
            arrangement_status = PaymentArrangement.Statuses.APPROVED
        if arrangement.status != arrangement_status:
            arrangement.status = arrangement_status
            arrangement.save(update_fields=['status', 'updated_at'])

    if payment.billing_id:
        payment.billing.refresh_from_db()

    refresh_consumer_account_status(payment.consumer)

    notifications = send_payment_notification(payment, system_settings=system_settings)
    return previous_status, notifications


def get_audience_consumers(audience):
    consumers = list(Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).order_by('full_name'))
    if audience == SMSBlast.Audiences.OVERDUE:
        return [
            consumer
            for consumer in consumers
            if any(resolve_billing_status(billing) == BillingRecord.Statuses.OVERDUE for billing in get_consumer_monthly_billings(consumer))
        ]
    if audience == SMSBlast.Audiences.PENDING:
        return [
            consumer
            for consumer in consumers
            if get_consumer_outstanding_balance(consumer) > 0
        ]
    return consumers


def send_sms_blast(sent_by, audience, message):
    blast = SMSBlast.objects.create(sent_by=sent_by, audience=audience, message=message, provider=sms_provider_name())
    recipients = list(get_audience_consumers(audience))
    sent_count = 0
    failed_count = 0
    recipient_names = []

    for consumer in recipients:
        recipient_names.append(consumer.full_name)
        result = send_sms_notification(consumer, message, Notification.Types.ADMIN)
        if result.status == Notification.Statuses.SENT:
            sent_count += 1
        else:
            failed_count += 1
        if consumer.portal_user:
            create_in_app_notification(
                consumer.portal_user,
                'Message from the water billing office',
                message,
                Notification.Types.ADMIN,
                consumer=consumer,
            )

    blast.mark_complete(sent_count, failed_count, ', '.join(recipient_names))
    return blast


def send_email_blast(audience, subject, message):
    recipients = list(get_audience_consumers(audience))
    sent_count = 0
    failed_count = 0

    for consumer in recipients:
        result = send_email_notification(consumer, subject, message, Notification.Types.ADMIN)
        if result.status == Notification.Statuses.SENT:
            sent_count += 1
        else:
            failed_count += 1
        if consumer.portal_user:
            create_in_app_notification(
                consumer.portal_user,
                subject,
                message,
                Notification.Types.ADMIN,
                consumer=consumer,
            )

    return {
        'total': len(recipients),
        'sent': sent_count,
        'failed': failed_count,
    }
