import csv
from io import BytesIO
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from PIL import Image, ImageDraw, ImageFont

from .forms import (
    AdminPaymentForm,
    BillingRecordForm,
    ConsumerForm,
    ConsumerPaymentForm,
    EmailBlastForm,
    LoginForm,
    MeterReadingForm,
    MeterReadingUpdateForm,
    PortalAccountForm,
    ProfileUpdateForm,
    SignUpForm,
    SMSBlastForm,
    SystemSettingsForm,
    TestEmailForm,
    TestSMSForm,
)
from .models import AuditLog, BillingRecord, Consumer, ConsumerProfile, MeterReading, Notification, Payment, SMSBlast, SystemSettings
from .permissions import PAYMENT_MANAGER_ROLES, role_required, ensure_user_profile, get_dashboard_url_for_user, get_linked_consumer, get_user_profile, get_user_role
from .services import (
    apply_panel_billing_status,
    build_reporting_month_choices,
    build_settlement_snapshot,
    build_system_monitoring_data,
    build_consumer_chart_data,
    create_or_update_billing_from_reading,
    create_paymongo_ewallet_payment,
    extract_paymongo_transaction_details,
    get_consumer_billing_comparison,
    get_existing_billing_for_month,
    get_consumer_monthly_billings,
    get_delivery_configuration_summary,
    get_payments_received_for_month,
    get_preferred_billing_records,
    get_next_payment_month,
    get_previous_reading_details,
    get_statement_payment_records,
    handle_meter_reading_submission,
    is_paymongo_configured,
    log_audit_action,
    month_start,
    paymongo_intent_is_paid,
    retrieve_paymongo_payment_intent,
    notify_roles,
    resolve_billing_status,
    send_billing_due_notification,
    send_email_blast,
    send_payment_notification,
    send_sms_blast,
    send_test_email,
    send_test_sms,
    sync_existing_billings_with_settings,
    update_payment_status,
)


def get_selected_month(month_value):
    if month_value:
        try:
            return datetime.strptime(month_value, '%Y-%m').date().replace(day=1)
        except ValueError:
            pass
    return timezone.localdate().replace(day=1)


def get_selected_date(date_value, default):
    if date_value:
        try:
            return datetime.strptime(date_value, '%Y-%m-%d').date()
        except ValueError:
            pass
    return default


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


def _pdf_font(size, bold=False):
    candidates = [
        'arialbd.ttf' if bold else 'arial.ttf',
        'calibrib.ttf' if bold else 'calibri.ttf',
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_wrapped_text(draw, xy, text, font, fill, max_width, line_gap=5):
    x, y = xy
    words = str(text or '').split()
    lines = []
    current = ''
    for word in words:
        candidate = f'{current} {word}'.strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    for line in lines or ['']:
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_gap
    return y


def _money(value):
    return f'PHP {Decimal(value or 0):,.2f}'


def _has_unpaid_overdue_balance(billing):
    return resolve_billing_status(billing) == BillingRecord.Statuses.OVERDUE and billing.amount_due > 0


def _build_admin_panel_context(selected_month=None):
    selected_month = month_start(selected_month) or timezone.localdate().replace(day=1)
    current_month_billings = get_preferred_billing_records(
        BillingRecord.objects.filter(**month_filter_kwargs('billing_month', selected_month))
    )
    all_billings = get_preferred_billing_records(BillingRecord.objects.all())
    monthly_payments = get_payments_received_for_month(selected_month)
    monthly_collected = sum(
        (payment.amount_paid for payment in monthly_payments if payment.status == Payment.Statuses.COMPLETED),
        Decimal('0'),
    )

    return {
        'system_settings': SystemSettings.load(),
        'total_consumers': Consumer.objects.count(),
        'active_connections': Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).count(),
        'pending_payments': Payment.objects.filter(status=Payment.Statuses.PENDING).count(),
        'overdue_bills': sum(1 for bill in all_billings if _has_unpaid_overdue_balance(bill)),
        'monthly_billed': sum((bill.total_amount for bill in current_month_billings), Decimal('0')),
        'monthly_collected': monthly_collected,
        'recent_bills': current_month_billings[:8],
        'recent_payments': monthly_payments[:8],
        'recent_readings': MeterReading.objects.filter(
            **month_filter_kwargs('billing_month', selected_month)
        ).select_related('consumer', 'submitted_by')[:8],
        'recent_blasts': SMSBlast.objects.all()[:5],
        'recent_audit_logs': AuditLog.objects.select_related('user')[:8],
        'delivery_config': get_delivery_configuration_summary(),
        'paymongo_configured': is_paymongo_configured(),
        'admin_monitoring_data': build_system_monitoring_data(selected_month=selected_month),
        'selected_month': selected_month,
        'admin_month_choices': build_reporting_month_choices(),
        'payment_status_choices': Payment.Statuses.choices,
    }


def _soa_payment_scope(selected_month):
    return (
        Q(**month_filter_kwargs('covered_month', selected_month))
        | Q(covered_month__isnull=True, **month_filter_kwargs('billing__billing_month', selected_month))
        | Q(covered_month__isnull=True, billing__isnull=True, **month_filter_kwargs('payment_date', selected_month))
    )


def _build_monthly_statement_context(selected_month, consumer=None):
    billings_queryset = BillingRecord.objects.filter(
        **month_filter_kwargs('billing_month', selected_month)
    ).select_related('consumer')
    if consumer is not None:
        billings_queryset = billings_queryset.filter(consumer=consumer)

    readings_queryset = MeterReading.objects.filter(
        **month_filter_kwargs('billing_month', selected_month)
    ).select_related('consumer', 'submitted_by')
    if consumer is not None:
        readings_queryset = readings_queryset.filter(consumer=consumer)

    monthly_billings = [apply_panel_billing_status(billing) for billing in get_preferred_billing_records(billings_queryset)]
    monthly_payments = get_statement_payment_records(selected_month, consumer=consumer)
    monthly_readings = list(readings_queryset.order_by('-reading_date', '-created_at'))
    settlement_snapshot = build_settlement_snapshot(selected_month=selected_month, consumer=consumer)

    return {
        'monthly_billings': monthly_billings[:20],
        'statement_billings': monthly_billings,
        'monthly_payments': monthly_payments[:20],
        'statement_payments': monthly_payments,
        'monthly_readings': monthly_readings[:20],
        'statement_readings': monthly_readings,
        'total_billed': sum((billing.total_amount for billing in monthly_billings), Decimal('0')),
        'total_collected': sum(
            (payment.amount_credited for payment in monthly_payments if payment.status == Payment.Statuses.COMPLETED),
            Decimal('0'),
        ),
        'total_usage': sum((reading.usage_m3 for reading in monthly_readings), Decimal('0')),
        'paid_bills': sum(1 for billing in monthly_billings if billing.panel_status == BillingRecord.Statuses.PAID),
        'pending_bills': settlement_snapshot['unsettled_count'],
        'pending_accounts_count': settlement_snapshot['pending_count'],
        'overdue_accounts_count': settlement_snapshot['overdue_count'],
        'pending_payments': sum(1 for payment in monthly_payments if payment.status == Payment.Statuses.PENDING),
        'failed_payments': sum(1 for payment in monthly_payments if payment.status == Payment.Statuses.FAILED),
        'settlement_accounts': settlement_snapshot['all_records'][:12],
        'settlement_accounts_total': settlement_snapshot['unsettled_total'],
    }


