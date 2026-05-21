import re
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.files.storage import default_storage
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Sum
from django.utils.crypto import get_random_string
from django.utils import timezone


MINIMUM_BILLABLE_USAGE_M3 = Decimal('30')


def calculate_usage_m3(previous_reading, current_reading):
    previous_value = previous_reading or Decimal('0')
    current_value = current_reading or Decimal('0')
    if current_value < previous_value:
        raise ValidationError('Current reading cannot be lower than the previous reading.')

    usage_value = current_value - previous_value
    return usage_value if usage_value >= MINIMUM_BILLABLE_USAGE_M3 else MINIMUM_BILLABLE_USAGE_M3


def _safe_file_url(field_file):
    if not field_file or not getattr(field_file, 'name', ''):
        return ''
    try:
        if default_storage.exists(field_file.name):
            return field_file.url
    except Exception:
        return ''
    return ''


def _name_initial(name, fallback='U'):
    cleaned = str(name or '').strip()
    return cleaned[:1].upper() if cleaned else fallback


def _meter_name_prefix(full_name):
    parts = [segment for segment in re.split(r'\s+', str(full_name or '').strip()) if segment]
    particles = {'DE', 'DELA', 'DEL', 'DELOS', 'DELAS', 'VAN', 'VON', 'SAN', 'SANTA', 'STA'}
    if parts:
        selected = [parts[-1]]
        index = len(parts) - 2
        while index >= 0 and re.sub(r'[^A-Za-z0-9]', '', parts[index]).upper() in particles:
            selected.insert(0, parts[index])
            index -= 1
        seed = ''.join(selected)
    else:
        seed = 'CONSUMER'
    normalized = re.sub(r'[^A-Za-z0-9]', '', seed).upper()
    return normalized[:12] or 'CONSUMER'


class ConsumerProfile(models.Model):
    class Roles(models.TextChoices):
        ADMIN = 'admin', 'Admin'
        SECRETARY = 'secretary', 'Secretary'
        TREASURER = 'treasurer', 'Treasurer'
        READER = 'reader', 'Reading Panel'
        CONSUMER = 'consumer', 'Consumer'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    full_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True)
    contact = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    role = models.CharField(max_length=20, choices=Roles.choices, default=Roles.CONSUMER)
    photo = models.ImageField(upload_to='profiles/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.full_name:
            self.full_name = self.user.get_full_name() or self.user.username
        if not self.email:
            self.email = self.user.email or ''
        super().save(*args, **kwargs)

    def __str__(self):
        return self.full_name or self.user.username

    @property
    def avatar_url(self):
        return _safe_file_url(self.photo) or _safe_file_url(getattr(getattr(self, 'consumer_record', None), 'photo', None))

    @property
    def avatar_initial(self):
        return _name_initial(self.full_name or self.user.username)


class Consumer(models.Model):
    class Statuses(models.TextChoices):
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'

    class AccountStatuses(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PENDING = 'pending', 'Pending'
        PARTIALLY_PAID = 'partially_paid', 'Partially Paid'
        DELINQUENT = 'delinquent', 'Delinquent'
        FOR_DISCONNECTION = 'for_disconnection', 'For Disconnection'
        DISCONNECTED = 'disconnected', 'Disconnected'

    profile = models.OneToOneField(
        ConsumerProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='consumer_record',
    )
    full_name = models.CharField(max_length=255)
    meter_number = models.CharField(max_length=32, unique=True, blank=True)
    address = models.TextField(blank=True)
    household_code = models.CharField(max_length=120, blank=True)
    village = models.CharField(max_length=120, blank=True)
    barangay = models.CharField(max_length=120, blank=True)
    contact_number = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.ACTIVE)
    account_status = models.CharField(
        max_length=30,
        choices=AccountStatuses.choices,
        default=AccountStatuses.ACTIVE,
    )
    warning_issued_at = models.DateTimeField(null=True, blank=True)
    disconnection_scheduled_for = models.DateField(null=True, blank=True)
    last_payment_activity_at = models.DateTimeField(null=True, blank=True)
    photo = models.ImageField(upload_to='consumers/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        if not self.meter_number:
            self.meter_number = self._generate_meter_number()
            if update_fields is not None:
                tracked_fields = set(update_fields)
                tracked_fields.add('meter_number')
                kwargs['update_fields'] = list(tracked_fields)
        super().save(*args, **kwargs)

    @property
    def portal_user(self):
        if self.profile:
            return self.profile.user
        return None

    @property
    def avatar_url(self):
        return _safe_file_url(self.photo) or _safe_file_url(getattr(self.profile, 'photo', None))

    @property
    def avatar_initial(self):
        return _name_initial(self.full_name)

    @property
    def rate_scope_label(self):
        if self.household_code:
            return f'Household: {self.household_code}'
        if self.village:
            return f'Village: {self.village}'
        if self.barangay:
            return f'Barangay: {self.barangay}'
        return 'Default rate'

    def _generate_meter_number(self):
        prefix = _meter_name_prefix(self.full_name)
        allowed = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
        while True:
            candidate = f'{prefix}-MTR-{get_random_string(5, allowed)}'
            if not Consumer.objects.exclude(pk=self.pk).filter(meter_number=candidate).exists():
                return candidate

    def __str__(self):
        return f'{self.id:03d} - {self.full_name}'


class SystemSettings(models.Model):
    rate_per_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=20)
    billing_due_days = models.PositiveIntegerField(default=15)
    enable_cash_payments = models.BooleanField(default=True)
    enable_online_payments = models.BooleanField(default=True)
    notify_by_email = models.BooleanField(default=True)
    notify_by_sms = models.BooleanField(default=False)
    payment_gateway_notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'System Settings'
        verbose_name_plural = 'System Settings'

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        defaults = {
            'rate_per_m3': Decimal('20.00'),
            'billing_due_days': 15,
            'enable_cash_payments': True,
            'enable_online_payments': True,
            'notify_by_email': True,
            'notify_by_sms': False,
        }
        settings_obj, _ = cls.objects.get_or_create(pk=1, defaults=defaults)
        return settings_obj

    def __str__(self):
        return 'System Settings'


class AreaRate(models.Model):
    class Categories(models.TextChoices):
        HOUSEHOLD = 'household', 'Household'
        VILLAGE = 'village', 'Village'
        BARANGAY = 'barangay', 'Barangay'

    category = models.CharField(max_length=20, choices=Categories.choices)
    location_name = models.CharField(max_length=120)
    rate_per_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=20)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'location_name']
        constraints = [
            models.UniqueConstraint(fields=['category', 'location_name'], name='unique_area_rate_category_location'),
        ]

    def save(self, *args, **kwargs):
        self.location_name = re.sub(r'\s+', ' ', str(self.location_name or '').strip())
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.get_category_display()} - {self.location_name}'


