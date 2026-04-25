import base64
import json
import re
from datetime import timedelta
from decimal import Decimal
from urllib import error, parse, request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db.models import Q

from .models import BillingRecord, Consumer, ConsumerProfile, MeterReading, Notification, Payment, SMSBlast, SystemSettings


User = get_user_model()


def _truncate_response_text(value, limit=400):
    if not value:
        return ''
    value = value.strip()
    return value if len(value) <= limit else f'{value[:limit]}...'


def email_provider_name():
    return (getattr(settings, 'EMAIL_DELIVERY_PROVIDER', 'console') or 'console').lower()


def sms_provider_name():
    return (getattr(settings, 'SMS_DELIVERY_PROVIDER', 'twilio') or 'twilio').lower()


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
        'sms_provider_label': sms_provider.title(),
        'sms_configured': is_sms_delivery_configured(),
        'sms_setup_note': (
            'Use your Twilio Account SID (AC...) plus either an Auth Token or an API Key SID/Secret. '
            'Do not use OAuth Client ID/Client Secret values for SMS delivery.'
        ),
        'sms_error': _twilio_configuration_error() if sms_provider == 'twilio' else '',
        'sms_env_keys': [
            'SMS_DELIVERY_PROVIDER',
            'TWILIO_ACCOUNT_SID',
            'TWILIO_AUTH_TOKEN or',
            'TWILIO_API_KEY_SID',
            'TWILIO_API_KEY_SECRET',
            'TWILIO_PHONE_NUMBER',
        ],
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


def _send_via_smtp(email_address, subject, message):
    provider_configured = is_email_delivery_configured()
    if not provider_configured:
        raise ValueError(
            'SMTP email settings are incomplete. Add EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, and DEFAULT_FROM_EMAIL in the .env file.'
        )

    send_mail(
        subject,
        message,
        getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@waterbilling.local'),
        [email_address],
        fail_silently=False,
    )
    return f'SMTP email sent to {email_address}.'


def send_email_notification(consumer, subject, message, notification_type, **related_objects):
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
            response_message = _send_via_sendgrid(email_address, subject, message)
        elif provider == 'smtp':
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

    provider = sms_provider_name()
    account_sid = getattr(settings, 'TWILIO_ACCOUNT_SID', '')
    auth_token = getattr(settings, 'TWILIO_AUTH_TOKEN', '')
    api_key_sid = getattr(settings, 'TWILIO_API_KEY_SID', '')
    api_key_secret = getattr(settings, 'TWILIO_API_KEY_SECRET', '')
    from_number = normalize_phone_number(getattr(settings, 'TWILIO_PHONE_NUMBER', ''))

    if provider != 'twilio':
        return _log_outbound_notification(
            Notification.Channels.SMS,
            'SMS Notification',
            message,
            Notification.Statuses.FAILED,
            consumer=consumer,
            response_message=f'Unsupported SMS provider "{provider}". Set SMS_DELIVERY_PROVIDER=twilio for live delivery.',
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


def notify_consumer(consumer, title, message, notification_type, send_email=False, send_sms=False, **related_objects):
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
        results['email'] = send_email_notification(consumer, title, message, notification_type, **related_objects)

    if send_sms:
        results['sms'] = send_sms_notification(consumer, message, notification_type, **related_objects)

    return results


def get_previous_reading_for_month(consumer, billing_month):
    prior_reading = (
        MeterReading.objects.filter(consumer=consumer, billing_month__lt=billing_month)
        .order_by('-billing_month', '-created_at')
        .first()
    )
    if prior_reading:
        return prior_reading.current_reading

    prior_billing = (
        BillingRecord.objects.filter(consumer=consumer, billing_month__lt=billing_month)
        .order_by('-billing_month', '-created_at')
        .first()
    )
    if prior_billing:
        return prior_billing.current_reading

    return Decimal('0')


def create_or_update_billing_from_reading(reading, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    billing = BillingRecord.objects.filter(consumer=reading.consumer, billing_month=reading.billing_month).first()
    if billing is None:
        billing = BillingRecord(
            consumer=reading.consumer,
            billing_month=reading.billing_month,
            amount_paid=Decimal('0'),
        )

    billing.previous_reading = reading.previous_reading
    billing.current_reading = reading.current_reading
    billing.rate_per_m3 = system_settings.rate_per_m3
    billing.billing_date = reading.reading_date
    billing.due_date = reading.reading_date + timedelta(days=system_settings.billing_due_days)
    billing.save()
    return billing


def handle_meter_reading_submission(form, submitted_by):
    system_settings = SystemSettings.load()
    consumer = form.cleaned_data['consumer']
    reading_date = form.cleaned_data['reading_date']
    billing_month = reading_date.replace(day=1)
    previous_reading = get_previous_reading_for_month(consumer, billing_month)

    reading, _ = MeterReading.objects.update_or_create(
        consumer=consumer,
        billing_month=billing_month,
        defaults={
            'reading_date': reading_date,
            'previous_reading': previous_reading,
            'current_reading': form.cleaned_data['current_reading'],
            'notes': form.cleaned_data.get('notes', ''),
            'submitted_by': submitted_by,
        },
    )
    billing = create_or_update_billing_from_reading(reading, system_settings)

    staff_message = (
        f'New meter reading for {consumer.full_name}: current reading {reading.current_reading}, '
        f'usage {reading.usage_m3} cubic meters.'
    )
    consumer_message = (
        f'Your meter reading for {reading.billing_month:%B %Y} has been posted. '
        f'Usage: {reading.usage_m3} cubic meters. Bill total: PHP {billing.total_amount}. '
        f'Due date: {billing.due_date:%B %d, %Y}.'
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
        billing=billing,
        meter_reading=reading,
    )

    return reading, billing


def send_billing_due_notification(billing, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    message = (
        f'Your water bill for {billing.billing_month:%B %Y} is now available. '
        f'Amount due: PHP {billing.total_amount}. Due date: {billing.due_date:%B %d, %Y}.'
    )
    return notify_consumer(
        billing.consumer,
        'Water bill due notice',
        message,
        Notification.Types.BILL_DUE,
        send_email=system_settings.notify_by_email,
        send_sms=system_settings.notify_by_sms,
        billing=billing,
    )


def send_payment_notification(payment, system_settings=None):
    system_settings = system_settings or SystemSettings.load()
    message = (
        f'Your payment of PHP {payment.amount_paid} for {payment.consumer.full_name} was marked as '
        f'{payment.get_status_display().lower()} on {payment.payment_date:%B %d, %Y}.'
    )
    return notify_consumer(
        payment.consumer,
        'Payment status update',
        message,
        Notification.Types.PAYMENT,
        send_email=system_settings.notify_by_email,
        send_sms=system_settings.notify_by_sms,
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

    if payment.billing_id:
        payment.billing.refresh_from_db()

    notifications = send_payment_notification(payment, system_settings=system_settings)
    return previous_status, notifications


def get_audience_consumers(audience):
    queryset = Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).order_by('full_name')
    if audience == SMSBlast.Audiences.OVERDUE:
        queryset = queryset.filter(billings__status=BillingRecord.Statuses.OVERDUE).distinct()
    elif audience == SMSBlast.Audiences.PENDING:
        queryset = queryset.filter(billings__status=BillingRecord.Statuses.PENDING).distinct()
    return queryset


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