def _build_soa_transactions(selected_month=None, consumer=None, start_date=None, end_date=None):
    if start_date and end_date:
        billings_queryset = BillingRecord.objects.filter(billing_date__range=(start_date, end_date)).select_related('consumer')
        payments_queryset = Payment.objects.filter(payment_date__range=(start_date, end_date)).select_related('consumer', 'billing')
        if consumer:
            billings_queryset = billings_queryset.filter(consumer=consumer)
            payments_queryset = payments_queryset.filter(consumer=consumer)
        billings = get_preferred_billing_records(billings_queryset)
        payments = list(payments_queryset.order_by('-payment_date', '-created_at'))
    else:
        billings_queryset = BillingRecord.objects.filter(**month_filter_kwargs('billing_month', selected_month)).select_related('consumer')
        if consumer:
            billings_queryset = billings_queryset.filter(consumer=consumer)
        billings = get_preferred_billing_records(billings_queryset)
        payments = get_statement_payment_records(selected_month, consumer=consumer)

    transactions = []
    for bill in billings:
        transactions.append(
            {
                'date': bill.billing_date,
                'reference': f'BILL-{bill.id:05d}',
                'description': (
                    f'{bill.consumer.full_name} - Water billing for {bill.billing_month:%B %Y} | '
                    f'Usage {bill.usage_m3} m3 | Due {bill.due_date:%b %d, %Y} | {bill.get_status_display()}'
                ),
                'debit': bill.total_amount,
                'credit': Decimal('0'),
                'source': bill,
            }
        )
    for payment in payments:
        is_completed = payment.status == Payment.Statuses.COMPLETED
        covered_month = payment.display_covered_month
        covered_label = covered_month.strftime('%B %Y') if covered_month else 'the selected billing month'
        transactions.append(
            {
                'date': payment.payment_date,
                'reference': payment.reference_number or payment.gateway_reference or f'PAY-{payment.id:05d}',
                'description': (
                    f'{payment.consumer.full_name} - {payment.display_payment_method} payment '
                    f'for {covered_label} ({payment.get_status_display()})'
                ),
                'debit': Decimal('0'),
                'credit': payment.amount_credited if is_completed else Decimal('0'),
                'source': payment,
            }
        )
    transactions.sort(key=lambda item: (item['date'], item['reference']))
    return billings, payments, transactions


def _build_soa_summary(billings, payments):
    overdue_bills = [bill for bill in billings if _has_unpaid_overdue_balance(bill)]
    pending_payments = [payment for payment in payments if payment.status == Payment.Statuses.PENDING]
    completed_payments = [payment for payment in payments if payment.status == Payment.Statuses.COMPLETED]
    cash_payments = [payment for payment in completed_payments if payment.payment_method == Payment.Methods.CASH]
    online_payments = [payment for payment in completed_payments if payment.payment_method != Payment.Methods.CASH]

    return {
        'pending_payments_count': len(pending_payments),
        'pending_payments_total': sum((payment.amount_paid for payment in pending_payments), Decimal('0')),
        'overdue_accounts_count': len({bill.consumer_id for bill in overdue_bills}),
        'overdue_accounts_total': sum((bill.amount_due for bill in overdue_bills), Decimal('0')),
        'cash_payments_count': len(cash_payments),
        'cash_payments_total': sum((payment.amount_credited for payment in cash_payments), Decimal('0')),
        'online_payments_count': len(online_payments),
        'online_payments_total': sum((payment.amount_credited for payment in online_payments), Decimal('0')),
    }


