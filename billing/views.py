from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    AdminPaymentForm,
    BillingRecordForm,
    ConsumerForm,
    ConsumerPaymentForm,
    EmailBlastForm,
    LoginForm,
    MeterReadingForm,
    SignUpForm,
    SMSBlastForm,
    SystemSettingsForm,
    TestEmailForm,
    TestSMSForm,
)
from .models import BillingRecord, Consumer, ConsumerProfile, MeterReading, Notification, Payment, SMSBlast, SystemSettings
from .permissions import PAYMENT_MANAGER_ROLES, role_required, ensure_user_profile, get_dashboard_url_for_user, get_linked_consumer, get_user_profile, get_user_role
from .services import (
    get_delivery_configuration_summary,
    handle_meter_reading_submission,
    notify_roles,
    send_billing_due_notification,
    send_email_blast,
    send_payment_notification,
    send_sms_blast,
    send_test_email,
    send_test_sms,
    update_payment_status,
)


def get_selected_month(month_value):
    if month_value:
        try:
            return datetime.strptime(month_value, '%Y-%m').date().replace(day=1)
        except ValueError:
            pass
    return timezone.localdate().replace(day=1)


def month_filter_kwargs(prefix, selected_month):
    return {
        f'{prefix}__year': selected_month.year,
        f'{prefix}__month': selected_month.month,
    }


def _format_money(value):
    if value is None:
        return '0.00'
    return f'{value:.2f}'


def _delivery_status_label(notification, enabled):
    if not enabled:
        return 'Disabled'
    if notification is None:
        return 'Skipped'
    return notification.get_status_display()


def _payment_status_payload(payment, previous_status, notification_results, system_settings):
    billing = payment.billing
    current_month = timezone.localdate().replace(day=1)
    monthly_collected = Payment.objects.filter(
        status=Payment.Statuses.COMPLETED,
        **month_filter_kwargs('payment_date', current_month),
    ).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0')

    email_status = _delivery_status_label(notification_results.get('email'), system_settings.notify_by_email)
    sms_status = _delivery_status_label(notification_results.get('sms'), system_settings.notify_by_sms)

    return {
        'ok': True,
        'payment_id': payment.id,
        'status': payment.status,
        'status_display': payment.get_status_display(),
        'previous_status': previous_status,
        'billing_status_display': billing.get_status_display() if billing else '-',
        'billing_amount_paid': _format_money(billing.amount_paid) if billing else '-',
        'billing_amount_due': _format_money(billing.amount_due) if billing else '-',
        'pending_payments': Payment.objects.filter(status=Payment.Statuses.PENDING).count(),
        'monthly_collected': _format_money(monthly_collected),
        'email_notification_status': email_status,
        'sms_notification_status': sms_status,
        'message': (
            f'Payment status updated to {payment.get_status_display()}. '
            f'Email: {email_status}. SMS: {sms_status}.'
        ),
    }


def home(request):
    total_consumers = Consumer.objects.count()
    total_billed = BillingRecord.objects.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    total_paid = BillingRecord.objects.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0')
    collection_efficiency = round((total_paid / total_billed) * 100, 2) if total_billed else 0

    context = {
        'total_consumers': total_consumers,
        'collection_efficiency': collection_efficiency,
    }
    return render(request, 'billing/home.html', context)


class RoleBasedLoginView(LoginView):
    template_name = 'registration/login.html'
    authentication_form = LoginForm

    def form_valid(self, form):
        response = super().form_valid(form)
        ensure_user_profile(self.request.user)
        return response

    def get_success_url(self):
        ensure_user_profile(self.request.user)
        return reverse(get_dashboard_url_for_user(self.request.user))


def signup_view(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, 'Account created successfully. You can now log in.')
            return redirect('login')
    else:
        form = SignUpForm()
    return render(request, 'registration/signup.html', {'form': form})


@login_required
def dashboard(request):
    ensure_user_profile(request.user)
    return redirect(get_dashboard_url_for_user(request.user))