class BillingRecord(models.Model):
    class Statuses(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PARTIALLY_PAID = 'partially_paid', 'Partially Paid'
        PAID = 'paid', 'Paid'
        OVERDUE = 'overdue', 'Overdue'

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='billings')
    billing_month = models.DateField(help_text='Use the first day of the billing month.')
    previous_reading = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    current_reading = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    usage_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rate_per_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=20)
    water_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    service_charge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    penalty_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    previous_arrears = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    running_balance_snapshot = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.PENDING)
    billing_date = models.DateField()
    due_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-billing_month', '-created_at']

    @property
    def amount_due(self):
        due = (self.total_amount or Decimal('0')) - (self.amount_paid or Decimal('0'))
        return due if due > 0 else Decimal('0')

    @property
    def current_bill_amount(self):
        return self.total_amount or Decimal('0')

    @property
    def statement_total_snapshot(self):
        return self.running_balance_snapshot or ((self.previous_arrears or Decimal('0')) + (self.total_amount or Decimal('0')))

    def save(self, *args, **kwargs):
        update_fields = kwargs.get('update_fields')
        tracked_update_fields = set(update_fields) if update_fields is not None else None
        should_recalculate_totals = tracked_update_fields is None or bool(
            tracked_update_fields.intersection(
                {
                    'billing_month',
                    'previous_reading',
                    'current_reading',
                    'rate_per_m3',
                    'service_charge',
                    'penalty_amount',
                    'previous_arrears',
                }
            )
        )

        if self.billing_month:
            self.billing_month = self.billing_month.replace(day=1)
            if tracked_update_fields is not None and 'billing_month' in tracked_update_fields:
                tracked_update_fields.add('billing_month')

        if should_recalculate_totals:
            self.usage_m3 = calculate_usage_m3(self.previous_reading, self.current_reading)
            self.water_charge = self.usage_m3 * (self.rate_per_m3 or Decimal('0'))
            self.total_amount = self.water_charge + (self.service_charge or Decimal('0')) + (self.penalty_amount or Decimal('0'))
            self.running_balance_snapshot = (self.previous_arrears or Decimal('0')) + (self.total_amount or Decimal('0'))
            if tracked_update_fields is not None:
                tracked_update_fields.update({'usage_m3', 'water_charge', 'total_amount', 'running_balance_snapshot'})

        if self.total_amount > 0 and self.amount_paid >= self.total_amount:
            self.status = self.Statuses.PAID
        elif self.amount_paid > 0:
            self.status = self.Statuses.PARTIALLY_PAID
        else:
            self.status = self.Statuses.PENDING
        if tracked_update_fields is not None:
            tracked_update_fields.add('status')
            kwargs['update_fields'] = list(tracked_update_fields)

        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.consumer.full_name} - {self.billing_month:%Y-%m}'


class Payment(models.Model):
    class Statuses(models.TextChoices):
        PENDING = 'pending', 'Pending'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    class Methods(models.TextChoices):
        CASH = 'cash', 'Cash'
        ONLINE = 'online', 'Online Payment'
        GCASH = 'gcash', 'GCash'
        PAYMAYA = 'paymaya', 'PayMaya'
        BANK = 'bank', 'Bank'

    class PaymentOptions(models.TextChoices):
        FULL = 'full', 'Full Payment'
        PARTIAL = 'partial', 'Partial Payment'

    class SettlementScopes(models.TextChoices):
        BULK = 'bulk', 'Bulk Settlement'
        SELECTIVE = 'selective', 'Selective Settlement'

    ONLINE_CHANNEL_VALUES = (
        Methods.GCASH,
        Methods.PAYMAYA,
        Methods.BANK,
    )

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='payments')
    billing = models.ForeignKey(BillingRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    payment_method = models.CharField(max_length=20, choices=Methods.choices, default=Methods.CASH)
    payment_option = models.CharField(max_length=20, choices=PaymentOptions.choices, default=PaymentOptions.FULL)
    settlement_scope = models.CharField(
        max_length=20,
        choices=SettlementScopes.choices,
        default=SettlementScopes.BULK,
    )
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_date = models.DateField()
    covered_month = models.DateField(null=True, blank=True, help_text='Use the first day of the covered month.')
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.COMPLETED)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_water_payments',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    arrangement_note = models.TextField(blank=True)
    reference_number = models.CharField(max_length=100, blank=True)
    proof_of_payment = models.FileField(upload_to='payment_proofs/', blank=True, null=True)
    gateway = models.CharField(max_length=30, blank=True)
    gateway_reference = models.CharField(max_length=120, blank=True)
    gateway_payment_id = models.CharField(max_length=120, blank=True)
    gateway_status = models.CharField(max_length=50, blank=True)
    gateway_redirect_url = models.URLField(blank=True)
    gateway_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-payment_date', '-created_at']

    @property
    def amount_credited(self):
        return (self.amount_paid or Decimal('0')) + (self.discount_amount or Decimal('0'))

    @property
    def allocated_amount(self):
        if not self.pk:
            return Decimal('0')
        return self.allocations.aggregate(total=Sum('amount_applied'))['total'] or Decimal('0')

    @property
    def unapplied_amount(self):
        remaining = self.amount_credited - self.allocated_amount
        return remaining if remaining > 0 else Decimal('0')

    @classmethod
    def normalize_online_channel(cls, value):
        raw_value = str(value or '').strip().lower()
        if not raw_value:
            return ''
        if raw_value.startswith('paymongo_'):
            raw_value = raw_value.split('_', 1)[1]
        if raw_value == 'maya':
            raw_value = cls.Methods.PAYMAYA
        return raw_value if raw_value in cls.ONLINE_CHANNEL_VALUES else ''

    @property
    def online_channel(self):
        direct_channel = self.normalize_online_channel(self.gateway)
        if direct_channel:
            return direct_channel

        response = self.gateway_response or {}
        candidates = [
            (((response.get('payment_method') or {}).get('attributes') or {}).get('type')),
            ((response.get('details') or {}).get('payment_method_type')),
        ]

        for payload_key in ('attached_intent', 'intent', 'payment_intent'):
            attributes = (response.get(payload_key) or {}).get('attributes') or {}
            allowed_methods = attributes.get('payment_method_allowed') or []
            if allowed_methods:
                candidates.append(allowed_methods[0])

        for candidate in candidates:
            normalized = self.normalize_online_channel(candidate)
            if normalized:
                return normalized

        return self.normalize_online_channel(self.payment_method)

    @property
    def display_payment_method(self):
        channel = self.online_channel
        if channel == self.Methods.GCASH:
            return self.Methods.GCASH.label
        if channel == self.Methods.PAYMAYA:
            return 'Maya'
        if channel == self.Methods.BANK:
            return self.Methods.BANK.label
        return self.get_payment_method_display()

    @property
    def display_reference_number(self):
        if self.payment_method == self.Methods.CASH:
            return '-'
        return self.reference_number or self.gateway_reference or self.gateway_payment_id or '-'

    @property
    def display_covered_month(self):
        if self.covered_month:
            return self.covered_month
        if self.billing_id and self.billing and self.billing.billing_month:
            return self.billing.billing_month.replace(day=1)
        if self.payment_date:
            return self.payment_date.replace(day=1)
        return None

    def save(self, *args, **kwargs):
        rebalance_consumer = kwargs.pop('rebalance_consumer', True)
        if self.billing_id and self.covered_month is None and self.billing and self.billing.billing_month:
            self.covered_month = self.billing.billing_month.replace(day=1)
        if self.covered_month:
            self.covered_month = self.covered_month.replace(day=1)
        super().save(*args, **kwargs)
        if rebalance_consumer and self.consumer_id:
            from .services import rebuild_consumer_payment_allocations

            rebuild_consumer_payment_allocations(self.consumer)

    def __str__(self):
        return f'{self.consumer.full_name} - {self.amount_paid}'


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='allocations')
    billing = models.ForeignKey(BillingRecord, on_delete=models.CASCADE, related_name='payment_allocations')
    amount_applied = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['billing__billing_month', 'created_at', 'id']
        constraints = [
            models.UniqueConstraint(fields=['payment', 'billing'], name='unique_payment_billing_allocation'),
        ]

    def __str__(self):
        return f'{self.payment_id} -> {self.billing_id}: {self.amount_applied}'