def _build_soa_pdf(selected_month=None, consumer=None, start_date=None, end_date=None):
    width, height = 1240, 1754
    image = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(image)

    ink = '#111827'
    muted = '#4b5563'
    line = '#d1d5db'
    red = '#dc2626'
    gray_band = '#f3f4f6'
    left = 90
    right = width - 90

    font_title = _pdf_font(42, bold=True)
    font_heading = _pdf_font(28, bold=True)
    font_body = _pdf_font(21)
    font_body_bold = _pdf_font(21, bold=True)
    font_small = _pdf_font(17)
    font_small_bold = _pdf_font(17, bold=True)

    draw.rectangle((0, 0, width, 18), fill=red)
    logo_path = settings.BASE_DIR / 'static' / 'legacy_img' / 'tabuan-logo.png'
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert('RGBA')
            logo.thumbnail((120, 120))
            image.paste(logo.convert('RGB'), (right - 120, 70), logo)
        except OSError:
            draw.rectangle((right - 120, 70, right, 190), outline=line, width=2)
    else:
        draw.rectangle((right - 120, 70, right, 190), outline=line, width=2)

    draw.text((left, 65), 'STATEMENT OF ACCOUNT', font=font_title, fill=ink)
    draw.text((left, 130), 'Tabuan Water Billing System', font=font_heading, fill=ink)
    draw.text((left, 170), 'Tabuan, Bayawan City, Negros Oriental', font=font_body, fill=muted)

    target_label = consumer.full_name if consumer else 'All Consumers'
    target_address = consumer.address if consumer and consumer.address else 'Tabuan, Bayawan City, Negros Oriental'
    draw.text((left, 255), 'BILL TO', font=font_body_bold, fill=ink)
    draw.text((left, 292), target_label, font=font_body, fill=ink)
    y_after_address = _draw_wrapped_text(draw, (left, 324), target_address, font_small, muted, 360)

    draw.text((left, max(380, y_after_address + 16)), f'Statement Date: {timezone.localdate():%B %d, %Y}', font=font_small, fill=ink)
    draw.text(
        (left, max(412, y_after_address + 48)),
        'Terms: Payment is due on or before the billing due date shown on each record.',
        font=font_small,
        fill=ink,
    )
    period_start = start_date or selected_month
    period_end = end_date or selected_month
    period_label = f'{period_start:%B %d, %Y} to {period_end:%B %d, %Y}' if start_date and end_date else f'{selected_month:%B %Y}'
    reference_label = f'SOA-{period_start:%Y%m%d}-{period_end:%Y%m%d}' if start_date and end_date else f'SOA-{selected_month:%Y%m}'
    draw.text((left, max(444, y_after_address + 80)), f'Account Reference: {reference_label}', font=font_small, fill=ink)
    draw.text((left, max(476, y_after_address + 112)), f'Period: {period_label}', font=font_small, fill=ink)

    draw.text((right - 360, 250), 'Prepared for:', font=font_small_bold, fill=ink)
    draw.text((right - 360, 282), target_label, font=font_small, fill=muted)
    draw.text((right - 360, 326), 'Business:', font=font_small_bold, fill=ink)
    draw.text((right - 260, 326), 'Tabuan Water Billing System', font=font_small, fill=muted)
    draw.text((right - 360, 358), 'Email:', font=font_small_bold, fill=ink)
    draw.text((right - 260, 358), 'johnmarkomale200@gmail.com', font=font_small, fill=muted)

    billings, payments, transactions = _build_soa_transactions(
        selected_month,
        consumer=consumer,
        start_date=start_date,
        end_date=end_date,
    )

    table_y = 555
    columns = [
        ('Date', left, 115),
        ('Reference', left + 125, 150),
        ('Description', left + 285, 455),
        ('Debit', left + 750, 140),
        ('Credit', left + 895, 140),
        ('Balance', left + 1040, 105),
    ]
    draw.rectangle((left, table_y, right, table_y + 34), fill=gray_band)
    for label, x, _ in columns:
        draw.text((x, table_y + 7), label, font=font_small_bold, fill=ink)
    draw.line((left, table_y + 34, right, table_y + 34), fill=line, width=2)

    pages = [image]

    def draw_transaction_header(active_draw, header_y):
        active_draw.rectangle((left, header_y, right, header_y + 34), fill=gray_band)
        for label, x, _ in columns:
            active_draw.text((x, header_y + 7), label, font=font_small_bold, fill=ink)
        active_draw.line((left, header_y + 34, right, header_y + 34), fill=line, width=2)

    def add_continuation_page(page_number):
        page = Image.new('RGB', (width, height), 'white')
        active_draw = ImageDraw.Draw(page)
        active_draw.rectangle((0, 0, width, 18), fill=red)
        active_draw.text((left, 60), 'STATEMENT OF ACCOUNT', font=font_heading, fill=ink)
        active_draw.text((left, 100), f'Transactions continued - {target_label} - {period_label}', font=font_body, fill=muted)
        draw_transaction_header(active_draw, 150)
        pages.append(page)
        return page, active_draw, 202

    balance = Decimal('0')
    page_number = 1
    y = table_y + 52
    if not transactions:
        draw.text((left, y), 'No billing or payment transactions were found for this period.', font=font_body, fill=muted)
        y += 42
    else:
        for item in transactions:
            if y > 1280:
                image, draw, y = add_continuation_page(page_number + 1)
                page_number += 1
            balance += item['debit'] - item['credit']
            draw.text((columns[0][1], y), f"{item['date']:%m/%d/%y}", font=font_small, fill=ink)
            draw.text((columns[1][1], y), str(item['reference'])[:16], font=font_small, fill=ink)
            _draw_wrapped_text(draw, (columns[2][1], y), item['description'], font_small, ink, columns[2][2], line_gap=2)
            draw.text((columns[3][1], y), _money(item['debit']) if item['debit'] else '-', font=font_small, fill=ink)
            draw.text((columns[4][1], y), _money(item['credit']) if item['credit'] else '-', font=font_small, fill=ink)
            draw.text((columns[5][1], y), _money(balance), font=font_small, fill=ink)
            y += 48
            draw.line((left, y - 10, right, y - 10), fill='#eef2f7', width=1)

    total_debit = sum((item['debit'] for item in transactions), Decimal('0'))
    total_credit = sum((item['credit'] for item in transactions), Decimal('0'))
    current_balance = total_debit - total_credit
    amount_overdue = sum((bill.amount_due for bill in billings if _has_unpaid_overdue_balance(bill)), Decimal('0'))
    soa_summary = _build_soa_summary(billings, payments)

    summary_x = right - 360
    summary_y = max(y + 30, 1200)
    draw.text((summary_x, summary_y), 'Current Balance:', font=font_small_bold, fill=ink)
    draw.text((summary_x + 215, summary_y), _money(current_balance), font=font_small_bold, fill=ink)
    draw.line((summary_x + 210, summary_y + 32, right, summary_y + 32), fill=ink, width=2)
    draw.text((summary_x, summary_y + 50), 'Amount Overdue:', font=font_small_bold, fill=ink)
    draw.text((summary_x + 215, summary_y + 50), _money(amount_overdue), font=font_small_bold, fill=ink)
    draw.line((summary_x + 210, summary_y + 82, right, summary_y + 82), fill=ink, width=2)

    status_y = summary_y + 112
    draw.text((left, status_y), 'Payment and Account Summary', font=font_body_bold, fill=ink)
    summary_rows = [
        ('Pending Payments', soa_summary['pending_payments_count'], soa_summary['pending_payments_total']),
        ('Overdue Accounts', soa_summary['overdue_accounts_count'], soa_summary['overdue_accounts_total']),
        ('Cash Payments', soa_summary['cash_payments_count'], soa_summary['cash_payments_total']),
        ('Online Payments', soa_summary['online_payments_count'], soa_summary['online_payments_total']),
    ]
    row_y = status_y + 38
    for label, count, total in summary_rows:
        draw.text((left, row_y), f'{label}: {count}', font=font_small_bold, fill=ink)
        draw.text((left + 270, row_y), _money(total), font=font_small, fill=muted)
        row_y += 30

    notes_y = row_y + 28
    draw.text((left, notes_y), 'Payment Instructions', font=font_body_bold, fill=ink)
    draw.text((left, notes_y + 38), 'Please settle your balance at the billing office or through the approved payment channel.', font=font_small, fill=muted)
    draw.text((left, notes_y + 70), 'For questions about this statement, please contact the Secretary or Treasurer.', font=font_small, fill=muted)

    secretary_names = ', '.join(ConsumerProfile.objects.filter(role=ConsumerProfile.Roles.SECRETARY).values_list('full_name', flat=True)) or 'Secretary'
    treasurer_names = ', '.join(ConsumerProfile.objects.filter(role=ConsumerProfile.Roles.TREASURER).values_list('full_name', flat=True)) or 'Treasurer'
    footer_y = height - 145
    draw.line((left, footer_y, right, footer_y), fill=line, width=2)
    draw.text((left, footer_y + 26), f'Secretary: {secretary_names}', font=font_small, fill=ink)
    draw.text((left, footer_y + 58), f'Treasurer: {treasurer_names}', font=font_small, fill=ink)
    draw.text((right - 230, footer_y + 58), 'Page 1 of 1', font=font_small, fill=ink)
    draw.rectangle((0, height - 50, width, height), fill=red)
    draw.text((left, height - 36), 'System v1.26 | 2026 | Created by Omale J. Ohn', font=font_small, fill='white')

    output = BytesIO()
    pages[0].save(output, format='PDF', resolution=150.0, save_all=True, append_images=pages[1:])
    output.seek(0)
    return output


