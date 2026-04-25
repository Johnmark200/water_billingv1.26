from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.utils import timezone

from .services import is_e164_phone_number, is_valid_email_address, normalize_phone_number
from .models import (
    BillingRecord,
    Consumer,
    ConsumerProfile,
    MeterReading,
    Payment,
    SMSBlast,
    SystemSettings,
)


class LoginForm(AuthenticationForm):
    username = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control'}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={'class': 'form-control'}))


class SignUpForm(UserCreationForm):
    full_name = forms.CharField(max_length=255)
    email = forms.EmailField(required=False)
    contact = forms.CharField(max_length=50, required=False)
    address = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), required=False)

    class Meta:
        model = User
        fields = ('username', 'full_name', 'email', 'contact', 'address', 'password1', 'password2')

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
                role='consumer',
            )
            Consumer.objects.create(
                profile=profile,
                full_name=profile.full_name,
                address=profile.address,
                contact_number=profile.contact,
                status='active',
            )
        return user


class ConsumerForm(forms.ModelForm):
    class Meta:
        model = Consumer
        fields = ['profile', 'full_name', 'address', 'contact_number', 'status', 'photo']
        widgets = {
            'address': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        available_profiles = ConsumerProfile.objects.filter(role=ConsumerProfile.Roles.CONSUMER).exclude(
            consumer_record__isnull=False
        )
        if self.instance.pk and self.instance.profile_id:
            available_profiles = ConsumerProfile.objects.filter(pk=self.instance.profile_id) | available_profiles
        self.fields['profile'].queryset = available_profiles.order_by('full_name')
        self.fields['profile'].required = False

    def clean_contact_number(self):
        contact_number = normalize_phone_number(self.cleaned_data.get('contact_number', ''))
        if contact_number and not is_e164_phone_number(contact_number):
            raise forms.ValidationError('Enter the contact number in E.164 format, for example +639171234567.')
        return contact_number


class BillingRecordForm(forms.ModelForm):
    class Meta:
        model = BillingRecord
        fields = [
            'consumer',
            'billing_month',
            'previous_reading',
            'current_reading',
            'rate_per_m3',
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


class AdminPaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ['consumer', 'billing', 'payment_method', 'amount_paid', 'payment_date', 'status', 'reference_number']
        widgets = {
            'payment_date': forms.DateInput(attrs={'type': 'date'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['consumer'].queryset = Consumer.objects.order_by('full_name')
        self.fields['billing'].queryset = BillingRecord.objects.select_related('consumer').order_by('-billing_month')


def build_payment_method_choices(system_settings):
    choices = []
    if system_settings.enable_cash_payments:
        choices.append((Payment.Methods.CASH, Payment.Methods.CASH.label))
    if system_settings.enable_online_payments:
        choices.extend(
            [
                (Payment.Methods.GCASH, Payment.Methods.GCASH.label),
                (Payment.Methods.PAYMAYA, Payment.Methods.PAYMAYA.label),
                (Payment.Methods.BANK, Payment.Methods.BANK.label),
            ]
        )
    return choices


class ConsumerPaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ['billing', 'payment_method', 'amount_paid', 'reference_number']

    def __init__(self, *args, consumer=None, system_settings=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.consumer = consumer
        self.system_settings = system_settings or SystemSettings.load()
        self.fields['billing'].queryset = BillingRecord.objects.none()
        self.fields['payment_method'].choices = build_payment_method_choices(self.system_settings)
        if consumer:
            self.fields['billing'].queryset = consumer.billings.exclude(status=BillingRecord.Statuses.PAID).order_by(
                '-billing_month'
            )

    def clean(self):
        cleaned_data = super().clean()
        billing = cleaned_data.get('billing')
        if billing and self.consumer and billing.consumer_id != self.consumer.id:
            raise forms.ValidationError('You can only pay bills assigned to your account.')
        if not self.fields['payment_method'].choices:
            raise forms.ValidationError('Payment channels are not enabled yet. Please contact the administrator.')
        return cleaned_data

    def clean_amount_paid(self):
        amount_paid = self.cleaned_data['amount_paid']
        if amount_paid <= 0:
            raise forms.ValidationError('Amount paid must be greater than zero.')
        return amount_paid

    def save(self, commit=True):
        payment = super().save(commit=False)
        payment.consumer = self.consumer
        payment.payment_date = timezone.localdate()
        payment.status = Payment.Statuses.PENDING
        if commit:
            payment.save()
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


class MeterReadingForm(forms.ModelForm):
    class Meta:
        model = MeterReading
        fields = ['consumer', 'reading_date', 'current_reading', 'notes']
        widgets = {
            'reading_date': forms.DateInput(attrs={'type': 'date'}),
            'notes': forms.Textarea(attrs={'rows': 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['consumer'].queryset = Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).order_by('full_name')

    def clean_current_reading(self):
        current_reading = self.cleaned_data['current_reading']
        if current_reading < 0:
            raise forms.ValidationError('Reading must be zero or greater.')
        return current_reading


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