@role_required(ConsumerProfile.Roles.ADMIN)
def admin_panel(request):
    current_month = timezone.localdate().replace(day=1)
    monthly_billed = BillingRecord.objects.filter(**month_filter_kwargs('billing_month', current_month)).aggregate(
        total=Sum('total_amount')
    )['total'] or Decimal('0')
    monthly_collected = Payment.objects.filter(
        status=Payment.Statuses.COMPLETED,
        **month_filter_kwargs('payment_date', current_month),
    ).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0')

    context = {
        'system_settings': SystemSettings.load(),
        'total_consumers': Consumer.objects.count(),
        'active_connections': Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).count(),
        'pending_payments': Payment.objects.filter(status=Payment.Statuses.PENDING).count(),
        'overdue_bills': BillingRecord.objects.filter(status=BillingRecord.Statuses.OVERDUE).count(),
        'monthly_billed': monthly_billed,
        'monthly_collected': monthly_collected,
        'recent_bills': BillingRecord.objects.select_related('consumer')[:8],
        'recent_payments': Payment.objects.select_related('consumer', 'billing')[:8],
        'recent_readings': MeterReading.objects.select_related('consumer', 'submitted_by')[:8],
        'recent_blasts': SMSBlast.objects.all()[:5],
        'current_month': current_month,
        'payment_status_choices': Payment.Statuses.choices,
    }
    return render(request, 'billing/admin_dashboard.html', context)


@role_required(ConsumerProfile.Roles.SECRETARY)
def secretary_panel(request):
    selected_month = get_selected_month(request.GET.get('month'))
    billings = BillingRecord.objects.filter(**month_filter_kwargs('billing_month', selected_month)).select_related('consumer')
    payments = Payment.objects.filter(**month_filter_kwargs('payment_date', selected_month)).select_related('consumer', 'billing')
    readings = MeterReading.objects.filter(**month_filter_kwargs('billing_month', selected_month)).select_related(
        'consumer',
        'submitted_by',
    )

    context = {
        'selected_month': selected_month,
        'monthly_billings': billings[:20],
        'monthly_payments': payments[:20],
        'monthly_readings': readings[:20],
        'total_billed': billings.aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
        'total_collected': payments.filter(status=Payment.Statuses.COMPLETED).aggregate(total=Sum('amount_paid'))['total']
        or Decimal('0'),
        'total_usage': readings.aggregate(total=Sum('usage_m3'))['total'] or Decimal('0'),
        'paid_bills': billings.filter(status=BillingRecord.Statuses.PAID).count(),
        'pending_bills': billings.exclude(status=BillingRecord.Statuses.PAID).count(),
    }
    return render(request, 'billing/secretary_dashboard.html', context)


@role_required(ConsumerProfile.Roles.TREASURER)
def treasurer_panel(request):
    current_month = timezone.localdate().replace(day=1)
    completed_payments = Payment.objects.filter(
        status=Payment.Statuses.COMPLETED,
        **month_filter_kwargs('payment_date', current_month),
    ).select_related('consumer', 'billing')
    pending_payments = Payment.objects.filter(status=Payment.Statuses.PENDING).select_related('consumer', 'billing')

    context = {
        'current_month': current_month,
        'monthly_collected': completed_payments.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0'),
        'pending_requests': pending_payments[:20],
        'recent_receipts': Payment.objects.filter(status=Payment.Statuses.COMPLETED).select_related('consumer', 'billing')[:20],
        'overdue_bills': BillingRecord.objects.filter(status=BillingRecord.Statuses.OVERDUE).select_related('consumer')[:10],
    }
    return render(request, 'billing/treasurer_dashboard.html', context)


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.READER)
def reader_panel(request):
    if request.method == 'POST':
        form = MeterReadingForm(request.POST)
        if form.is_valid():
            reading, billing = handle_meter_reading_submission(form, request.user)
            messages.success(
                request,
                f'Meter reading saved for {reading.consumer.full_name}. Billing record for {billing.billing_month:%B %Y} was updated.',
            )
            return redirect('reader_panel')
    else:
        form = MeterReadingForm(initial={'reading_date': timezone.localdate()})

    readings = MeterReading.objects.select_related('consumer', 'submitted_by')
    if get_user_role(request.user) == ConsumerProfile.Roles.READER:
        readings = readings.filter(submitted_by=request.user)

    return render(
        request,
        'billing/reader_dashboard.html',
        {
            'form': form,
            'recent_readings': readings[:20],
        },
    )