def _store_paymongo_gateway_start(payment, ewallet_payment):
    payment_intent = ewallet_payment.get('attached_intent') or ewallet_payment.get('intent') or {}
    details = extract_paymongo_transaction_details(payment_intent)
    intent_id = details.get('intent_id') or payment.gateway_reference
    online_channel = Payment.normalize_online_channel(
        (((ewallet_payment.get('payment_method') or {}).get('attributes') or {}).get('type'))
        or details.get('payment_method_type')
        or payment.gateway
    )

    if online_channel:
        payment.gateway = online_channel
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
    online_channel = Payment.normalize_online_channel(details.get('payment_method_type') or payment.gateway)

    if online_channel:
        payment.gateway = online_channel
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


def _build_receipt_context(payment):
    covered_month = payment.display_covered_month
    return {
        'payment': payment,
        'consumer': payment.consumer,
        'billing': payment.billing,
        'covered_month': covered_month,
        'receipt_number': f'REC-{payment.id:06d}',
        'reference_number': payment.display_reference_number,
        'system_name': 'Tabuan Water Billing System',
        'system_version': 'v1.26',
        'system_year': '2026',
        'creator_name': 'Omale J. Ohn',
    }


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
    role = get_user_role(user)
    show_all_consumer_transactions = role in {
        ConsumerProfile.Roles.SECRETARY,
        ConsumerProfile.Roles.TREASURER,
    }

    if show_all_consumer_transactions:
        billing_records = get_preferred_billing_records(BillingRecord.objects.select_related('consumer'), limit=10)
        payments = Payment.objects.select_related('consumer', 'billing')[:10]
        meter_readings = MeterReading.objects.select_related('consumer')[:10]
    else:
        billing_records = get_consumer_monthly_billings(consumer, limit=10) if consumer else []
        payments = consumer.payments.select_related('billing').all()[:10] if consumer else Payment.objects.none()
        meter_readings = consumer.meter_readings.all()[:10] if consumer else MeterReading.objects.none()

    return {
        'profile': profile,
        'consumer': consumer,
        'role': role,
        'profile_form': ProfileUpdateForm(instance=profile),
        'billing_records': billing_records,
        'payments': payments,
        'meter_readings': meter_readings,
        'show_all_consumer_transactions': show_all_consumer_transactions,
    }


def _build_reader_panel_context(request, form=None, edit_form=None):
    system_settings = SystemSettings.load()
    recent_readings = _scoped_reader_readings(request.user)[:20]
    recent_billings = []
    seen_billing_ids = set()
    for reading in recent_readings:
        billing = get_existing_billing_for_month(reading.consumer, reading.billing_month)
        if billing and billing.id not in seen_billing_ids:
            recent_billings.append(billing)
            seen_billing_ids.add(billing.id)

    return {
        **_build_profile_context(request.user),
        'form': form or MeterReadingForm(initial={'reading_date': timezone.localdate()}),
        'edit_form': edit_form or MeterReadingUpdateForm(),
        'recent_readings': recent_readings,
        'recent_billings': recent_billings[:20],
        'reader_total_readings': len(recent_readings),
        'reader_total_billings': len(recent_billings),
        'reader_total_usage': sum((reading.usage_m3 for reading in recent_readings), Decimal('0')),
        'reader_latest_sync': recent_readings[0].updated_at if recent_readings else None,
        'reader_rate_per_m3': system_settings.rate_per_m3,
    }


def _build_consumer_panel_context(request, payment_form=None):
    consumer = get_linked_consumer(request.user)
    system_settings = SystemSettings.load()
    current_billing, previous_billing = get_consumer_billing_comparison(consumer)
    paymongo_ready = system_settings.enable_online_payments and is_paymongo_configured()

    context = {
        **_build_profile_context(request.user),
        'consumer': consumer,
        'payment_form': payment_form,
        'billing_records': get_consumer_monthly_billings(consumer, limit=10) if consumer else [],
        'payments': (
            consumer.payments.exclude(status=Payment.Statuses.COMPLETED).select_related('billing')[:10]
            if consumer
            else Payment.objects.none()
        ),
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
        'consumer_chart_data': build_consumer_chart_data(consumer),
        'consumer_paymongo_ready': paymongo_ready,
    }

    if consumer and context['payment_form'] is None and paymongo_ready:
        context['payment_form'] = ConsumerPaymentForm(consumer=consumer, system_settings=system_settings)
    context['billing_balance_data'] = [
        {
            'month': billing.billing_month.strftime('%Y-%m'),
            'balance': str(billing.amount_due),
            'total': str(billing.total_amount),
        }
        for billing in get_consumer_monthly_billings(consumer) if consumer
    ]

    return context


def _get_paymongo_payment_for_request(request, payment_id):
    payments = Payment.objects.select_related('consumer', 'billing')
    role = get_user_role(request.user, create=True)

    if role == ConsumerProfile.Roles.CONSUMER:
        consumer = get_linked_consumer(request.user)
        if consumer is None:
            return None
        return payments.filter(pk=payment_id, consumer=consumer).first()

    if role in PAYMENT_MANAGER_ROLES:
        return payments.filter(pk=payment_id).first()

    return None


def _paymongo_home_url_for_request(request):
    role = get_user_role(request.user, create=True)
    if role in PAYMENT_MANAGER_ROLES:
        return reverse('payments')
    return reverse('consumer_panel')


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
        'billing_rows_html': render_to_string('billing/includes/reader_billing_rows.html', context, request=request),
        'form_html': render_to_string('billing/includes/reader_reading_form.html', context, request=request),
        'edit_form_html': render_to_string('billing/includes/reader_edit_form.html', context, request=request),
        'total_readings': context['reader_total_readings'],
        'total_billings': context['reader_total_billings'],
        'total_usage': str(context['reader_total_usage']),
        'latest_sync': context['reader_latest_sync'].strftime('%Y-%m-%d %H:%M') if context['reader_latest_sync'] else '-',
    }


def _render_secretary_live_payload(request, context):
    return {
        'ok': True,
        'settlement_rows_html': render_to_string('billing/includes/settlement_account_rows.html', context, request=request),
        'payment_rows_html': render_to_string('billing/includes/secretary_payment_rows.html', context, request=request),
        'reading_rows_html': render_to_string('billing/includes/secretary_reading_rows.html', context, request=request),
        'billing_rows_html': render_to_string('billing/includes/secretary_billing_rows.html', context, request=request),
        'total_billed': _format_money(context['total_billed']),
        'total_collected': _format_money(context['total_collected']),
        'total_usage': str(context['total_usage']),
        'pending_accounts_count': context['pending_accounts_count'],
        'overdue_accounts_count': context['overdue_accounts_count'],
        'pending_payments': context['pending_payments'],
        'failed_payments': context['failed_payments'],
    }


def _render_treasurer_live_payload(request, context):
    return {
        'ok': True,
        'monthly_collected': _format_money(context['monthly_collected']),
        'pending_accounts_count': context['pending_accounts_count'],
        'pending_payment_requests_count': context['pending_payment_requests_count'],
        'overdue_accounts_count': context['overdue_accounts_count'],
        'pending_account_rows_html': render_to_string('billing/includes/settlement_account_rows.html', context, request=request),
        'pending_request_rows_html': render_to_string('billing/includes/treasurer_pending_payment_rows.html', context, request=request),
        'receipt_rows_html': render_to_string('billing/includes/treasurer_receipt_rows.html', context, request=request),
        'overdue_rows_html': render_to_string('billing/includes/treasurer_overdue_rows.html', context, request=request),
    }


def _render_admin_live_payload(request, context):
    return {
        'ok': True,
        'total_consumers': context['total_consumers'],
        'active_connections': context['active_connections'],
        'pending_payments': context['pending_payments'],
        'overdue_bills': context['overdue_bills'],
        'selected_month_label': context['selected_month'].strftime('%B %Y'),
        'monthly_billed': _format_money(context['monthly_billed']),
        'monthly_collected': _format_money(context['monthly_collected']),
        'monitoring_html': render_to_string('billing/includes/admin_monitoring_section.html', context, request=request),
        'billing_rows_html': render_to_string('billing/includes/admin_billing_rows.html', context, request=request),
        'payment_rows_html': render_to_string('billing/includes/admin_payment_rows.html', context, request=request),
        'reading_rows_html': render_to_string('billing/includes/admin_reading_rows.html', context, request=request),
    }


def _render_consumer_live_payload(request, context, message=''):
    return {
        'ok': True,
        'message': message,
        'summary_html': render_to_string('billing/includes/profile_summary.html', context, request=request),
        'charts_html': render_to_string('billing/includes/consumer_usage_charts.html', context, request=request),
        'comparison_html': render_to_string('billing/includes/consumer_billing_comparison.html', context, request=request),
        'receipt_rows_html': render_to_string('billing/includes/consumer_receipt_rows.html', context, request=request),
        'billing_rows_html': render_to_string('billing/includes/consumer_billing_rows.html', context, request=request),
        'reading_rows_html': render_to_string('billing/includes/consumer_reading_rows.html', context, request=request),
        'payment_rows_html': render_to_string('billing/includes/consumer_payment_rows.html', context, request=request),
    }


def _build_treasurer_panel_context():
    current_month = timezone.localdate().replace(day=1)
    completed_payments = Payment.objects.filter(
        status=Payment.Statuses.COMPLETED,
        **month_filter_kwargs('payment_date', current_month),
    ).select_related('consumer', 'billing')
    pending_payment_requests = list(
        Payment.objects.filter(status=Payment.Statuses.PENDING).select_related('consumer', 'billing')[:12]
    )
    for payment in pending_payment_requests:
        if payment.billing_id and payment.billing:
            apply_panel_billing_status(payment.billing)
    settlement_snapshot = build_settlement_snapshot()
    recent_receipts = list(
        Payment.objects.filter(status=Payment.Statuses.COMPLETED).select_related('consumer', 'billing')[:20]
    )
    for payment in recent_receipts:
        if payment.billing_id and payment.billing:
            apply_panel_billing_status(payment.billing)

    return {
        'current_month': current_month,
        'monthly_collected': completed_payments.aggregate(total=Sum('amount_paid'))['total'] or Decimal('0'),
        'pending_accounts_count': settlement_snapshot['pending_count'],
        'overdue_accounts_count': settlement_snapshot['overdue_count'],
        'pending_payment_requests_count': Payment.objects.filter(status=Payment.Statuses.PENDING).count(),
        'settlement_accounts': settlement_snapshot['pending_records'][:20],
        'overdue_bills': settlement_snapshot['overdue_records'][:10],
        'pending_payment_requests': pending_payment_requests,
        'recent_receipts': recent_receipts,
        'large_payments': Payment.objects.filter(amount_paid__gte=Decimal('10000')).select_related('consumer', 'billing')[:10],
        'recent_audit_logs': AuditLog.objects.select_related('user').filter(
            role__in=[ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER],
        )[:8],
        'hide_header': True,
        'payment_status_choices': Payment.Statuses.choices,
    }


def home(request):
    total_consumers = Consumer.objects.count()
    billing_records = get_preferred_billing_records(BillingRecord.objects.all())
    total_billed = sum((billing.total_amount for billing in billing_records), Decimal('0'))
    total_paid = sum((billing.amount_paid for billing in billing_records), Decimal('0'))
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
@require_POST
def logout_view(request):
    logout(request)
    return render(request, 'registration/logged_out.html')


