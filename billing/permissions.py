from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect

from .models import ConsumerProfile


ADMIN_ROLES = {ConsumerProfile.Roles.ADMIN}
REPORT_ROLES = {
    ConsumerProfile.Roles.ADMIN,
    ConsumerProfile.Roles.SECRETARY,
    ConsumerProfile.Roles.TREASURER,
}
PAYMENT_MANAGER_ROLES = {
    ConsumerProfile.Roles.ADMIN,
    ConsumerProfile.Roles.TREASURER,
}
READING_ENTRY_ROLES = {
    ConsumerProfile.Roles.ADMIN,
    ConsumerProfile.Roles.READER,
}
STAFF_ROLES = {
    ConsumerProfile.Roles.ADMIN,
    ConsumerProfile.Roles.SECRETARY,
    ConsumerProfile.Roles.TREASURER,
    ConsumerProfile.Roles.READER,
}


def ensure_user_profile(user):
    if not getattr(user, 'is_authenticated', False):
        return None

    default_role = ConsumerProfile.Roles.ADMIN if (user.is_staff or user.is_superuser) else ConsumerProfile.Roles.CONSUMER
    full_name = user.get_full_name() or user.username
    profile, _ = ConsumerProfile.objects.get_or_create(
        user=user,
        defaults={
            'full_name': full_name,
            'email': user.email or '',
            'role': default_role,
        },
    )

    changed = False
    if user.is_staff or user.is_superuser:
        if profile.role != ConsumerProfile.Roles.ADMIN:
            profile.role = ConsumerProfile.Roles.ADMIN
            changed = True
    if not profile.full_name:
        profile.full_name = full_name
        changed = True
    if user.email and not profile.email:
        profile.email = user.email
        changed = True
    if changed:
        profile.save()

    return profile


def get_user_profile(user, create=False):
    if create:
        return ensure_user_profile(user)
    if not getattr(user, 'is_authenticated', False):
        return None
    return ConsumerProfile.objects.filter(user=user).first()


def get_user_role(user, create=False):
    if not getattr(user, 'is_authenticated', False):
        return None
    if user.is_staff or user.is_superuser:
        return ConsumerProfile.Roles.ADMIN
    profile = get_user_profile(user, create=create)
    return profile.role if profile else ConsumerProfile.Roles.CONSUMER


def get_linked_consumer(user):
    profile = get_user_profile(user)
    if not profile:
        return None
    return getattr(profile, 'consumer_record', None)


def get_dashboard_route(role):
    return {
        ConsumerProfile.Roles.ADMIN: 'admin_panel',
        ConsumerProfile.Roles.SECRETARY: 'secretary_panel',
        ConsumerProfile.Roles.TREASURER: 'treasurer_panel',
        ConsumerProfile.Roles.READER: 'reader_panel',
        ConsumerProfile.Roles.CONSUMER: 'consumer_panel',
    }.get(role, 'consumer_panel')


def get_dashboard_url_for_user(user):
    return get_dashboard_route(get_user_role(user, create=True))


def build_navigation_items(role):
    items = [
        {'label': 'Dashboard', 'url_name': get_dashboard_route(role)},
    ]

    if role in ADMIN_ROLES:
        items.append({'label': 'Consumers', 'url_name': 'consumers'})

    if role in {ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER}:
        items.extend(
            [
                {'label': 'Billing', 'url_name': 'billing'},
                {'label': 'Payments', 'url_name': 'payments'},
            ]
        )

    if role in REPORT_ROLES:
        items.append({'label': 'Reports', 'url_name': 'reports'})

    if role == ConsumerProfile.Roles.ADMIN:
        items.extend(
            [
                {'label': 'Communications', 'url_name': 'communications'},
                {'label': 'Settings', 'url_name': 'payment_settings'},
            ]
        )

    if role in READING_ENTRY_ROLES:
        items.append({'label': 'Meter Readings', 'url_name': 'reader_panel'})

    if role == ConsumerProfile.Roles.CONSUMER:
        items.append({'label': 'Notifications', 'url_name': 'notifications'})

    items.append({'label': 'Profile', 'url_name': 'profile'})
    return items


def role_required(*allowed_roles):
    def decorator(view_func):
        @login_required
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            role = get_user_role(request.user, create=True)
            if role not in allowed_roles:
                messages.error(request, 'You do not have permission to access that page.')
                return redirect(get_dashboard_url_for_user(request.user))
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator
