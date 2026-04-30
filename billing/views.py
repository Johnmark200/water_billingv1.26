from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
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
    MeterReadingUpdateForm,
    ProfileUpdateForm,
    SignUpForm,
    SMSBlastForm,
    SystemSettingsForm,
    TestEmailForm,
    TestSMSForm,
)
from .models import BillingRecord, Consumer, ConsumerProfile, MeterReading, Notification, Payment, SMSBlast, SystemSettings
from .permissions import PAYMENT_MANAGER_ROLES, role_required, ensure_user_profile, get_dashboard_url_for_user, get_linked_consumer, get_user_profile, get_user_role
from .services import (
    create_or_update_billing_from_reading,
    create_paymongo_ewallet_payment,
    extract_paymongo_transaction_details,
    get_consumer_billing_comparison,
    get_consumer_monthly_billings,
    get_delivery_configuration_summary,
    get_next_payment_month,
    get_previous_reading_details,
    handle_meter_reading_submission,
    month_start,
    paymongo_intent_is_paid,
    retrieve_paymongo_payment_intent,
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


def _store_paymongo_gateway_start(payment, ewallet_payment):
    payment_intent = ewallet_payment.get('attached_intent') or ewallet_payment.get('intent') or {}
    details = extract_paymongo_transaction_details(payment_intent)
    intent_id = details.get('intent_id') or payment.gateway_reference

    payment.gateway = 'paymongo'
    payment.gateway_reference = intent_id
    payment.reference_number = intent_id
    payment.gateway_status = details.get('payment_status') or details.get('intent_status') or ''
    payment.gateway_redirect_url = ewallet_payment.get('redirect_url', '')
    payment.gateway_response = ewallet_payment
    payment.save(
        update_fields=[
            'gateway',
            'gateway_reference',
            'reference_number',
            'gateway_status',
            'gateway_redirect_url',
            'gateway_response',
        ]
    )


def _store_paymongo_gateway_result(payment, payment_intent):
    details = extract_paymongo_transaction_details(payment_intent)
    intent_id = details.get('intent_id') or payment.gateway_reference or payment.reference_number

    payment.gateway = 'paymongo'
    payment.gateway_reference = intent_id
    payment.reference_number = intent_id
    payment.gateway_payment_id = details.get('payment_id') or payment.gateway_payment_id
    payment.gateway_status = details.get('payment_status') or details.get('intent_status') or payment.gateway_status
    payment.gateway_response = {
        'payment_intent': payment_intent,
        'details': details,
    }
    payment.save(
        update_fields=[
            'gateway',
            'gateway_reference',
            'reference_number',
            'gateway_payment_id',
            'gateway_status',
            'gateway_response',
        ]
    )


def _is_ajax(request):
    return request.headers.get('x-requested-with') == 'XMLHttpRequest'


def _scoped_reader_readings(user):
    readings = MeterReading.objects.select_related('consumer', 'submitted_by')
    if get_user_role(user) == ConsumerProfile.Roles.READER:
        readings = readings.filter(submitted_by=user)
    return readings


def _build_profile_context(user):
    profile = get_user_profile(user, create=True)
    consumer = get_linked_consumer(user)

    return {
        'profile': profile,
        'consumer': consumer,
        'role': get_user_role(user),
        'profile_form': ProfileUpdateForm(instance=profile),
        'billing_records': get_consumer_monthly_billings(consumer, limit=10) if consumer else [],
        'payments': consumer.payments.select_related('billing').all()[:10] if consumer else Payment.objects.none(),
        'meter_readings': consumer.meter_readings.all()[:10] if consumer else MeterReading.objects.none(),
    }


def _build_reader_panel_context(request, form=None, edit_form=None):
    return {
        **_build_profile_context(request.user),
        'form': form or MeterReadingForm(initial={'reading_date': timezone.localdate()}),
        'edit_form': edit_form or MeterReadingUpdateForm(),
        'recent_readings': _scoped_reader_readings(request.user)[:20],
    }


def _build_consumer_panel_context(request, payment_form=None):
    consumer = get_linked_consumer(request.user)
    system_settings = SystemSettings.load()
    current_billing, previous_billing = get_consumer_billing_comparison(consumer)

    context = {
        **_build_profile_context(request.user),
        'consumer': consumer,
        'payment_form': payment_form,
        'billing_records': get_consumer_monthly_billings(consumer, limit=10) if consumer else [],
        'payments': consumer.payments.select_related('billing').all()[:10] if consumer else Payment.objects.none(),
        'receipts': (
            consumer.payments.filter(status=Payment.Statuses.COMPLETED).select_related('billing')[:10]
            if consumer
            else Payment.objects.none()
        ),
        'meter_readings': consumer.meter_readings.all()[:10] if consumer else MeterReading.objects.none(),
        'notification_feed': Notification.objects.filter(
            recipient=request.user,
            channel=Notification.Channels.IN_APP,
        )[:10],
        'current_billing': current_billing,
        'previous_billing': previous_billing,
        'next_payment_month': get_next_payment_month(consumer) if consumer else None,
        'system_settings': system_settings,
    }

    if consumer and context['payment_form'] is None:
        context['payment_form'] = ConsumerPaymentForm(consumer=consumer, system_settings=system_settings)

    return context


def _render_profile_response_payload(request, context):
    return {
        'ok': True,
        'message': 'Profile updated successfully.',
        'summary_html': render_to_string('billing/includes/profile_summary.html', context, request=request),
        'form_html': render_to_string('billing/includes/profile_form.html', context, request=request),
    }


def _render_reader_live_payload(request, context, message=''):
    return {
        'ok': True,
        'message': message,
        'summary_html': render_to_string('billing/includes/profile_summary.html', context, request=request),
        'rows_html': render_to_string('billing/includes/reader_reading_rows.html', context, request=request),
        'form_html': render_to_string('billing/includes/reader_reading_form.html', context, request=request),
        'edit_form_html': render_to_string('billing/includes/reader_edit_form.html', context, request=request),
    }


def _render_consumer_live_payload(request, context, message=''):
    return {
        'ok': True,
        'message': message,
        'summary_html': render_to_string('billing/includes/profile_summary.html', context, request=request),
        'comparison_html': render_to_string('billing/includes/consumer_billing_comparison.html', context, request=request),
        'receipt_rows_html': render_to_string('billing/includes/consumer_receipt_rows.html', context, request=request),
        'billing_rows_html': render_to_string('billing/includes/consumer_billing_rows.html', context, request=request),
        'reading_rows_html': render_to_string('billing/includes/consumer_reading_rows.html', context, request=request),
        'payment_rows_html': render_to_string('billing/includes/consumer_payment_rows.html', context, request=request),
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

        context = _build_reader_panel_context(request, form=form)
        return render(request, 'billing/reader_dashboard.html', context)

    context = _build_reader_panel_context(request)
    return render(request, 'billing/reader_dashboard.html', context)


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
                if payment.payment_method == Payment.Methods.ONLINE:
                    try:
                        ewallet_payment = create_paymongo_ewallet_payment(
                            payment,
                            request.build_absolute_uri(reverse('paymongo_success', args=[payment.id])),
                            request.build_absolute_uri(reverse('paymongo_cancel', args=[payment.id])),
                        )
                        _store_paymongo_gateway_start(payment, ewallet_payment)
                    except Exception as exc:
                        update_payment_status(
                            payment,
                            Payment.Statuses.FAILED,
                            system_settings=system_settings,
                        )
                        messages.error(request, f'Online payment could not be started: {exc}')
                        return redirect('consumer_panel')

                    notify_roles(
                        {ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.TREASURER},
                        'New online payment started by consumer',
                        (
                            f'{consumer.full_name} started an online PayMongo payment of '
                            f'PHP {payment.amount_paid}.'
                        ),
                        Notification.Types.PAYMENT,
                        consumer=consumer,
                        payment=payment,
                        billing=payment.billing,
                    )
                    return redirect(ewallet_payment['redirect_url'])

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

    context = _build_consumer_panel_context(request, payment_form=payment_form)
    return render(request, 'billing/consumer_dashboard.html', context)


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.READER)
def reader_panel_data(request):
    context = _build_reader_panel_context(request)
    return JsonResponse(_render_reader_live_payload(request, context))


@role_required(ConsumerProfile.Roles.CONSUMER)
def consumer_panel_data(request):
    context = _build_consumer_panel_context(request)
    return JsonResponse(_render_consumer_live_payload(request, context))


@login_required
@require_POST
def update_profile_view(request):
    profile = get_user_profile(request.user, create=True)
    form = ProfileUpdateForm(request.POST, request.FILES, instance=profile)

    if form.is_valid():
        form.save()
        context = _build_profile_context(request.user)
        if _is_ajax(request):
            return JsonResponse(_render_profile_response_payload(request, context))
        messages.success(request, 'Profile updated successfully.')
    else:
        if _is_ajax(request):
            context = {
                **_build_profile_context(request.user),
                'profile_form': form,
            }
            return JsonResponse(
                {
                    'ok': False,
                    'message': 'Please fix the highlighted profile fields.',
                    'form_html': render_to_string('billing/includes/profile_form.html', context, request=request),
                },
                status=400,
            )
        messages.error(request, 'Please fix the highlighted profile fields.')

    return redirect(request.POST.get('next') or 'profile')


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.READER)
@require_POST
def submit_reader_reading(request):
    form = MeterReadingForm(request.POST)
    if form.is_valid():
        reading, billing = handle_meter_reading_submission(form, request.user)
        context = _build_reader_panel_context(request)
        payload = _render_reader_live_payload(
            request,
            context,
            message=(
                f'Meter reading saved for {reading.consumer.full_name}. '
                f'Billing record for {billing.billing_month:%B %Y} was updated.'
            ),
        )
        payload['reading_id'] = reading.id
        if _is_ajax(request):
            return JsonResponse(payload)
        messages.success(request, payload['message'])
        return redirect('reader_panel')

    if _is_ajax(request):
        context = _build_reader_panel_context(request, form=form)
        return JsonResponse(
            {
                'ok': False,
                'message': 'Please correct the reading details and try again.',
                'form_html': render_to_string('billing/includes/reader_reading_form.html', context, request=request),
            },
            status=400,
        )

    return render(request, 'billing/reader_dashboard.html', _build_reader_panel_context(request, form=form))


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.READER)
def reader_reading_context(request):
    consumer_id = request.GET.get('consumer')
    reading_date_value = request.GET.get('reading_date')
    try:
        reading_date = datetime.strptime(reading_date_value, '%Y-%m-%d').date() if reading_date_value else timezone.localdate()
    except ValueError:
        reading_date = timezone.localdate()

    consumer = get_object_or_404(Consumer, pk=consumer_id, status=Consumer.Statuses.ACTIVE)
    details = get_previous_reading_details(consumer, reading_date)
    current_reading = (
        MeterReading.objects.filter(consumer=consumer, billing_month=month_start(reading_date))
        .order_by('-created_at')
        .first()
    )

    return JsonResponse(
        {
            'ok': True,
            'previous_reading': str(details['value']),
            'previous_month': details['month'].strftime('%B %Y') if details['month'] else '',
            'current_month': month_start(reading_date).strftime('%B %Y'),
            'current_reading': str(current_reading.current_reading) if current_reading else '',
            'notes': current_reading.notes if current_reading else '',
        }
    )


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.READER)
@require_POST
def update_reader_reading(request, reading_id):
    readings = _scoped_reader_readings(request.user)
    reading = get_object_or_404(readings, pk=reading_id)
    form = MeterReadingUpdateForm(request.POST, instance=reading)

    if form.is_valid():
        updated_reading = form.save(commit=False)
        previous_details = get_previous_reading_details(updated_reading.consumer, updated_reading.billing_month)
        updated_reading.previous_reading = previous_details['value']
        updated_reading.save()
        billing = create_or_update_billing_from_reading(updated_reading)
        context = _build_reader_panel_context(request)
        payload = _render_reader_live_payload(
            request,
            context,
            message=(
                f'Reading for {updated_reading.consumer.full_name} was updated. '
                f'Billing for {billing.billing_month:%B %Y} is now in sync.'
            ),
        )
        payload['reading_id'] = updated_reading.id
        return JsonResponse(payload)

    context = _build_reader_panel_context(request, edit_form=form)
    return JsonResponse(
        {
            'ok': False,
            'message': 'Please correct the reading update and try again.',
            'edit_form_html': render_to_string('billing/includes/reader_edit_form.html', context, request=request),
        },
        status=400,
    )


@role_required(ConsumerProfile.Roles.CONSUMER)
def paymongo_success(request, payment_id):
    consumer = get_linked_consumer(request.user)
    payment = get_object_or_404(Payment.objects.select_related('consumer', 'billing'), pk=payment_id, consumer=consumer)

    if payment.payment_method != Payment.Methods.ONLINE:
        messages.error(request, 'That payment is not an online PayMongo transaction.')
        return redirect('consumer_panel')
    gateway_reference = payment.gateway_reference or payment.reference_number
    if not gateway_reference:
        messages.error(request, 'No PayMongo payment reference is linked to this payment.')
        return redirect('consumer_panel')

    system_settings = SystemSettings.load()
    try:
        payment_intent = retrieve_paymongo_payment_intent(gateway_reference)
        _store_paymongo_gateway_result(payment, payment_intent)
    except Exception as exc:
        messages.error(request, f'Unable to verify PayMongo payment: {exc}')
        return redirect('consumer_panel')

    if paymongo_intent_is_paid(payment_intent):
        update_payment_status(payment, Payment.Statuses.COMPLETED, system_settings=system_settings)
        messages.success(request, 'Online payment verified and marked as completed.')
    elif payment.gateway_status in {'awaiting_payment_method', 'failed', 'canceled', 'cancelled'}:
        update_payment_status(payment, Payment.Statuses.FAILED, system_settings=system_settings)
        messages.error(request, 'PayMongo returned the transaction as failed or cancelled.')
    else:
        messages.info(request, 'PayMongo returned, but the payment is still pending verification.')
    return redirect('consumer_panel')


@role_required(ConsumerProfile.Roles.CONSUMER)
def paymongo_cancel(request, payment_id):
    consumer = get_linked_consumer(request.user)
    payment = get_object_or_404(Payment.objects.select_related('consumer', 'billing'), pk=payment_id, consumer=consumer)

    gateway_reference = payment.gateway_reference or payment.reference_number
    if gateway_reference:
        try:
            payment_intent = retrieve_paymongo_payment_intent(gateway_reference)
            _store_paymongo_gateway_result(payment, payment_intent)
        except Exception as exc:
            messages.warning(request, f'Unable to refresh PayMongo status: {exc}')

    if payment.payment_method == Payment.Methods.ONLINE and payment.status == Payment.Statuses.PENDING:
        update_payment_status(payment, Payment.Statuses.FAILED, system_settings=SystemSettings.load())
    messages.error(request, 'Online payment was cancelled or failed.')
    return redirect('consumer_panel')


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
    return render(request, 'billing/profile.html', _build_profile_context(request.user))