@login_required
def dashboard(request):
    ensure_user_profile(request.user)
    return redirect(get_dashboard_url_for_user(request.user))


@role_required(ConsumerProfile.Roles.ADMIN)
def admin_panel(request):
    context = _build_admin_panel_context(get_selected_month(request.GET.get('month')))
    return render(request, 'billing/admin_dashboard.html', context)


@role_required(ConsumerProfile.Roles.ADMIN)
def admin_panel_data(request):
    context = _build_admin_panel_context(get_selected_month(request.GET.get('month')))
    return JsonResponse(_render_admin_live_payload(request, context))


@role_required(ConsumerProfile.Roles.SECRETARY)
def secretary_panel(request):
    selected_month = get_selected_month(request.GET.get('month'))
    context = {
        **_build_monthly_statement_context(selected_month),
        'selected_month': selected_month,
        'recent_audit_logs': AuditLog.objects.select_related('user').filter(
            role__in=[ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER],
        )[:8],
        'hide_header': True,
    }
    return render(request, 'billing/secretary_dashboard.html', context)


@role_required(ConsumerProfile.Roles.SECRETARY)
def secretary_panel_data(request):
    selected_month = get_selected_month(request.GET.get('month'))
    context = {
        **_build_monthly_statement_context(selected_month),
        'selected_month': selected_month,
    }
    return JsonResponse(_render_secretary_live_payload(request, context))


@role_required(ConsumerProfile.Roles.TREASURER)
def treasurer_panel(request):
    return render(request, 'billing/treasurer_dashboard.html', _build_treasurer_panel_context())


@role_required(ConsumerProfile.Roles.TREASURER)
def treasurer_panel_data(request):
    return JsonResponse(_render_treasurer_live_payload(request, _build_treasurer_panel_context()))


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.READER)
def reader_panel(request):
    if request.method == 'POST':
        form = MeterReadingForm(request.POST)
        if form.is_valid():
            reading, billing = handle_meter_reading_submission(form, request.user)
            messages.success(
                request,
                (
                    f'Meter reading saved for {reading.consumer.full_name}. '
                    f'Billing for {billing.billing_month:%B %Y} is PHP {billing.total_amount} due on {billing.due_date:%B %d, %Y}.'
                ),
            )
            return redirect('reader_panel')

        context = _build_reader_panel_context(request, form=form)
        context['hide_header'] = True
        return render(request, 'billing/reader_dashboard.html', context)

    context = _build_reader_panel_context(request)
    context['hide_header'] = True
    return render(request, 'billing/reader_dashboard.html', context)


@role_required(ConsumerProfile.Roles.CONSUMER)
def consumer_panel(request):
    consumer = get_linked_consumer(request.user)
    system_settings = SystemSettings.load()
    payment_form = None
    paymongo_ready = system_settings.enable_online_payments and is_paymongo_configured()

    if consumer:
        if request.method == 'POST':
            if not paymongo_ready:
                messages.error(request, 'Online payments are currently unavailable. Please contact the billing office.')
                return redirect('consumer_panel')
            payment_form = ConsumerPaymentForm(request.POST, consumer=consumer, system_settings=system_settings)
            if payment_form.is_valid():
                payment = payment_form.save()
                try:
                    ewallet_payment = create_paymongo_ewallet_payment(
                        payment,
                        request.build_absolute_uri(reverse('paymongo_success', args=[payment.id])),
                        request.build_absolute_uri(reverse('paymongo_cancel', args=[payment.id])),
                        payment_form.cleaned_data.get('online_wallet'),
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
                    {ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER},
                    'New online payment started by consumer',
                    (
                        f'{consumer.full_name} started a {payment.display_payment_method} PayMongo payment of '
                        f'PHP {payment.amount_paid}.'
                    ),
                    Notification.Types.PAYMENT,
                    consumer=consumer,
                    payment=payment,
                    billing=payment.billing,
                )
                return redirect(payment.gateway_redirect_url or ewallet_payment['redirect_url'])
        else:
            if paymongo_ready:
                payment_form = ConsumerPaymentForm(consumer=consumer, system_settings=system_settings)

    context = _build_consumer_panel_context(request, payment_form=payment_form)
    context['hide_header'] = True
    return render(request, 'billing/consumer_dashboard.html', context)


@role_required(ConsumerProfile.Roles.CONSUMER)
def account_center(request):
    consumer = get_linked_consumer(request.user)
    context = _build_consumer_panel_context(request)
    context.update(
        {
            'consumer': consumer,
            'system_version': 'v1.26',
            'system_year': '2026',
            'creator_name': 'Omale J Ohn',
        }
    )
    return render(request, 'billing/account_center.html', context)


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
        if get_user_role(request.user) in {ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER}:
            log_audit_action(request.user, 'Updated profile details', target=profile.full_name)
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
                f'Billing for {billing.billing_month:%B %Y} is PHP {billing.total_amount} due on {billing.due_date:%B %d, %Y}.'
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
    system_settings = SystemSettings.load()
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
            'rate_per_m3': str(system_settings.rate_per_m3),
            'due_date': (reading_date + timedelta(days=system_settings.billing_due_days)).strftime('%B %d, %Y'),
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


@login_required
def paymongo_success(request, payment_id):
    payment = _get_paymongo_payment_for_request(request, payment_id)
    if payment is None:
        messages.error(request, 'You do not have permission to access that PayMongo payment.')
        return redirect(get_dashboard_url_for_user(request.user))

    return render(
        request,
        'billing/payment_processing.html',
        {
            'payment': payment,
            'verify_url': reverse('paymongo_verify', args=[payment.id]),
            'receipt_url': reverse('payment_receipt', args=[payment.id]),
            'home_url': _paymongo_home_url_for_request(request),
        },
    )


