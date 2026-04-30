from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Sum
from django.utils import timezone


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


class Consumer(models.Model):
    class Statuses(models.TextChoices):
        ACTIVE = 'active', 'Active'
        INACTIVE = 'inactive', 'Inactive'

    profile = models.OneToOneField(
        ConsumerProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='consumer_record',
    )
    full_name = models.CharField(max_length=255)
    address = models.TextField(blank=True)
    contact_number = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.ACTIVE)
    photo = models.ImageField(upload_to='consumers/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def portal_user(self):
        if self.profile:
            return self.profile.user
        return None

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


class BillingRecord(models.Model):
    class Statuses(models.TextChoices):
        PENDING = 'pending', 'Pending'
        PAID = 'paid', 'Paid'
        OVERDUE = 'overdue', 'Overdue'

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='billings')
    billing_month = models.DateField(help_text='Use the first day of the billing month.')
    previous_reading = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    current_reading = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    usage_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rate_per_m3 = models.DecimalField(max_digits=10, decimal_places=2, default=20)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
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

    def save(self, *args, **kwargs):
        if self.billing_month:
            self.billing_month = self.billing_month.replace(day=1)
        self.usage_m3 = (self.current_reading or Decimal('0')) - (self.previous_reading or Decimal('0'))
        if self.usage_m3 < 0:
            self.usage_m3 = Decimal('0')

        self.total_amount = self.usage_m3 * (self.rate_per_m3 or Decimal('0'))
        today = timezone.localdate()
        if self.total_amount > 0 and self.amount_paid >= self.total_amount:
            self.status = self.Statuses.PAID
        elif self.due_date and self.due_date < today:
            self.status = self.Statuses.OVERDUE
        else:
            self.status = self.Statuses.PENDING

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

    consumer = models.ForeignKey(Consumer, on_delete=models.CASCADE, related_name='payments')
    billing = models.ForeignKey(BillingRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name='payments')
    payment_method = models.CharField(max_length=20, choices=Methods.choices, default=Methods.CASH)
    amount_paid = models.DecimalField(max_digits=10, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_date = models.DateField()
    covered_month = models.DateField(null=True, blank=True, help_text='Use the first day of the covered month.')
    status = models.CharField(max_length=20, choices=Statuses.choices, default=Statuses.COMPLETED)
    reference_number = models.CharField(max_length=100, blank=True)
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
    def display_covered_month(self):
        if self.covered_month:
            return self.covered_month
        if self.billing_id and self.billing and self.billing.billing_month:
            return self.billing.billing_month.replace(day=1)
        if self.payment_date:
            return self.payment_date.replace(day=1)
        return None

    def save(self, *args, **kwargs):
        if self.billing_id and self.covered_month is None and self.billing and self.billing.billing_month:
            self.covered_month = self.billing.billing_month.replace(day=1)
        if self.covered_month:
            self.covered_month = self.covered_month.replace(day=1)
        super().save(*args, **kwargs)
        if self.billing:
            paid_total = sum(
                payment.amount_credited
                for payment in self.billing.payments.filter(status=self.Statuses.COMPLETED).only(
                    'amount_paid',
                    'discount_amount',
                )
            )
            self.billing.amount_paid = paid_total
            self.billing.save(update_fields=['amount_paid', 'usage_m3', 'total_amount', 'status'])

    def __str__(self):
        return f'{self.consumer.full_name} - {self.amount_paid}'


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
        self.usage_m3 = (self.current_reading or Decimal('0')) - (self.previous_reading or Decimal('0'))
        if self.usage_m3 < 0:
            self.usage_m3 = Decimal('0')
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