@role_required(ConsumerProfile.Roles.CONSUMER)
def consumer_panel(request):
    consumer = get_linked_consumer(request.user)
    system_settings = SystemSettings.load()
    payment_form = None

    if consumer:
        if request.method == 'POST':
            payment_form = ConsumerPaymentForm(request.POST, consumer=consumer, system_settings=system_settings)
            if payment_form.is_valid():
                payment = payment_form.save()
                notify_roles(
                    {ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.TREASURER},
                    'New payment submitted by consumer',
                    (
                        f'{consumer.full_name} submitted a {payment.get_payment_method_display()} payment of '
                        f'PHP {payment.amount_paid}.'
                    ),
                    Notification.Types.PAYMENT,
                    consumer=consumer,
                    payment=payment,
                    billing=payment.billing,
                )
                send_payment_notification(payment, system_settings=system_settings)
                messages.success(request, 'Payment submitted. The billing office can now monitor its status.')
                return redirect('consumer_panel')
        else:
            payment_form = ConsumerPaymentForm(consumer=consumer, system_settings=system_settings)

    context = {
        'consumer': consumer,
        'profile': get_user_profile(request.user, create=True),
        'payment_form': payment_form,
        'billing_records': consumer.billings.all()[:10] if consumer else BillingRecord.objects.none(),
        'payments': consumer.payments.all()[:10] if consumer else Payment.objects.none(),
        'receipts': consumer.payments.filter(status=Payment.Statuses.COMPLETED)[:10] if consumer else Payment.objects.none(),
        'meter_readings': consumer.meter_readings.all()[:10] if consumer else MeterReading.objects.none(),
        'notification_feed': Notification.objects.filter(
            recipient=request.user,
            channel=Notification.Channels.IN_APP,
        )[:10],
    }
    return render(request, 'billing/consumer_dashboard.html', context)


@role_required(ConsumerProfile.Roles.ADMIN)
def consumer_list(request):
    query = request.GET.get('q', '').strip()
    consumers = Consumer.objects.select_related('profile', 'profile__user').all()
    if query:
        consumers = consumers.filter(
            Q(full_name__icontains=query) | Q(address__icontains=query) | Q(contact_number__icontains=query)
        )
    return render(request, 'billing/consumers.html', {'consumers': consumers, 'query': query})


@role_required(ConsumerProfile.Roles.ADMIN)
def add_consumer(request):
    if request.method == 'POST':
        form = ConsumerForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, 'Consumer added successfully.')
            return redirect('consumers')
    else:
        form = ConsumerForm()
    return render(request, 'billing/add_consumer.html', {'form': form, 'page_title': 'Add Consumer'})


@role_required(ConsumerProfile.Roles.ADMIN)
def edit_consumer(request, consumer_id):
    consumer = get_object_or_404(Consumer, pk=consumer_id)
    if request.method == 'POST':
        form = ConsumerForm(request.POST, request.FILES, instance=consumer)
        if form.is_valid():
            form.save()
            messages.success(request, 'Consumer updated successfully.')
            return redirect('consumers')
    else:
        form = ConsumerForm(instance=consumer)
    return render(request, 'billing/add_consumer.html', {'form': form, 'page_title': 'Edit Consumer'})


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER)
def billing_list(request):
    billings = BillingRecord.objects.select_related('consumer')
    consumer_id = request.GET.get('consumer')
    if consumer_id:
        billings = billings.filter(consumer_id=consumer_id)
    consumers = Consumer.objects.order_by('full_name')
    return render(
        request,
        'billing/billing.html',
        {
            'billings': billings,
            'consumers': consumers,
            'selected_consumer': consumer_id,
            'can_add_billing': get_user_role(request.user) == ConsumerProfile.Roles.ADMIN,
        },
    )