class PaymentArrangement(models.Model):
    class ArrangementTypes(models.TextChoices):
        SELECTIVE = 'selective', 'Selective Monthly Settlement'
        PAYMENT_PLAN = 'payment_plan', 'Payment Arrangement'

    class Statuses(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        COMPLETED = 'completed', 'Completed'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='payment_arrangements')
    payment = models.OneToOneField(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='arrangement',
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='requested_payment_arrangements',
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_payment_arrangements',
    )
    arrangement_type = models.CharField(
        max_length=20,
        choices=ArrangementTypes.choices,
        default=ArrangementTypes.SELECTIVE,
    )
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.PENDING)
    selected_billings = models.JSONField(default=list, blank=True)
    requested_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    outstanding_balance_snapshot = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    remaining_balance_snapshot = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Arrangement #{self.id} - {self.consumer.full_name}'


class DisconnectionRecord(models.Model):
    class Statuses(models.TextChoices):
        MONITORING = 'monitoring', 'Monitoring'
        FOR_DISCONNECTION = 'for_disconnection', 'For Disconnection'
        DISCONNECTED = 'disconnected', 'Disconnected'
        CANCELLED = 'cancelled', 'Cancelled'

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='disconnection_records')
    arrangement = models.ForeignKey(
        PaymentArrangement,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='disconnection_records',
    )
    status = models.CharField(max_length=30, choices=Statuses.choices, default=Statuses.MONITORING)
    outstanding_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unpaid_months_count = models.PositiveIntegerField(default=0)
    warning_sent_at = models.DateTimeField(null=True, blank=True)
    scheduled_disconnection_date = models.DateField(null=True, blank=True)
    last_payment_date = models.DateField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='confirmed_disconnection_records',
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Disconnection #{self.id} - {self.consumer.full_name}'


class MeterReading(models.Model):
    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='meter_readings')
    reading_date = models.DateField(default=timezone.localdate)
    billing_month = models.DateField(editable=False)
    previous_reading = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    current_reading = models.DecimalField(max_digits=10, decimal_places=2)
    usage_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='submitted_meter_readings',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-reading_date', '-created_at']
        constraints = [
            models.UniqueConstraint(fields=['consumer', 'billing_month'], name='unique_monthly_meter_reading'),
        ]

    def save(self, *args, **kwargs):
        self.billing_month = self.reading_date.replace(day=1)
        self.usage_m3 = calculate_usage_m3(self.previous_reading, self.current_reading)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.consumer.full_name} - {self.billing_month:%Y-%m}'