@login_required
def paymongo_verify(request, payment_id):
    payment = _get_paymongo_payment_for_request(request, payment_id)
    if payment is None:
        return JsonResponse({'ok': False, 'message': 'You do not have permission to verify that PayMongo payment.'}, status=403)

    if payment.status == Payment.Statuses.COMPLETED:
        return JsonResponse(
            {
                'ok': True,
                'status': payment.status,
                'message': 'Payment completed. Your receipt is ready.',
                'receipt_url': reverse('payment_receipt', args=[payment.id]),
            }
        )

    if payment.payment_method != Payment.Methods.ONLINE:
        return JsonResponse({'ok': False, 'message': 'That payment is not an online PayMongo transaction.'}, status=400)
    gateway_reference = payment.gateway_reference or payment.reference_number
    if not gateway_reference:
        return JsonResponse({'ok': False, 'message': 'No PayMongo payment reference is linked to this payment.'}, status=400)

    system_settings = SystemSettings.load()
    try:
        payment_intent = retrieve_paymongo_payment_intent(gateway_reference)
        _store_paymongo_gateway_result(payment, payment_intent)
    except Exception as exc:
        return JsonResponse({'ok': False, 'message': f'Unable to verify PayMongo payment: {exc}'}, status=502)

    if paymongo_intent_is_paid(payment_intent):
        update_payment_status(payment, Payment.Statuses.COMPLETED, system_settings=system_settings)
        payment.refresh_from_db()
        if payment.billing_id:
            payment.billing.refresh_from_db()
        return JsonResponse(
            {
                'ok': True,
                'status': payment.status,
                'message': 'Payment completed. Your receipt has been sent by email and SMS.',
                'receipt_url': reverse('payment_receipt', args=[payment.id]),
            }
        )
    elif payment.gateway_status in {'awaiting_payment_method', 'failed', 'canceled', 'cancelled'}:
        update_payment_status(payment, Payment.Statuses.FAILED, system_settings=system_settings)
        return JsonResponse({'ok': False, 'message': 'PayMongo returned the transaction as failed or cancelled.'}, status=400)
    return JsonResponse(
        {
            'ok': False,
            'pending': True,
            'message': 'PayMongo returned, but the payment is still pending verification. Please try again shortly.',
        },
        status=202,
    )


@login_required
def paymongo_cancel(request, payment_id):
    payment = _get_paymongo_payment_for_request(request, payment_id)
    if payment is None:
        messages.error(request, 'You do not have permission to access that PayMongo payment.')
        return redirect(get_dashboard_url_for_user(request.user))

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
    return redirect(_paymongo_home_url_for_request(request))


@login_required
def payment_receipt_view(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related('consumer', 'billing', 'consumer__profile'), pk=payment_id)
    role = get_user_role(request.user, create=True)
    linked_consumer = get_linked_consumer(request.user)
    can_view = role in {
        ConsumerProfile.Roles.ADMIN,
        ConsumerProfile.Roles.SECRETARY,
        ConsumerProfile.Roles.TREASURER,
    } or (linked_consumer and linked_consumer.id == payment.consumer_id)
    if not can_view:
        messages.error(request, 'You do not have permission to access that receipt.')
        return redirect(get_dashboard_url_for_user(request.user))
    context = _build_receipt_context(payment)
    context['show_account_center'] = role == ConsumerProfile.Roles.CONSUMER
    context['print_on_load'] = request.GET.get('print') in {'1', 'true', 'yes'}
    return render(request, 'billing/payment_receipt.html', context)


@role_required(ConsumerProfile.Roles.ADMIN)
def consumer_list(request):
    query = request.GET.get('q', '').strip()
    consumers = Consumer.objects.select_related('profile', 'profile__user').all()
    if query:
        consumers = consumers.filter(
            Q(full_name__icontains=query) | Q(address__icontains=query) | Q(contact_number__icontains=query)
        )
    return render(request, 'billing/consumers.html', {'consumers': consumers, 'query': query, 'hide_header': True})


