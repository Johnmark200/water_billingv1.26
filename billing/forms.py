from datetime import date
from decimal import Decimal
import re

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm, PasswordResetForm, UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    AreaRate,
    BillingRecord,
    Consumer,
    ConsumerProfile,
    MeetingMinutes,
    MeterReading,
    MINIMUM_BILLABLE_USAGE_M3,
    Payment,
    PaymentArrangement,
    SMSBlast,
    SystemSettings,
)
from .services import (
    build_unpaid_billing_choices,
    build_consumer_payment_month_choices,
    create_payment_arrangement,
    get_consumer_outstanding_balance,
    get_existing_billing_for_month,
    get_matching_approved_arrangement,
    get_preferred_billing_records,
    get_previous_arrears_balance,
    get_previous_reading_for_month,
    get_selected_billings_for_consumer,
    get_unpaid_billing_records,
    is_e164_phone_number,
    is_valid_email_address,
    normalize_phone_number,
    rebuild_consumer_payment_allocations,
)


SPECIAL_CHARACTER_PATTERN = re.compile(r'[!@#$%^&*(),.?":{}|<>_\-+=/\\[\]~`]')


def collect_password_strength_issues(password, username='', email=''):
    issues = []

    if len(password) < 8:
        issues.append('Use at least 8 characters.')
    if not re.search(r'[A-Z]', password):
        issues.append('Add at least one uppercase letter.')
    if not re.search(r'[a-z]', password):
        issues.append('Add at least one lowercase letter.')
    if not re.search(r'\d', password):
        issues.append('Add at least one number.')
    if not SPECIAL_CHARACTER_PATTERN.search(password):
        issues.append('Add at least one special character.')

    temp_user = User(username=username or '', email=email or '')
    try:
        password_validation.validate_password(password, user=temp_user)
    except ValidationError as exc:
        for message in exc.messages:
            if message not in issues:
                issues.append(message)

    return issues


class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control'}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}))


class AccountPasswordResetForm(PasswordResetForm):
    email = forms.EmailField(
        label='Email Address',
        widget=forms.EmailInput(attrs={'autocomplete': 'email'}),
    )


class PasswordChangeOTPRequestForm(forms.Form):
    OTP_CHANNEL_CHOICES = (
        ('email', 'Email OTP'),
        ('sms', 'SMS OTP'),
    )

    current_password = forms.CharField(widget=forms.PasswordInput)
    new_password1 = forms.CharField(widget=forms.PasswordInput)
    new_password2 = forms.CharField(label='Confirm Password', widget=forms.PasswordInput)
    otp_channel = forms.ChoiceField(choices=OTP_CHANNEL_CHOICES, widget=forms.RadioSelect)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.profile = ConsumerProfile.objects.filter(user=user).first()

    def clean_current_password(self):
        current_password = self.cleaned_data.get('current_password', '')
        if not self.user.check_password(current_password):
            raise forms.ValidationError('Current password is incorrect.')
        return current_password

    def clean_otp_channel(self):
        channel = self.cleaned_data.get('otp_channel', '')
        if channel == 'email':
            email = (self.user.email or (self.profile.email if self.profile else '')).strip()
            if not email or not is_valid_email_address(email):
                raise forms.ValidationError('No valid email address is available for this account.')
        elif channel == 'sms':
            phone = normalize_phone_number(self.profile.contact if self.profile else '')
            if not phone or not is_e164_phone_number(phone):
                raise forms.ValidationError('No valid SMS contact number is available for this account.')
        return channel

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('new_password1', '')
        password2 = cleaned_data.get('new_password2', '')
        email = self.user.email or (self.profile.email if self.profile else '')

        if password1 and password2 and password1 != password2:
            self.add_error('new_password2', 'Passwords do not match. Please try again.')

        if password1 and self.user.check_password(password1):
            self.add_error('new_password1', 'Your new password must be different from your current password.')

        if password1:
            issues = collect_password_strength_issues(password1, username=self.user.username, email=email)
            if issues:
                self.add_error('new_password1', 'Your password is too weak. Please use a stronger password.')
                for issue in issues:
                    self.add_error('new_password1', issue)

        if password1:
            try:
                password_validation.validate_password(password1, user=self.user)
            except ValidationError as exc:
                for message in exc.messages:
                    self.add_error('new_password1', message)

        return cleaned_data


class PasswordChangeOTPVerifyForm(forms.Form):
    otp_code = forms.CharField(max_length=6, min_length=6)

    def clean_otp_code(self):
        otp_code = ''.join(character for character in self.cleaned_data.get('otp_code', '') if character.isdigit())
        if len(otp_code) != 6:
            raise forms.ValidationError('Enter the 6-digit OTP code.')
        return otp_code