class Notification(models.Model):
    class Channels(models.TextChoices):
        IN_APP = 'in_app', 'In App'
        EMAIL = 'email', 'Email'
        SMS = 'sms', 'SMS'

    class Types(models.TextChoices):
        GENERAL = 'general', 'General'
        BILL_DUE = 'bill_due', 'Bill Due'
        PAYMENT = 'payment', 'Payment'
        READING = 'reading', 'Meter Reading'
        ADMIN = 'admin', 'Admin'

    class Statuses(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='billing_notifications',
    )
    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, null=True, blank=True, related_name='notifications')
    billing = models.ForeignKey(BillingRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications')
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name='notifications')
    meter_reading = models.ForeignKey(
        MeterReading,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='notifications',
    )
    channel = models.CharField(max_length=20, choices=Channels.choices, default=Channels.IN_APP)
    notification_type = models.CharField(max_length=20, choices=Types.choices, default=Types.GENERAL)
    title = models.CharField(max_length=255)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.PENDING)
    is_read = models.BooleanField(default=False)
    response_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.title} ({self.get_channel_display()})'


class SMSBlast(models.Model):
    class Audiences(models.TextChoices):
        ALL_ACTIVE = 'all_active', 'All Active Consumers'
        OVERDUE = 'overdue', 'Consumers With Overdue Bills'
        PENDING = 'pending', 'Consumers With Pending Bills'

    class Statuses(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SENT = 'sent', 'Sent'
        FAILED = 'failed', 'Failed'

    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sms_blasts',
    )
    audience = models.CharField(max_length=30, choices=Audiences.choices, default=Audiences.ALL_ACTIVE)
    message = models.TextField()
    provider = models.CharField(max_length=50, default='twilio')
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.PENDING)
    total_recipients = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    recipients_snapshot = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def mark_complete(self, sent_count, failed_count, recipients_snapshot):
        self.sent_count = sent_count
        self.failed_count = failed_count
        self.total_recipients = sent_count + failed_count
        self.recipients_snapshot = recipients_snapshot
        self.sent_at = timezone.now()
        self.status = self.Statuses.SENT if sent_count else self.Statuses.FAILED
        self.save(update_fields=['sent_count', 'failed_count', 'total_recipients', 'recipients_snapshot', 'sent_at', 'status'])

    def __str__(self):
        return f'SMS Blast - {self.get_audience_display()}'


class AuditLog(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='billing_audit_logs',
    )
    role = models.CharField(max_length=30, blank=True)
    action = models.CharField(max_length=120)
    target = models.CharField(max_length=255, blank=True)
    details = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        actor = self.user.username if self.user_id else 'System'
        return f'{actor} - {self.action}'


class MeetingMinutes(models.Model):
    class Statuses(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        APPROVED = 'approved', 'Approved'

    secretary = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='secretary_meeting_minutes',
    )
    title = models.CharField(max_length=255)
    meeting_date = models.DateField(default=timezone.localdate)
    meeting_time = models.TimeField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True)
    attendees = models.TextField(blank=True)
    agenda = models.TextField(blank=True)
    discussion_points = models.TextField(blank=True)
    resolutions = models.TextField(blank=True)
    action_items = models.TextField(blank=True)
    additional_notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.DRAFT)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-meeting_date', '-updated_at']

    def __str__(self):
        return f'{self.title} ({self.meeting_date:%Y-%m-%d})'

    @property
    def is_editable(self):
        return self.status != self.Statuses.APPROVED

    def build_snapshot(self):
        return {
            'title': self.title,
            'meeting_date': self.meeting_date.isoformat() if self.meeting_date else '',
            'meeting_time': self.meeting_time.isoformat() if self.meeting_time else '',
            'location': self.location,
            'attendees': self.attendees,
            'agenda': self.agenda,
            'discussion_points': self.discussion_points,
            'resolutions': self.resolutions,
            'action_items': self.action_items,
            'additional_notes': self.additional_notes,
            'status': self.status,
            'approved_at': self.approved_at.isoformat() if self.approved_at else '',
        }

    def record_revision(self, edited_by=None, change_summary='', changed_fields=None):
        return MeetingMinutesRevision.objects.create(
            meeting_minutes=self,
            edited_by=edited_by,
            revision_number=self.revisions.count() + 1,
            change_summary=str(change_summary or ''),
            changed_fields=list(changed_fields or []),
            snapshot=self.build_snapshot(),
        )


class MeetingMinutesRevision(models.Model):
    meeting_minutes = models.ForeignKey(
        MeetingMinutes,
        on_delete=models.CASCADE,
        related_name='revisions',
    )
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='meeting_minutes_revisions',
    )
    revision_number = models.PositiveIntegerField()
    change_summary = models.CharField(max_length=255, blank=True)
    changed_fields = models.JSONField(default=list, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-revision_number', '-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['meeting_minutes', 'revision_number'],
                name='unique_meeting_minutes_revision_number',
            ),
        ]

    def __str__(self):
        return f'{self.meeting_minutes.title} - Revision {self.revision_number}'