@role_required(ConsumerProfile.Roles.ADMIN)
def add_consumer(request):
    consumer_form = ConsumerForm()
    portal_account_form = PortalAccountForm()

    if request.method == 'POST':
        action = request.POST.get('action', 'create_consumer')
        if action == 'create_portal_account':
            portal_account_form = PortalAccountForm(request.POST, request.FILES)
            if portal_account_form.is_valid():
                account = portal_account_form.save()
                profile = ConsumerProfile.objects.get(user=account)
                messages.success(
                    request,
                    f'{profile.get_role_display()} account "{account.username}" created successfully.',
                )
                return redirect('consumers')
        else:
            consumer_form = ConsumerForm(request.POST, request.FILES)
            if consumer_form.is_valid():
                consumer_form.save()
                messages.success(request, 'Consumer record added successfully.')
                return redirect('consumers')

    return render(
        request,
        'billing/add_consumer.html',
        {
            'form': consumer_form,
            'portal_account_form': portal_account_form,
            'page_title': 'Add Account',
        },
    )


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
            'billings': get_preferred_billing_records(billings),
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
    selected_consumer = request.GET.get('consumer', '').strip()

    if request.method == 'POST':
        if not can_edit:
            messages.error(request, 'Only admin and treasurer accounts can record payments.')
            return redirect('payments')

        form = AdminPaymentForm(request.POST)
        if form.is_valid():
            payment = form.save(commit=False)
            if payment.payment_method == Payment.Methods.ONLINE:
                if not is_paymongo_configured():
                    messages.error(request, 'PayMongo is not configured. Configure the payment gateway before starting online checkout.')
                    return redirect('payments')

                payment.status = Payment.Statuses.PENDING
                payment.reference_number = ''
                payment.save()

                try:
                    ewallet_payment = create_paymongo_ewallet_payment(
                        payment,
                        request.build_absolute_uri(reverse('paymongo_success', args=[payment.id])),
                        request.build_absolute_uri(reverse('paymongo_cancel', args=[payment.id])),
                        form.cleaned_data.get('online_channel'),
                    )
                    _store_paymongo_gateway_start(payment, ewallet_payment)
                except Exception as exc:
                    update_payment_status(payment, Payment.Statuses.FAILED, system_settings=SystemSettings.load())
                    messages.error(request, f'Online payment could not be started: {exc}')
                    return redirect('payments')

                log_audit_action(
                    request.user,
                    'Started online payment checkout',
                    target=payment.consumer.full_name,
                    details=(
                        f'Payment #{payment.id} for PHP {payment.amount_paid} started via '
                        f'{payment.display_payment_method}. Reference {payment.display_reference_number}.'
                    ),
                )
                messages.success(
                    request,
                    (
                        f'Online checkout started for {payment.consumer.full_name}. '
                        f'Reference: {payment.display_reference_number}.'
                    ),
                )
                return redirect(payment.gateway_redirect_url or ewallet_payment['redirect_url'])

            payment.save()
            send_payment_notification(payment)
            log_audit_action(
                request.user,
                'Recorded payment',
                target=payment.consumer.full_name,
                details=f'Payment #{payment.id} for PHP {payment.amount_paid} marked {payment.get_status_display()}.',
            )
            messages.success(request, 'Payment saved successfully and the consumer was notified.')
            return redirect('payments')
    else:
        form = AdminPaymentForm(
            initial={
                'payment_date': timezone.localdate(),
                'status': Payment.Statuses.COMPLETED,
                'consumer': selected_consumer or None,
            }
        ) if can_edit else None

    payments = Payment.objects.select_related('consumer', 'billing')
    if selected_consumer:
        payments = payments.filter(consumer_id=selected_consumer)
    billing_records = get_preferred_billing_records(BillingRecord.objects.select_related('consumer'))
    if selected_consumer:
        billing_records = [billing for billing in billing_records if str(billing.consumer_id) == selected_consumer]
    billing_balance_data = [
        {
            'id': billing.id,
            'consumer_id': billing.consumer_id,
            'balance': str(billing.amount_due),
            'total': str(billing.total_amount),
            'month': billing.billing_month.strftime('%B %Y'),
        }
        for billing in billing_records
    ]
    return render(
        request,
        'billing/payments.html',
        {
            'payments': payments,
            'form': form,
            'can_edit': can_edit,
            'can_notify_payment': user_role == ConsumerProfile.Roles.ADMIN,
            'payment_status_choices': Payment.Statuses.choices,
            'billing_balance_data': billing_balance_data,
            'selected_consumer': selected_consumer,
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
    log_audit_action(
        request.user,
        'Updated payment status',
        target=payment.consumer.full_name,
        details=f'Payment #{payment.id}: {previous_status} to {new_status}.',
    )
    notify_roles(
        {ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER},
        'Payment status updated',
        f'{request.user.username} updated payment #{payment.id} for {payment.consumer.full_name} from {previous_status} to {new_status}.',
        Notification.Types.PAYMENT,
        consumer=payment.consumer,
        payment=payment,
        billing=payment.billing,
    )

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
    selected_consumer = request.GET.get('consumer', '').strip()
    selected_consumer_record = get_object_or_404(Consumer, pk=selected_consumer) if selected_consumer else None
    default_start_date = selected_month
    default_end_date = selected_month.replace(day=28) + timedelta(days=4)
    default_end_date = default_end_date - timedelta(days=default_end_date.day)
    soa_start_date = get_selected_date(request.GET.get('start_date'), default_start_date)
    soa_end_date = get_selected_date(request.GET.get('end_date'), default_end_date)
    statement_context = _build_monthly_statement_context(selected_month, consumer=selected_consumer_record)

    total_billed = statement_context['total_billed']
    total_collected = statement_context['total_collected']
    total_usage = statement_context['total_usage']
    total_consumers = Consumer.objects.count()
    paid_bills = statement_context['paid_bills']
    total_bills = len(statement_context['statement_billings'])
    overdue_bills = sum(1 for billing in statement_context['statement_billings'] if _has_unpaid_overdue_balance(billing))

    context = {
        **statement_context,
        'selected_month': selected_month,
        'active_percent': round((Consumer.objects.filter(status=Consumer.Statuses.ACTIVE).count() / total_consumers) * 100)
        if total_consumers
        else 0,
        'paid_percent': round((paid_bills / total_bills) * 100) if total_bills else 0,
        'pending_percent': round((overdue_bills / total_bills) * 100) if total_bills else 0,
        'collection_rate': round((total_collected / total_billed) * 100) if total_billed else 0,
        'total_consumers': total_consumers,
        'total_bills': total_bills,
        'overdue_bills': overdue_bills,
        'total_collected': total_collected,
        'total_billed': total_billed,
        'total_usage': total_usage,
        'recent_payments': statement_context['statement_payments'][:10],
        'recent_readings': statement_context['statement_readings'][:10],
        'consumers': Consumer.objects.order_by('full_name'),
        'selected_consumer': selected_consumer,
        'soa_start_date': soa_start_date,
        'soa_end_date': soa_end_date,
    }
    return render(request, 'billing/reports.html', context)


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY, ConsumerProfile.Roles.TREASURER)
def reports_export_view(request):
    selected_month = get_selected_month(request.GET.get('month'))
    default_start_date = selected_month
    default_end_date = selected_month.replace(day=28) + timedelta(days=4)
    default_end_date = default_end_date - timedelta(days=default_end_date.day)
    start_date = get_selected_date(request.GET.get('start_date'), default_start_date)
    end_date = get_selected_date(request.GET.get('end_date'), default_end_date)
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    consumer_id = request.GET.get('consumer', '').strip()
    consumer = get_object_or_404(Consumer, pk=consumer_id) if consumer_id else None
    pdf_file = _build_soa_pdf(selected_month, consumer=consumer, start_date=start_date, end_date=end_date)
    response = HttpResponse(pdf_file.getvalue(), content_type='application/pdf')
    target = consumer.full_name if consumer else 'all-consumers'
    response['Content-Disposition'] = (
        f'attachment; filename="statement-of-account-{target}-{start_date:%Y-%m-%d}-to-{end_date:%Y-%m-%d}.pdf"'
    )
    log_audit_action(
        request.user,
        'Generated statement of account PDF',
        target=f'{target} {start_date:%Y-%m-%d} to {end_date:%Y-%m-%d}',
    )
    return response


@role_required(ConsumerProfile.Roles.ADMIN, ConsumerProfile.Roles.SECRETARY)
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
                log_audit_action(
                    request.user,
                    'Sent SMS blast',
                    target=blast.get_audience_display(),
                    details=f'Sent {blast.sent_count}; failed {blast.failed_count}.',
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
                log_audit_action(
                    request.user,
                    'Sent email blast',
                    target=email_form.cleaned_data['audience'],
                    details=f'Sent {result["sent"]}; failed {result["failed"]}.',
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
                log_audit_action(
                    request.user,
                    'Sent SMS test',
                    target=test_sms_form.cleaned_data['phone_number'],
                    details=result.get_status_display(),
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
                log_audit_action(
                    request.user,
                    'Sent email test',
                    target=test_email_form.cleaned_data['email'],
                    details=result.get_status_display(),
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
            settings_obj = form.save()
            updated_billings = sync_existing_billings_with_settings(settings_obj)
            log_audit_action(
                request.user,
                'Updated payment and notification settings',
                target='SystemSettings',
                details=f'Recalculated {updated_billings} billing record(s) after the settings change.',
            )
            messages.success(
                request,
                (
                    'Payment and notification settings updated successfully. '
                    f'{updated_billings} billing record(s) were recalculated across all panels.'
                ),
            )
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