@role_required(ConsumerProfile.Roles.ADMIN)
def add_billing(request):
    if request.method == 'POST':
        form = BillingRecordForm(request.POST)
        if form.is_valid():
            billing = form.save()
            send_billing_due_notification(billing)
            messages.success(request, 'Billing record created successfully and notifications were queued.')
            return redirect('billing')
    else:
        settings_obj = SystemSettings.load()
        form = BillingRecordForm(
            initial={
                'billing_date': timezone.localdate(),
                'due_date': timezone.localdate() + timedelta(days=settings_obj.billing_due_days),
                'rate_per_m3': settings_obj.rate_per_m3,
            }
        )
    return render(request, 'billing/add_billing.html', {'form': form})


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER)
def payments_list(request):
    user_role = get_user_role(request.user)
    can_edit = user_role in PAYMENT_MANAGER_ROLES

    if request.method == 'POST':
        if not can_edit:
            messages.error(request, 'Only admin and treasurer accounts can record payments.')
            return redirect('payments')

        form = AdminPaymentForm(request.POST)
        if form.is_valid():
            payment = form.save()
            send_payment_notification(payment)
            messages.success(request, 'Payment saved successfully and the consumer was notified.')
            return redirect('payments')
    else:
        form = AdminPaymentForm(
            initial={
                'payment_date': timezone.localdate(),
                'status': Payment.Statuses.COMPLETED,
            }
        ) if can_edit else None

    payments = Payment.objects.select_related('consumer', 'billing')
    return render(
        request,
        'billing/payments.html',
        {
            'payments': payments,
            'form': form,
            'can_edit': can_edit,
            'can_notify_payment': user_role == ConsumerProfile.Roles.ADMIN,
            'payment_status_choices': Payment.Statuses.choices,
        },
    )


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.TREASURER)
@require_POST
def update_payment_status_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related('consumer', 'billing'), pk=payment_id)
    new_status = request.POST.get('status', '').strip().lower()
    system_settings = SystemSettings.load()

    try:
        previous_status, notification_results = update_payment_status(payment, new_status, system_settings=system_settings)
    except ValueError as exc:
        return JsonResponse({'ok': False, 'message': str(exc)}, status=400)

    payment.refresh_from_db()
    if payment.billing_id:
        payment.billing.refresh_from_db()

    return JsonResponse(_payment_status_payload(payment, previous_status, notification_results, system_settings))


@role_required(ConsumerProfile.Roles.ADMIN)
def notify_payment_status(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related('consumer', 'billing'), pk=payment_id)
    send_payment_notification(payment)
    messages.success(request, f'Payment status notification sent for {payment.consumer.full_name}.')
    return redirect('payments')


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER)
def reports_view(request):
    selected_month = get_selected_month(request.GET.get('month'))
    billings = BillingRecord.objects.filter(**month_filter_kwargs('billing_month', selected_month))
    payments = Payment.objects.filter(**month_filter_kwargs('payment_date', selected_month))
    readings = MeterReading.objects.filter(**month_filter_kwargs('billing_month', selected_month))

    total_billed = billings.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
    total_collected = payments.filter(status=Payment.Statuses.COMPLETED).aggregate(total=Sum('amount_paid'))['total'] or Decimal('0')
    total_usage = readings.aggregate(total=Sum('usage_m3'))['total'] or Decimal('0')
    total_consumers = Consumer.objects.count()
    paid_bills = billings.filter(status=BillingRecord.Statuses.PAID).count()
    total_bills = billings.count()
    overdue_bills = billings.filter(status=BillingRecord.Statuses.OVERDUE).count()

    context = {
        'selected_month': selected_month,
        'active_percent': round((Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).count() / total_consumers) * 100)
        if total_consumers
        else 0,
        'paid_percent': round((paid_bills / total_bills) * 100) if total_bills else 0,
        'pending_percent': round((overdue_bills / total_bills) * 100) if total_bills else 0,
        'collection_rate': round((total_collected / total_billed) * 100) if total_billed else 0,
        'total_consumers': total_consumers,
        'total_bills': total_bills,
        'total_collected': total_collected,
        'total_billed': total_billed,
        'total_usage': total_usage,
        'recent_payments': payments.select_related('consumer', 'billing')[:10],
        'recent_readings': readings.select_related('consumer', 'submitted_by')[:10],
    }
    return render(request, 'billing/reports.html', context)


