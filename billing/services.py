import base64
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from urllib import error, parse, request

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.db.models import Q
from django.utils import timezone

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


def _sms_api_ph_configuration_error():
    api_key = getattr(settings, 'SMS_API_PH_API_KEY', '')
    endpoint = getattr(settings, 'SMS_API_PH_ENDPOINT', '')

    if not endpoint:
        return 'SMS_API_PH_ENDPOINT is missing.'
    if not api_key:
        return 'SMS_API_PH_API_KEY is missing.'
    if not api_key.startswith('sk-'):
        return 'SMS_API_PH_API_KEY should start with sk-.'
    return ''


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
            'Use your SMS API Philippines API key. Requests are sent with the x-api-key header '
            'to the configured SMS_API_PH_ENDPOINT.'
        )
        sms_error = _sms_api_ph_configuration_error()
        sms_env_keys = [
            'SMS_DELIVERY_PROVIDER',
            'SMS_API_PH_ENDPOINT',
            'SMS_API_PH_API_KEY',
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


def _send_via_sms_api_ph(phone_number, message):
    config_error = _sms_api_ph_configuration_error()
    if config_error:
        raise ValueError(config_error)

    payload = json.dumps({'recipient': phone_number, 'message': message}).encode()
    sms_request = request.Request(
        getattr(settings, 'SMS_API_PH_ENDPOINT', ''),
        data=payload,
        headers={
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-api-key': getattr(settings, 'SMS_API_PH_API_KEY', ''),
        },
        method='POST',
    )

    with request.urlopen(sms_request, timeout=getattr(settings, 'SMS_API_TIMEOUT', 10)) as response:  # pragma: no cover - network-dependent
        body = response.read().decode()

    # Parse and validate the response
    status, message_id, error = _parse_sms_api_ph_response(body)
    
    if status == 'failed':
        raise ValueError(error or 'SMS API PH rejected the message')
    
    # Return success response with optional message ID
    if message_id:
        return f'SMS API PH accepted (ID: {message_id}) for {phone_number}'
    return f'SMS API PH accepted the SMS for {phone_number}'


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


def create_paymongo_ewallet_payment(payment, success_url, cancel_url):
    ewallet_type = _paymongo_ewallet_type()
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

    return {
        'intent_id': payment_intent.get('id', ''),
        'intent_status': attributes.get('status', ''),
        'payment_id': latest_payment_id if isinstance(latest_payment_id, str) else '',
        'payment_status': latest_payment_attributes.get('status', ''),
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


def month_start(value):
    if not value:
        return None
    return value.replace(day=1)


def add_months(base_month, months):
    normalized = month_start(base_month)
    month_index = normalized.month - 1 + months
    return date(normalized.year + month_index // 12, (month_index % 12) + 1, 1)


def _billing_rank(record):
    return (
        record.total_amount or Decimal('0'),
        record.amount_paid or Decimal('0'),
        record.current_reading or Decimal('0'),
        record.created_at,
    )


def get_consumer_monthly_billings(consumer, limit=None):
    if consumer is None:
        return []

    billings_by_month = {}
    for record in consumer.billings.all().order_by('-billing_month', '-created_at'):
        billing_month = month_start(record.billing_month)
        current = billings_by_month.get(billing_month)
        if current is None or _billing_rank(record) > _billing_rank(current):
            billings_by_month[billing_month] = record

    months = sorted(billings_by_month.keys(), reverse=True)
    records = [billings_by_month[item] for item in months]
    return records[:limit] if limit else records


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

    payment_totals = _payment_totals_by_month(consumer)
    monthly_billings = list(reversed(get_consumer_monthly_billings(consumer)))

    for billing in monthly_billings:
        billing_month = month_start(billing.billing_month)
        credited_amount = payment_totals.get(billing_month, Decimal('0'))
        if credited_amount < (billing.total_amount or Decimal('0')):
            return billing_month

    covered_months = set(payment_totals.keys())
    covered_months.update(
        month_start(billing.billing_month)
        for billing in monthly_billings
        if billing.status == BillingRecord.Statuses.PAID
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
    choices = []
    for offset in range(advance_months + 1):
        candidate = add_months(next_month, offset)
        label = candidate.strftime('%B %Y')
        if offset == 0:
            label = f'{label} (Next due)'
        elif offset > 0:
            label = f'{label} (Advance)'
        choices.append((candidate.strftime('%Y-%m'), label))
    return choices


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

    prior_billing = (
        BillingRecord.objects.filter(consumer=consumer, billing_month__lt=target_month)
        .order_by('-billing_month', '-created_at')
        .first()
    )
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


def _sync_advance_payments_to_billing(billing):
    Payment.objects.filter(
        consumer=billing.consumer,
        billing__isnull=True,
        covered_month=month_start(billing.billing_month),
    ).update(billing=billing)

    billing.amount_paid = sum(
        payment.amount_credited
        for payment in billing.payments.filter(status=Payment.Statuses.COMPLETED).only('amount_paid', 'discount_amount')
    )
    billing.save(update_fields=['amount_paid', 'usage_m3', 'total_amount', 'status'])
    return billing


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
    return _sync_advance_payments_to_billing(billing)


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
    covered_month = payment.display_covered_month
    covered_label = covered_month.strftime('%B %Y') if covered_month else 'the selected billing month'
    discount_message = f' Discount applied: PHP {payment.discount_amount}.' if payment.discount_amount else ''
    message = (
        f'Your payment for {covered_label} was marked as {payment.get_status_display().lower()} '
        f'on {payment.payment_date:%B %d, %Y}. Amount paid: PHP {payment.amount_paid}.{discount_message}'
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
