from .models import Notification, SystemSettings
from .permissions import (
    ADMIN_ROLES,
    PAYMENT_MANAGER_ROLES,
    READING_ENTRY_ROLES,
    REPORT_ROLES,
    get_dashboard_url_for_user,
    get_linked_consumer,
    get_user_profile,
    get_user_role,
)


def billing_app_context(request):
    if not request.user.is_authenticated:
        return {}

    role = get_user_role(request.user, create=True)
    profile = get_user_profile(request.user)
    unread_notifications = Notification.objects.filter(
        recipient=request.user,
        channel=Notification.Channels.IN_APP,
        is_read=False,
    ).count()

    return {
        'current_role': role,
        'current_profile': profile,
        'linked_consumer': get_linked_consumer(request.user),
        'unread_notifications': unread_notifications,
        'dashboard_url_name': get_dashboard_url_for_user(request.user),
        'can_manage_consumers': role in ADMIN_ROLES,
        'can_view_reports': role in REPORT_ROLES,
        'can_manage_payments': role in PAYMENT_MANAGER_ROLES,
        'can_manage_settings': role in ADMIN_ROLES,
        'can_input_meter_readings': role in READING_ENTRY_ROLES,
        'payment_settings': SystemSettings.load(),
    }
