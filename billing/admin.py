from django.contrib import admin

from .models import (
    AuditLog,
    BillingRecord,
    Consumer,
    ConsumerProfile,
    DisconnectionRecord,
    MeetingMinutes,
    MeetingMinutesRevision,
    MeterReading,
    Notification,
    Payment,
    PaymentArrangement,
    SMSBlast,
    SystemSettings,
)
from .services import send_payment_notification


@admin.register(ConsumerProfile)
class ConsumerProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'user', 'role', 'contact', 'created_at')
    search_fields = ('full_name', 'user__username', 'email')


@admin.register(Consumer)
class ConsumerAdmin(admin.ModelAdmin):
    list_display = ('id', 'full_name', 'contact_number', 'status', 'account_status', 'created_at')
    search_fields = ('full_name', 'address', 'contact_number')
    list_filter = ('status', 'account_status')


@admin.register(BillingRecord)
class BillingRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'billing_month', 'total_amount', 'amount_paid', 'status', 'due_date')
    search_fields = ('consumer__full_name',)
    list_filter = ('status', 'billing_month')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'payment_method', 'settlement_scope', 'amount_paid', 'payment_date', 'status', 'gateway_status')
    search_fields = ('consumer__full_name', 'reference_number', 'gateway_reference', 'gateway_payment_id')
    list_filter = ('status', 'payment_method', 'gateway', 'settlement_scope')
    list_editable = ('status',)

    def save_model(self, request, obj, form, change):
        status_changed = change and 'status' in form.changed_data
        super().save_model(request, obj, form, change)
        if status_changed:
            send_payment_notification(obj)


@admin.register(MeterReading)
class MeterReadingAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'billing_month', 'current_reading', 'usage_m3', 'reading_date', 'submitted_by')
    search_fields = ('consumer__full_name',)
    list_filter = ('billing_month', 'reading_date')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('id', 'title', 'channel', 'notification_type', 'consumer', 'recipient', 'status', 'created_at')
    search_fields = ('title', 'message', 'consumer__full_name', 'recipient__username')
    list_filter = ('channel', 'notification_type', 'status', 'is_read')


@admin.register(PaymentArrangement)
class PaymentArrangementAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'arrangement_type', 'status', 'requested_amount', 'approved_by', 'approved_at', 'created_at')
    search_fields = ('consumer__full_name', 'notes', 'requested_by__username', 'approved_by__username')
    list_filter = ('status', 'arrangement_type', 'approved_at')


@admin.register(DisconnectionRecord)
class DisconnectionRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'status', 'outstanding_balance', 'unpaid_months_count', 'scheduled_disconnection_date', 'confirmed_at')
    search_fields = ('consumer__full_name', 'notes')
    list_filter = ('status', 'scheduled_disconnection_date', 'confirmed_at')


@admin.register(SMSBlast)
class SMSBlastAdmin(admin.ModelAdmin):
    list_display = ('id', 'audience', 'status', 'provider', 'total_recipients', 'sent_count', 'failed_count', 'sent_at')
    search_fields = ('message',)
    list_filter = ('audience', 'status', 'provider')


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    list_display = (
        'rate_per_m3',
        'billing_due_days',
        'enable_cash_payments',
        'enable_online_payments',
        'notify_by_email',
        'notify_by_sms',
        'updated_at',
    )


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'user', 'role', 'action', 'target', 'created_at')
    search_fields = ('user__username', 'role', 'action', 'target', 'details')
    list_filter = ('role', 'action', 'created_at')


class MeetingMinutesRevisionInline(admin.TabularInline):
    model = MeetingMinutesRevision
    extra = 0
    can_delete = False
    fields = ('revision_number', 'edited_by', 'change_summary', 'created_at')
    readonly_fields = ('revision_number', 'edited_by', 'change_summary', 'created_at')


@admin.register(MeetingMinutes)
class MeetingMinutesAdmin(admin.ModelAdmin):
    list_display = ('title', 'secretary', 'meeting_date', 'status', 'approved_at', 'updated_at')
    search_fields = ('title', 'secretary__username', 'secretary__consumerprofile__full_name', 'location')
    list_filter = ('status', 'meeting_date', 'updated_at')
    readonly_fields = ('created_at', 'updated_at', 'approved_at')
    inlines = [MeetingMinutesRevisionInline]


@admin.register(MeetingMinutesRevision)
class MeetingMinutesRevisionAdmin(admin.ModelAdmin):
    list_display = ('meeting_minutes', 'revision_number', 'edited_by', 'created_at')
    search_fields = ('meeting_minutes__title', 'edited_by__username', 'change_summary')
    list_filter = ('created_at',)
    readonly_fields = ('meeting_minutes', 'edited_by', 'revision_number', 'change_summary', 'changed_fields', 'snapshot', 'created_at')