@role_required(ConsumerProfile.Roles.ADMIN)
def communications_view(request):
    sms_form = SMSBlastForm()
    email_form = EmailBlastForm()
    test_sms_form = TestSMSForm()
    test_email_form = TestEmailForm()

    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'sms':
            sms_form = SMSBlastForm(request.POST)
            if sms_form.is_valid():
                blast = send_sms_blast(
                    request.user,
                    sms_form.cleaned_data['audience'],
                    sms_form.cleaned_data['message'],
                )
                messages.success(
                    request,
                    f'SMS blast processed. Sent: {blast.sent_count}, Failed: {blast.failed_count}, Total recipients: {blast.total_recipients}.',
                )
                return redirect('communications')
            messages.error(request, 'SMS blast could not be sent. Check the form fields and provider configuration.')
        elif action == 'email':
            email_form = EmailBlastForm(request.POST)
            if email_form.is_valid():
                result = send_email_blast(
                    email_form.cleaned_data['audience'],
                    email_form.cleaned_data['subject'],
                    email_form.cleaned_data['message'],
                )
                messages.success(
                    request,
                    f'Email blast processed. Sent: {result["sent"]}, Failed: {result["failed"]}, Total recipients: {result["total"]}.',
                )
                return redirect('communications')
            messages.error(request, 'Email blast could not be sent. Check the form fields and provider configuration.')
        elif action == 'test_sms':
            test_sms_form = TestSMSForm(request.POST)
            if test_sms_form.is_valid():
                result = send_test_sms(
                    test_sms_form.cleaned_data['phone_number'],
                    test_sms_form.cleaned_data['message'],
                )
                feedback = 'SMS test sent successfully.' if result.status == Notification.Statuses.SENT else 'SMS test failed.'
                messages.success(request, feedback) if result.status == Notification.Statuses.SENT else messages.error(
                    request,
                    feedback,
                )
                return redirect('communications')
            messages.error(request, 'SMS test could not be sent. Check the phone number format and Twilio configuration.')
        elif action == 'test_email':
            test_email_form = TestEmailForm(request.POST)
            if test_email_form.is_valid():
                result = send_test_email(
                    test_email_form.cleaned_data['email'],
                    test_email_form.cleaned_data['subject'],
                    test_email_form.cleaned_data['message'],
                )
                feedback = 'Email test sent successfully.' if result.status == Notification.Statuses.SENT else 'Email test failed.'
                messages.success(request, feedback) if result.status == Notification.Statuses.SENT else messages.error(
                    request,
                    feedback,
                )
                return redirect('communications')
            messages.error(request, 'Email test could not be sent. Check the email address and provider configuration.')

    return render(
        request,
        'billing/communications.html',
        {
            'sms_form': sms_form,
            'email_form': email_form,
            'test_sms_form': test_sms_form,
            'test_email_form': test_email_form,
            'recent_blasts': SMSBlast.objects.all()[:10],
            'recent_notifications': Notification.objects.exclude(channel=Notification.Channels.IN_APP)[:20],
            'delivery_config': get_delivery_configuration_summary(),
        },
    )


@role_required(ConsumerProfile.Roles.ADMIN)
def payment_settings_view(request):
    settings_obj = SystemSettings.load()
    if request.method == 'POST':
        form = SystemSettingsForm(request.POST, instance=settings_obj)
        if form.is_valid():
            form.save()
            messages.success(request, 'Payment and notification settings updated successfully.')
            return redirect('payment_settings')
    else:
        form = SystemSettingsForm(instance=settings_obj)

    return render(request, 'billing/payment_settings.html', {'form': form})


@login_required
def notifications_view(request):
    Notification.objects.filter(
        recipient=request.user,
        channel=Notification.Channels.IN_APP,
        is_read=False,
    ).update(is_read=True)
    notifications = Notification.objects.filter(recipient=request.user, channel=Notification.Channels.IN_APP)
    return render(request, 'billing/notifications.html', {'notifications': notifications})


@login_required
def profile_view(request):
    profile = get_user_profile(request.user, create=True)
    consumer = get_linked_consumer(request.user)
    role = get_user_role(request.user)

    if consumer:
        billing_records = consumer.billings.all()[:10]
        payments = consumer.payments.all()[:10]
        meter_readings = consumer.meter_readings.all()[:10]
    else:
        billing_records = BillingRecord.objects.none()
        payments = Payment.objects.none()
        meter_readings = MeterReading.objects.none()

    context = {
        'profile': profile,
        'consumer': consumer,
        'billing_records': billing_records,
        'payments': payments,
        'meter_readings': meter_readings,
        'role': role,
    }
    return render(request, 'billing/profile.html', context)