class SignUpForm(UserCreationForm):
    full_name = forms.CharField(max_length=255)
    email = forms.EmailField(required=False)
    contact = forms.CharField(max_length=50, required=False)
    address = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)

    class Meta:
        model = User
        fields = ('username', 'full_name', 'email', 'contact', 'address', 'password1', 'password2')

    def _post_clean(self):
        forms.ModelForm._post_clean(self)

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if email and not is_valid_email_address(email):
            raise forms.ValidationError('Enter a valid email address.')
        return email

    def clean_contact(self):
        contact = normalize_phone_number(self.cleaned_data.get('contact', ''))
        if contact and not is_e164_phone_number(contact):
            raise forms.ValidationError('Enter the contact number in E.164 format, for example +639171234567.')
        return contact

    def clean(self):
        cleaned_data = forms.ModelForm.clean(self)
        password1 = cleaned_data.get('password1', '')
        password2 = cleaned_data.get('password2', '')
        username = cleaned_data.get('username', '')
        email = cleaned_data.get('email', '')

        if password1 and username and username.lower() in password1.lower():
            self.add_error('password1', 'Your username cannot be used as your password.')

        if password1:
            issues = collect_password_strength_issues(password1, username=username, email=email)
            if issues:
                self.add_error('password1', 'Your password is too weak. Please use a stronger password.')
                for issue in issues:
                    self.add_error('password1', issue)

        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'Passwords do not match. Please try again.')

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data.get('email', '')
        if commit:
            user.save()
            profile = ConsumerProfile.objects.create(
                user=user,
                full_name=self.cleaned_data['full_name'],
                email=self.cleaned_data.get('email', ''),
                contact=self.cleaned_data.get('contact', ''),
                address=self.cleaned_data.get('address', ''),
                role=ConsumerProfile.Roles.CONSUMER,
            )
            Consumer.objects.create(
                profile=profile,
                full_name=profile.full_name,
                address=profile.address,
                contact_number=profile.contact,
                status=Consumer.Statuses.ACTIVE,
            )
        return user


class PortalAccountForm(UserCreationForm):
    role = forms.ChoiceField(
        choices=[
            (ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.ADMIN.label),
            (ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.SECRETARY.label),
            (ConsumerProfile.Roles.TREASURER, ConsumerProfile.Roles.TREASURER.label),
            (ConsumerProfile.Roles.READER, ConsumerProfile.Roles.READER.label),
        ]
    )
    full_name = forms.CharField(max_length=255)
    email = forms.EmailField(required=False)
    contact = forms.CharField(max_length=50, required=False)
    address = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)
    photo = forms.ImageField(required=False)

    class Meta:
        model = User
        fields = (
            'role',
            'username',
            'full_name',
            'email',
            'contact',
            'address',
            'photo',
            'password1',
            'password2',
        )

    def _post_clean(self):
        forms.ModelForm._post_clean(self)

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if email and not is_valid_email_address(email):
            raise forms.ValidationError('Enter a valid email address.')
        return email

    def clean_contact(self):
        contact = normalize_phone_number(self.cleaned_data.get('contact', ''))
        if contact and not is_e164_phone_number(contact):
            raise forms.ValidationError('Enter the contact number in E.164 format, for example +639171234567.')
        return contact

    def clean(self):
        cleaned_data = forms.ModelForm.clean(self)
        password1 = cleaned_data.get('password1', '')
        password2 = cleaned_data.get('password2', '')
        username = cleaned_data.get('username', '')
        email = cleaned_data.get('email', '')

        if password1 and username and username.lower() in password1.lower():
            self.add_error('password1', 'Your username cannot be used as your password.')

        if password1:
            issues = collect_password_strength_issues(password1, username=username, email=email)
            if issues:
                self.add_error('password1', 'Your password is too weak. Please use a stronger password.')
                for issue in issues:
                    self.add_error('password1', issue)

        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'Passwords do not match. Please try again.')

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        role = self.cleaned_data['role']
        user.email = self.cleaned_data.get('email', '')
        user.is_staff = role == ConsumerProfile.Roles.ADMIN

        if commit:
            user.save()
            ConsumerProfile.objects.create(
                user=user,
                full_name=self.cleaned_data['full_name'],
                email=self.cleaned_data.get('email', ''),
                contact=self.cleaned_data.get('contact', ''),
                address=self.cleaned_data.get('address', ''),
                role=role,
                photo=self.cleaned_data.get('photo'),
            )
        return user


