from django.contrib import admin

from .models import AuditLog, BillingRecord, Consumer, ConsumerProfile, MeterReading, Notification, Payment, SMSBlast, SystemSettings
from .services import send_payment_notification


@admin.register(ConsumerProfile)
class ConsumerProfileAdmin(admin.ModelAdmin):
    list_display = ('full_name', 'user', 'role', 'contact', 'created_at')
    search_fields = ('full_name', 'user__username', 'email')


@admin.register(Consumer)
class ConsumerAdmin(admin.ModelAdmin):
    list_display = ('id', 'full_name', 'contact_number', 'status', 'created_at')
    search_fields = ('full_name', 'address', 'contact_number')
    list_filter = ('status',)


@admin.register(BillingRecord)
class BillingRecordAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'billing_month', 'total_amount', 'amount_paid', 'status', 'due_date')
    search_fields = ('consumer__full_name',)
    list_filter = ('status', 'billing_month')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'consumer', 'payment_method', 'amount_paid', 'payment_date', 'status', 'gateway_status')
    search_fields = ('consumer__full_name', 'reference_number', 'gateway_reference', 'gateway_payment_id')
    list_filter = ('status', 'payment_method', 'gateway')
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