class ConsumerForm(forms.ModelForm):
    linked_account_role = forms.ChoiceField(
        choices=ConsumerProfile.Roles.choices,
        required=False,
        initial=ConsumerProfile.Roles.CONSUMER,
        help_text='Only applied when a linked account profile is selected.',
    )

    class Meta:
        model = Consumer
        fields = [
            'profile',
            'full_name',
            'meter_number',
            'barangay',
            'village',
            'household_code',
            'address',
            'contact_number',
            'status',
            'photo',
        ]
        widgets = {
            'address': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        available_profiles = ConsumerProfile.objects.exclude(consumer_record__isnull=False)
        if self.instance.pk and self.instance.profile_id:
            available_profiles = ConsumerProfile.objects.filter(pk=self.instance.profile_id) | available_profiles
        self.fields['profile'].queryset = available_profiles.order_by('full_name')
        self.fields['profile'].required = False
        self.fields['profile'].label = 'Linked account profile'
        self.fields['profile'].help_text = 'Optional. Link this consumer to an existing system account profile.'
        self.fields['meter_number'].required = False
        self.fields['meter_number'].help_text = 'Generated automatically as LASTNAME-MTR-YYYY###### and stays permanent for the consumer account.'
        self.fields['meter_number'].widget.attrs['readonly'] = 'readonly'
        self.fields['meter_number'].widget.attrs['placeholder'] = 'Generated after saving as LASTNAME-MTR-YYYY######'
        self.fields['linked_account_role'].label = 'Linked account role'
        if self.instance.pk and self.instance.profile_id:
            self.fields['linked_account_role'].initial = self.instance.profile.role
        self.order_fields(
            [
                'profile',
                'linked_account_role',
                'full_name',
                'meter_number',
                'barangay',
                'village',
                'household_code',
                'address',
                'contact_number',
                'status',
                'photo',
            ]
        )

    def clean_contact_number(self):
        contact_number = normalize_phone_number(self.cleaned_data.get('contact_number', ''))
        if contact_number and not is_e164_phone_number(contact_number):
            raise forms.ValidationError('Enter the contact number in E.164 format, for example +639171234567.')
        return contact_number

    def clean_meter_number(self):
        return getattr(self.instance, 'meter_number', '') or self.initial.get('meter_number', '')

    def save(self, commit=True):
        consumer = super().save(commit=False)
        profile = self.cleaned_data.get('profile')

        if commit:
            consumer.save()

            if profile:
                profile_updates = []
                linked_role = self.cleaned_data.get('linked_account_role') or profile.role or ConsumerProfile.Roles.CONSUMER

                if profile.role != linked_role:
                    profile.role = linked_role
                    profile_updates.append('role')
                if profile.full_name != consumer.full_name:
                    profile.full_name = consumer.full_name
                    profile_updates.append('full_name')
                if profile.address != consumer.address:
                    profile.address = consumer.address
                    profile_updates.append('address')
                if profile.contact != consumer.contact_number:
                    profile.contact = consumer.contact_number
                    profile_updates.append('contact')
                if 'photo' in self.changed_data and profile.photo != consumer.photo:
                    profile.photo = consumer.photo
                    profile_updates.append('photo')

                if profile_updates:
                    profile.save(update_fields=profile_updates)

        return consumer


class BillingRecordForm(forms.ModelForm):
    class Meta:
        model = BillingRecord
        fields = [
            'consumer',
            'billing_month',
            'previous_reading',
            'current_reading',
            'rate_per_m3',
            'service_charge',
            'penalty_amount',
            'amount_paid',
            'status',
            'billing_date',
            'due_date',
        ]
        widgets = {
            'billing_month': forms.DateInput(attrs={'type': 'date'}),
            'billing_date': forms.DateInput(attrs={'type': 'date'}),
            'due_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['consumer'].queryset = Consumer.objects.order_by('full_name')
        self.fields['current_reading'].help_text = (
            f'Enter the actual current meter reading. Minimum billable usage is {MINIMUM_BILLABLE_USAGE_M3} m³.'
        )

    def clean(self):
        cleaned_data = super().clean()
        consumer = cleaned_data.get('consumer')
        billing_month = cleaned_data.get('billing_month')
        previous_reading = cleaned_data.get('previous_reading')
        current_reading = cleaned_data.get('current_reading')

        if consumer and billing_month:
            existing_billing = get_existing_billing_for_month(consumer, billing_month)
            if existing_billing and existing_billing.pk != self.instance.pk:
                self.add_error('billing_month', 'A billing record for this consumer and month already exists.')
        if (
            previous_reading is not None
            and current_reading is not None
            and current_reading < previous_reading
        ):
            self.add_error('current_reading', 'Current reading cannot be lower than the previous reading.')

        return cleaned_data

    def save(self, commit=True):
        billing = super().save(commit=False)
        if billing.consumer_id and billing.billing_month:
            billing.previous_arrears = get_previous_arrears_balance(billing.consumer, billing.billing_month)
        if commit:
            billing.save()
        return billing


class AdminPaymentForm(forms.ModelForm):
    online_channel = forms.ChoiceField(
        choices=[
            (Payment.Methods.GCASH, Payment.Methods.GCASH.label),
            (Payment.Methods.PAYMAYA, 'Maya'),
        ],
        required=False,
    )
    selected_billings = forms.MultipleChoiceField(
        label='Selected billing months',
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    arrangement_note = forms.CharField(
        label='Arrangement note',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
    )

    class Meta:
        model = Payment
        fields = [
            'consumer',
            'billing',
            'payment_method',
            'payment_option',
            'amount_paid',
            'discount_amount',
            'payment_date',
            'status',
            'reference_number',
        ]
        widgets = {
            'payment_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        consumer_id = None
        self.unpaid_billing_choices = []
        if self.is_bound:
            consumer_id = self.data.get(self.add_prefix('consumer')) or self.data.get('consumer')
        else:
            consumer_id = self.initial.get('consumer')

        self.fields['consumer'].queryset = Consumer.objects.order_by('full_name')
        billings_queryset = BillingRecord.objects.select_related('consumer')
        if consumer_id:
            billings_queryset = billings_queryset.filter(consumer_id=consumer_id)
        billing_ids = [billing.id for billing in get_preferred_billing_records(billings_queryset)]
        self.fields['billing'].queryset = BillingRecord.objects.filter(id__in=billing_ids).select_related('consumer').order_by(
            '-billing_month',
            '-created_at',
        )
        if consumer_id:
            try:
                consumer = Consumer.objects.get(pk=consumer_id)
            except (Consumer.DoesNotExist, ValueError, TypeError):
                consumer = None
            self.unpaid_billing_choices = build_unpaid_billing_choices(consumer)
        else:
            self.unpaid_billing_choices = [
                (
                    str(billing.id),
                    f'{billing.consumer.full_name} - {billing.billing_month:%B %Y} - PHP {billing.amount_due:,.2f}',
                )
                for billing in get_preferred_billing_records(BillingRecord.objects.select_related('consumer'))
                if billing.amount_due > 0
            ]
        self.fields['selected_billings'].choices = self.unpaid_billing_choices
        self.fields['payment_method'].choices = [
            (Payment.Methods.CASH, Payment.Methods.CASH.label),
            (Payment.Methods.ONLINE, Payment.Methods.ONLINE.label),
        ]
        self.fields['payment_option'].widget = forms.RadioSelect(choices=Payment.PaymentOptions.choices)
        self.fields['payment_option'].initial = Payment.PaymentOptions.FULL
        self.fields['discount_amount'].required = False
        self.fields['discount_amount'].label = 'Discount applied'
        self.fields['reference_number'].required = False
        self.fields['reference_number'].widget = forms.HiddenInput()
        self.fields['amount_paid'].required = False
        self.fields['amount_paid'].widget.attrs.update({'readonly': 'readonly'})
        self.fields['online_channel'].widget = forms.HiddenInput()
        self.fields['online_channel'].initial = Payment.normalize_online_channel(
            self.data.get(self.add_prefix('online_channel')) if self.is_bound else self.instance.gateway
        )
        self.fields['billing'].required = False

    def clean_amount_paid(self):
        return self.cleaned_data.get('amount_paid') or Decimal('0')

    def clean(self):
        cleaned_data = super().clean()
        consumer = cleaned_data.get('consumer')
        billing = cleaned_data.get('billing')
        payment_option = cleaned_data.get('payment_option') or Payment.PaymentOptions.FULL
        payment_method = cleaned_data.get('payment_method')
        selected_ids = cleaned_data.get('selected_billings') or []
        selected_billings = get_selected_billings_for_consumer(consumer, selected_ids) if consumer else []

        if payment_option == Payment.PaymentOptions.PARTIAL and not selected_billings and billing and billing.amount_due > 0:
            selected_billings = [billing]
            cleaned_data['selected_billings'] = [str(billing.id)]

        if payment_option == Payment.PaymentOptions.FULL:
            selected_billings = get_unpaid_billing_records(consumer) if consumer else []
            cleaned_data['settlement_scope'] = Payment.SettlementScopes.BULK
            if selected_billings:
                cleaned_data['amount_paid'] = get_consumer_outstanding_balance(consumer)
                cleaned_data['billing'] = selected_billings[-1]
                cleaned_data['covered_month'] = selected_billings[-1].billing_month
            elif billing:
                cleaned_data['amount_paid'] = billing.amount_due or billing.total_amount
                cleaned_data['covered_month'] = billing.billing_month
        else:
            cleaned_data['settlement_scope'] = Payment.SettlementScopes.SELECTIVE
            if not selected_billings:
                self.add_error('selected_billings', 'Choose at least one unpaid billing month for a selective settlement.')
            else:
                cleaned_data['amount_paid'] = sum((selected.amount_due for selected in selected_billings), Decimal('0'))
                cleaned_data['billing'] = selected_billings[-1]
                cleaned_data['covered_month'] = selected_billings[-1].billing_month
        cleaned_data['selected_billing_records'] = selected_billings

        if payment_method == Payment.Methods.CASH:
            cleaned_data['reference_number'] = ''
            cleaned_data['online_channel'] = ''
        elif payment_method == Payment.Methods.ONLINE:
            online_channel = Payment.normalize_online_channel(cleaned_data.get('online_channel'))
            if not online_channel:
                self.add_error('online_channel', 'Choose the online payment channel for this transaction.')
            cleaned_data['online_channel'] = online_channel
        if (cleaned_data.get('amount_paid') or Decimal('0')) <= 0:
            self.add_error('amount_paid', 'Amount paid must be greater than zero.')
        return cleaned_data

    def save(self, commit=True):
        payment = super().save(commit=False)
        payment.gateway = self.cleaned_data.get('online_channel', '') if payment.payment_method == Payment.Methods.ONLINE else ''
        payment.billing = self.cleaned_data.get('billing')
        payment.covered_month = self.cleaned_data.get('covered_month')
        payment.settlement_scope = self.cleaned_data.get('settlement_scope', Payment.SettlementScopes.BULK)
        payment.arrangement_note = self.cleaned_data.get('arrangement_note', '')
        if commit:
            payment.save(rebalance_consumer=False)
        return payment


def build_payment_method_choices(system_settings):
    choices = []
    if system_settings.enable_cash_payments:
        choices.append((Payment.Methods.CASH, Payment.Methods.CASH.label))
    if system_settings.enable_online_payments:
        choices.append((Payment.Methods.ONLINE, Payment.Methods.ONLINE.label))
    return choices


class ConsumerPaymentForm(forms.ModelForm):
    covered_month = forms.ChoiceField(label='Payment month')
    payment_channel = forms.ChoiceField(
        label='Payment method',
        choices=[
            ('gcash', 'GCash'),
            ('bank', 'Bank'),
            ('cash', 'Cash'),
        ],
        initial='gcash',
        required=False,
        widget=forms.RadioSelect,
    )
    selected_billings = forms.MultipleChoiceField(
        label='Selected unpaid months',
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    payment_method = forms.ChoiceField(
        choices=[(Payment.Methods.ONLINE, Payment.Methods.ONLINE.label)],
        initial=Payment.Methods.ONLINE,
        required=False,
        widget=forms.HiddenInput(),
    )
    payment_option = forms.ChoiceField(
        choices=Payment.PaymentOptions.choices,
        initial=Payment.PaymentOptions.FULL,
        widget=forms.RadioSelect,
    )
    online_wallet = forms.ChoiceField(
        choices=[
            ('gcash', 'GCash'),
            ('paymaya', 'Maya'),
        ],
        required=False,
        widget=forms.HiddenInput(),
    )
    arrangement_note = forms.CharField(
        label='Arrangement note',
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
    )
    proof_of_payment = forms.FileField(
        label='Proof of payment',
        required=False,
    )

    class Meta:
        model = Payment
        fields = ['payment_method', 'payment_option', 'amount_paid', 'reference_number', 'online_wallet', 'proof_of_payment']

    def __init__(self, *args, consumer=None, system_settings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.consumer = consumer
        self.system_settings = system_settings or SystemSettings.load()
        self.available_months = []
        self.unpaid_billings = get_unpaid_billing_records(consumer) if consumer else []
        self.arrangement_request_required = False
        self.approved_arrangement = None
        self.fields['payment_method'].choices = [(Payment.Methods.ONLINE, Payment.Methods.ONLINE.label)]
        self.fields['payment_method'].initial = Payment.Methods.ONLINE
        self.fields['payment_method'].widget = forms.HiddenInput()
        self.fields['reference_number'].required = False
        self.fields['reference_number'].widget.attrs.update(
            {
                'placeholder': 'Enter bank reference number or office receipt number',
            }
        )
        self.fields['amount_paid'].required = False
        self.fields['amount_paid'].widget.attrs.update({'readonly': 'readonly'})
        self.fields['covered_month'].help_text = 'Bulk payment automatically uses your full outstanding balance.'
        self.fields['covered_month'].choices = []
        self.fields['selected_billings'].choices = build_unpaid_billing_choices(consumer)

        if consumer:
            self.available_months = build_consumer_payment_month_choices(consumer)
            self.fields['covered_month'].choices = self.available_months
            if self.available_months and not self.is_bound:
                self.initial.setdefault('covered_month', self.available_months[0][0])

    def clean_covered_month(self):
        raw_value = (self.cleaned_data.get('covered_month') or '').strip()
        try:
            year, month = [int(part) for part in raw_value.split('-', 1)]
            return date(year, month, 1)
        except (TypeError, ValueError):
            raise forms.ValidationError('Select a valid payment month.')

    def clean(self):
        cleaned_data = super().clean()
        covered_month = cleaned_data.get('covered_month')
        payment_channel = (cleaned_data.get('payment_channel') or 'gcash').strip().lower()
        payment_option = cleaned_data.get('payment_option') or Payment.PaymentOptions.FULL
        selected_ids = cleaned_data.get('selected_billings') or []
        selected_billings = get_selected_billings_for_consumer(self.consumer, selected_ids) if self.consumer else []

        if covered_month and self.consumer:
            cleaned_data['billing'] = get_existing_billing_for_month(self.consumer, covered_month)

        if payment_channel == 'gcash' and not self.system_settings.enable_online_payments:
            raise forms.ValidationError('Online payments are not enabled yet. Please contact the administrator.')
        if self.consumer and not self.available_months:
            raise forms.ValidationError('No payment months are available right now. Please contact the administrator.')
        billing = cleaned_data.get('billing')
        if payment_option == Payment.PaymentOptions.FULL:
            cleaned_data['settlement_scope'] = Payment.SettlementScopes.BULK
            if self.unpaid_billings:
                cleaned_data['selected_billing_records'] = self.unpaid_billings
                cleaned_data['billing'] = self.unpaid_billings[-1]
                cleaned_data['covered_month'] = self.unpaid_billings[-1].billing_month
                cleaned_data['amount_paid'] = get_consumer_outstanding_balance(self.consumer)
            elif billing:
                cleaned_data['selected_billing_records'] = [billing]
                cleaned_data['amount_paid'] = billing.amount_due or billing.total_amount
            else:
                self.add_error('covered_month', 'No outstanding billing month is available for payment.')
        else:
            cleaned_data['settlement_scope'] = Payment.SettlementScopes.SELECTIVE
            if not selected_billings and billing and billing.amount_due > 0:
                selected_billings = [billing]
                cleaned_data['selected_billings'] = [str(billing.id)]
            if not selected_billings:
                self.add_error('selected_billings', 'Choose at least one unpaid billing month to request selective settlement.')
            else:
                cleaned_data['selected_billing_records'] = selected_billings
                cleaned_data['billing'] = selected_billings[-1]
                cleaned_data['covered_month'] = selected_billings[-1].billing_month
                cleaned_data['amount_paid'] = sum((selected.amount_due for selected in selected_billings), Decimal('0'))
                self.approved_arrangement = get_matching_approved_arrangement(self.consumer, selected_billings)
                self.arrangement_request_required = self.approved_arrangement is None

        if payment_channel == 'gcash':
            cleaned_data['payment_method'] = Payment.Methods.ONLINE
            cleaned_data['online_wallet'] = 'gcash'
            if not self.arrangement_request_required and cleaned_data.get('online_wallet') not in {'gcash', 'paymaya'}:
                self.add_error('payment_channel', 'Choose a supported e-wallet before continuing to PayMongo.')
            cleaned_data['reference_number'] = ''
        elif payment_channel == 'bank':
            cleaned_data['payment_method'] = Payment.Methods.BANK
            cleaned_data['online_wallet'] = ''
        else:
            cleaned_data['payment_method'] = Payment.Methods.CASH
            cleaned_data['online_wallet'] = ''

        if (cleaned_data.get('amount_paid') or Decimal('0')) <= 0:
            self.add_error('amount_paid', 'Amount paid must be greater than zero.')

        return cleaned_data

    def clean_amount_paid(self):
        return self.cleaned_data.get('amount_paid') or Decimal('0')

    def save_arrangement_request(self, requested_by):
        return create_payment_arrangement(
            self.consumer,
            self.cleaned_data.get('selected_billing_records') or [],
            requested_by=requested_by,
            requested_amount=self.cleaned_data.get('amount_paid') or Decimal('0'),
            outstanding_balance=get_consumer_outstanding_balance(self.consumer),
            notes=self.cleaned_data.get('arrangement_note', ''),
            status=PaymentArrangement.Statuses.PENDING,
        )

    def save(self, commit=True):
        payment = super().save(commit=False)
        payment.consumer = self.consumer
        payment.billing = self.cleaned_data.get('billing')
        payment.covered_month = self.cleaned_data.get('covered_month')
        payment.discount_amount = Decimal('0')
        payment.payment_date = timezone.localdate()
        payment.payment_method = self.cleaned_data.get('payment_method', Payment.Methods.ONLINE)
        payment.status = Payment.Statuses.PENDING
        payment.gateway = self.cleaned_data.get('online_wallet', '') if payment.payment_method == Payment.Methods.ONLINE else ''
        payment.reference_number = '' if payment.payment_method == Payment.Methods.ONLINE else (self.cleaned_data.get('reference_number', '') or '')
        payment.proof_of_payment = self.cleaned_data.get('proof_of_payment')
        payment.settlement_scope = self.cleaned_data.get('settlement_scope', Payment.SettlementScopes.BULK)
        payment.arrangement_note = self.cleaned_data.get('arrangement_note', '')
        if commit:
            payment.save(rebalance_consumer=False)
            if self.approved_arrangement is not None:
                self.approved_arrangement.payment = payment
                self.approved_arrangement.status = PaymentArrangement.Statuses.APPROVED
                self.approved_arrangement.save(update_fields=['payment', 'status', 'updated_at'])
            rebuild_consumer_payment_allocations(self.consumer)
        return payment


class SystemSettingsForm(forms.ModelForm):
    class Meta:
        model = SystemSettings
        fields = [
            'rate_per_m3',
            'billing_due_days',
            'enable_cash_payments',
            'enable_online_payments',
            'notify_by_email',
            'notify_by_sms',
            'payment_gateway_notes',
        ]
        widgets = {
            'payment_gateway_notes': forms.Textarea(attrs={'rows': 4}),
        }


class AreaRateForm(forms.ModelForm):
    class Meta:
        model = AreaRate
        fields = ['category', 'location_name', 'rate_per_m3', 'is_active']

    def clean_location_name(self):
        return re.sub(r'\s+', ' ', self.cleaned_data.get('location_name', '').strip())


class MeterReadingForm(forms.ModelForm):
    meter_number = forms.CharField(max_length=32)
    consumer_id = forms.IntegerField(required=False, widget=forms.HiddenInput())
    consumer_account = forms.CharField(required=False)
    registered_connection = forms.CharField(required=False)

    class Meta:
        model = MeterReading
        fields = ['meter_number', 'consumer_id', 'consumer_account', 'registered_connection', 'reading_date', 'current_reading', 'notes']
        widgets = {
            'reading_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['meter_number'].widget.attrs.update(
            {
                'placeholder': 'Enter assigned meter number',
                'autocomplete': 'off',
                'list': 'reader-meter-number-list',
            }
        )
        self.fields['current_reading'].help_text = (
            f'Enter the actual current meter reading. Minimum billable usage is {MINIMUM_BILLABLE_USAGE_M3} m³.'
        )

        self.fields['consumer_account'].widget.attrs.update(
            {
                'placeholder': 'Auto-filled from the meter number lookup',
                'readonly': 'readonly',
            }
        )
        self.fields['registered_connection'].widget.attrs.update(
            {
                'placeholder': 'Auto-filled linked consumer account',
                'readonly': 'readonly',
            }
        )

    def clean_meter_number(self):
        return self.cleaned_data.get('meter_number', '').strip().upper()

    def clean_current_reading(self):
        current_reading = self.cleaned_data['current_reading']
        if current_reading < 0:
            raise forms.ValidationError('Reading must be zero or greater.')
        return current_reading

    def clean(self):
        cleaned_data = super().clean()
        meter_number = cleaned_data.get('meter_number')
        provided_consumer_id = cleaned_data.get('consumer_id')
        provided_account = (cleaned_data.get('consumer_account') or '').strip().upper()
        consumer = getattr(self.instance, 'consumer', None)
        if meter_number:
            consumer = Consumer.objects.filter(
                status=Consumer.Statuses.ACTIVE,
                meter_number__iexact=meter_number,
            ).first()
            if consumer is None:
                self.add_error('meter_number', 'No active consumer account is linked to that meter number.')
            else:
                cleaned_data['consumer'] = consumer
                expected_account = f'ACC-{consumer.id:05d}'
                if provided_consumer_id and consumer.id != provided_consumer_id:
                    self.add_error('consumer_account', 'The selected consumer account does not match the entered meter number.')
                if provided_account and provided_account != expected_account:
                    self.add_error('consumer_account', 'The linked consumer account reference does not match the entered meter number.')
        reading_date = cleaned_data.get('reading_date') or getattr(self.instance, 'reading_date', None)
        current_reading = cleaned_data.get('current_reading')

        if consumer and reading_date and current_reading is not None:
            target_month = reading_date.replace(day=1)
            previous_reading = get_previous_reading_for_month(consumer, target_month)
            cleaned_data['previous_reading_snapshot'] = previous_reading
            if current_reading < previous_reading:
                self.add_error('current_reading', 'Current reading cannot be lower than the previous reading.')
            existing_reading = MeterReading.objects.filter(consumer=consumer, billing_month=target_month).exclude(pk=self.instance.pk).first()
            if existing_reading is not None:
                self.add_error(
                    None,
                    'A meter reading for this billing cycle already exists. Use the edit action to correct the submitted reading.',
                )

        return cleaned_data


class MeterReadingUpdateForm(MeterReadingForm):
    class Meta(MeterReadingForm.Meta):
        fields = ['current_reading', 'notes']


class ProfileUpdateForm(forms.ModelForm):
    class Meta:
        model = ConsumerProfile
        fields = ['full_name', 'email', 'contact', 'address', 'photo']
        widgets = {
            'address': forms.Textarea(attrs={'rows': 3}),
        }

    def clean_email(self):
        email = self.cleaned_data.get('email', '').strip()
        if email and not is_valid_email_address(email):
            raise forms.ValidationError('Enter a valid email address.')
        return email

    def clean_contact(self):
        contact = normalize_phone_number(self.cleaned_data.get('contact', ''))
        if contact and not is_e164_phone_number(contact):
            raise forms.ValidationError('Enter the contact number in E.164 format, for example +639171234567.')
        return contact

    def save(self, commit=True):
        profile = super().save(commit=False)
        user = profile.user
        user.email = self.cleaned_data.get('email', '')

        if commit:
            user.save(update_fields=['email'])
            profile.save()

            consumer = getattr(profile, 'consumer_record', None)
            if consumer:
                consumer.full_name = profile.full_name
                consumer.address = profile.address
                consumer.contact_number = profile.contact
                if 'photo' in self.changed_data:
                    consumer.photo = profile.photo

                update_fields = ['full_name', 'address', 'contact_number']
                if 'photo' in self.changed_data:
                    update_fields.append('photo')
                consumer.save(update_fields=update_fields)

        return profile


class MeetingMinutesForm(forms.ModelForm):
    change_summary = forms.CharField(
        max_length=255,
        required=False,
        help_text='Optional. Briefly describe what changed in this revision.',
    )

    class Meta:
        model = MeetingMinutes
        fields = [
            'title',
            'meeting_date',
            'meeting_time',
            'location',
            'attendees',
            'agenda',
            'discussion_points',
            'resolutions',
            'action_items',
            'additional_notes',
        ]
        widgets = {
            'meeting_date': forms.DateInput(attrs={'type': 'date'}),
            'meeting_time': forms.TimeInput(attrs={'type': 'time'}),
            'attendees': forms.Textarea(attrs={'rows': 4}),
            'agenda': forms.Textarea(attrs={'rows': 5}),
            'discussion_points': forms.Textarea(attrs={'rows': 8}),
            'resolutions': forms.Textarea(attrs={'rows': 5}),
            'action_items': forms.Textarea(attrs={'rows': 5}),
            'additional_notes': forms.Textarea(attrs={'rows': 4}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name, field in self.fields.items():
            css_class = 'meeting-minutes-input'
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault('class', css_class)
            elif isinstance(field.widget, (forms.DateInput, forms.TimeInput)):
                field.widget.attrs.setdefault('class', css_class)
            else:
                field.widget.attrs.setdefault('class', css_class)

        self.fields['attendees'].help_text = 'List attendees, one per line, or group them by role.'
        self.fields['agenda'].help_text = 'Use numbered agenda items to keep the document format consistent.'
        self.fields['discussion_points'].label = 'Minutes and Discussion'
        self.fields['action_items'].help_text = 'Record assignments, deadlines, and follow-up work.'


class SMSBlastForm(forms.ModelForm):
    class Meta:
        model = SMSBlast
        fields = ['audience', 'message']
        widgets = {
            'message': forms.Textarea(attrs={'rows': 4}),
        }


class EmailBlastForm(forms.Form):
    audience = forms.ChoiceField(choices=SMSBlast.Audiences.choices)
    subject = forms.CharField(max_length=255)
    message = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}))


class TestSMSForm(forms.Form):
    phone_number = forms.CharField(max_length=20, help_text='Use E.164 format, for example +639171234567.')
    message = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}))

    def clean_phone_number(self):
        phone_number = normalize_phone_number(self.cleaned_data['phone_number'])
        if not is_e164_phone_number(phone_number):
            raise forms.ValidationError('Enter the phone number in E.164 format, for example +639171234567.')
        return phone_number


class TestEmailForm(forms.Form):
    email = forms.EmailField()
    subject = forms.CharField(max_length=255)
    message = forms.CharField(widget=forms.Textarea(attrs={'rows': 4}))

    def clean_email(self):
        email = self.cleaned_data['email'].strip()
        if not is_valid_email_address(email):
            raise forms.ValidationError('Enter a valid email address.')
        return email
