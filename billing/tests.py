from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import BillingRecordForm, MeterReadingForm, SignUpForm
from .models import BillingRecord, Consumer, ConsumerProfile, MeterReading, Payment
from .services import build_consumer_chart_data, build_consumer_payment_month_choices
from .views import _build_soa_summary, _build_soa_transactions


class SignUpFormTests(TestCase):
    def test_rejects_password_mismatch(self):
        form = SignUpForm(
            data={
                'username': 'maria',
                'full_name': 'Maria Cruz',
                'password1': 'StrongPass1!',
                'password2': 'WrongPass1!',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('Passwords do not match. Please try again.', form.errors['password2'])

    def test_rejects_username_inside_password(self):
        form = SignUpForm(
            data={
                'username': 'maria',
                'full_name': 'Maria Cruz',
                'password1': 'MariaPass1!',
                'password2': 'MariaPass1!',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('Your username cannot be used as your password.', form.errors['password1'])


class ConsumerPaymentChoiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='consumer1', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Consumer One',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.profile,
            full_name='Consumer One',
            status=Consumer.Statuses.ACTIVE,
        )

    def test_next_payment_choice_moves_to_following_month_after_paid_bill(self):
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=billing.total_amount,
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
        )

        choices = build_consumer_payment_month_choices(self.consumer)

        self.assertTrue(choices)
        self.assertEqual(choices[0][0], '2026-05')


class MeterReadingFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reader1', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Reader Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.profile,
            full_name='Reader Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 3, 31),
            previous_reading=Decimal('0'),
            current_reading=Decimal('100'),
        )

    def test_allows_lower_than_previous_reading(self):
        form = MeterReadingForm(
            data={
                'consumer': self.consumer.id,
                'reading_date': '2026-04-30',
                'current_reading': '90',
                'notes': '',
            }
        )

        self.assertTrue(form.is_valid())

    def test_allows_large_reading_jump(self):
        form = MeterReadingForm(
            data={
                'consumer': self.consumer.id,
                'reading_date': '2026-04-30',
                'current_reading': '700',
                'notes': '',
            }
        )

        self.assertTrue(form.is_valid())

    def test_lower_current_input_is_used_as_billable_usage(self):
        reading = MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 4, 30),
            previous_reading=Decimal('100'),
            current_reading=Decimal('15'),
        )
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('100'),
            current_reading=Decimal('15'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 30),
            due_date=date(2026, 5, 15),
        )

        self.assertEqual(reading.usage_m3, Decimal('15'))
        self.assertEqual(billing.usage_m3, Decimal('15'))
        self.assertEqual(billing.total_amount, Decimal('300'))


class BillingRecordFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='billingadmin', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Billing Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer = Consumer.objects.create(
            full_name='Billing Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )

    def test_rejects_duplicate_consumer_month(self):
        form = BillingRecordForm(
            data={
                'consumer': self.consumer.id,
                'billing_month': '2026-04-15',
                'previous_reading': '10',
                'current_reading': '15',
                'rate_per_m3': '20',
                'amount_paid': '0',
                'status': BillingRecord.Statuses.PENDING,
                'billing_date': '2026-04-15',
                'due_date': '2026-04-30',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('A billing record for this consumer and month already exists.', form.errors['billing_month'])


class ProfileTransactionVisibilityTests(TestCase):
    def setUp(self):
        self.consumer_user = User.objects.create_user(username='consumer1', password='StrongPass1!')
        self.consumer_profile = ConsumerProfile.objects.create(
            user=self.consumer_user,
            full_name='Consumer One',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.consumer_profile,
            full_name='Consumer One',
            status=Consumer.Statuses.ACTIVE,
        )
        self.billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        Payment.objects.create(
            consumer=self.consumer,
            billing=self.billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('50'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
        )
        MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 4, 30),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
        )

    def test_secretary_profile_shows_all_consumer_transactions(self):
        secretary = User.objects.create_user(username='secretary1', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=secretary,
            full_name='Secretary One',
            role=ConsumerProfile.Roles.SECRETARY,
        )

        self.client.force_login(secretary)
        response = self.client.get(reverse('profile'))

        self.assertContains(response, 'Recent Consumer Billing')
        self.assertContains(response, 'Recent Consumer Payments')
        self.assertContains(response, 'Recent Consumer Meter Readings')
        self.assertContains(response, 'Consumer One')
        self.assertNotContains(response, 'No billing records available.')
        self.assertNotContains(response, 'No payments available.')
        self.assertNotContains(response, 'No meter readings available.')

    def test_treasurer_profile_shows_all_consumer_transactions(self):
        treasurer = User.objects.create_user(username='treasurer1', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=treasurer,
            full_name='Treasurer One',
            role=ConsumerProfile.Roles.TREASURER,
        )

        self.client.force_login(treasurer)
        response = self.client.get(reverse('profile'))

        self.assertContains(response, 'Recent Consumer Billing')
        self.assertContains(response, 'Consumer One')
        self.assertNotContains(response, 'No billing records available.')


class StaffPanelPendingMonitoringTests(TestCase):
    def setUp(self):
        today = timezone.localdate()
        billing_month = today.replace(day=1)

        self.secretary = User.objects.create_user(username='secretary-panel', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.secretary,
            full_name='Secretary Panel',
            role=ConsumerProfile.Roles.SECRETARY,
        )

        self.treasurer = User.objects.create_user(username='treasurer-panel', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.treasurer,
            full_name='Treasurer Panel',
            role=ConsumerProfile.Roles.TREASURER,
        )

        pending_user = User.objects.create_user(username='pending-consumer', password='StrongPass1!')
        pending_profile = ConsumerProfile.objects.create(
            user=pending_user,
            full_name='Pending Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.pending_consumer = Consumer.objects.create(
            profile=pending_profile,
            full_name='Pending Consumer',
            status=Consumer.Statuses.ACTIVE,
        )

        overdue_user = User.objects.create_user(username='overdue-consumer', password='StrongPass1!')
        overdue_profile = ConsumerProfile.objects.create(
            user=overdue_user,
            full_name='Overdue Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.overdue_consumer = Consumer.objects.create(
            profile=overdue_profile,
            full_name='Overdue Consumer',
            status=Consumer.Statuses.ACTIVE,
        )

        self.pending_billing = BillingRecord.objects.create(
            consumer=self.pending_consumer,
            billing_month=billing_month,
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=billing_month,
            due_date=today + timedelta(days=7),
        )
        self.overdue_billing = BillingRecord.objects.create(
            consumer=self.overdue_consumer,
            billing_month=billing_month,
            previous_reading=Decimal('0'),
            current_reading=Decimal('12'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=billing_month,
            due_date=today - timedelta(days=7),
        )
        self.pending_payment = Payment.objects.create(
            consumer=self.pending_consumer,
            billing=self.pending_billing,
            covered_month=billing_month,
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('50'),
            payment_date=today,
            status=Payment.Statuses.PENDING,
            reference_number='TAB-REF-1001',
        )

    def test_secretary_panel_lists_accounts_requiring_settlement(self):
        self.client.force_login(self.secretary)

        response = self.client.get(reverse('secretary_panel'), {'month': timezone.localdate().strftime('%Y-%m')})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pending_accounts_count'], 1)
        self.assertEqual(response.context['overdue_accounts_count'], 1)
        self.assertContains(response, 'Accounts Needing Settlement')
        self.assertContains(response, 'Pending Consumer')
        self.assertContains(response, 'Overdue Consumer')

    def test_secretary_live_payload_reports_pending_and_overdue_counts(self):
        self.client.force_login(self.secretary)

        response = self.client.get(
            reverse('secretary_panel_data'),
            {'month': timezone.localdate().strftime('%Y-%m')},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['pending_accounts_count'], 1)
        self.assertEqual(payload['overdue_accounts_count'], 1)
        self.assertIn('Pending Consumer', payload['settlement_rows_html'])
        self.assertIn('Overdue Consumer', payload['settlement_rows_html'])

    def test_treasurer_panel_lists_pending_accounts_and_pending_transactions(self):
        self.client.force_login(self.treasurer)

        response = self.client.get(reverse('treasurer_panel'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['pending_accounts_count'], 1)
        self.assertEqual(response.context['pending_payment_requests_count'], 1)
        self.assertContains(response, 'Pending Payment Requests')
        self.assertContains(response, 'Pending Consumer')
        self.assertContains(response, 'Pending Transaction Verification')
        self.assertContains(response, 'TAB-REF-1001')

    def test_treasurer_live_payload_refreshes_pending_sections(self):
        self.client.force_login(self.treasurer)

        response = self.client.get(reverse('treasurer_panel_data'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['pending_accounts_count'], 1)
        self.assertEqual(payload['pending_payment_requests_count'], 1)
        self.assertEqual(payload['overdue_accounts_count'], 1)
        self.assertIn('Pending Consumer', payload['pending_account_rows_html'])
        self.assertIn('TAB-REF-1001', payload['pending_request_rows_html'])
        self.assertIn('Overdue Consumer', payload['overdue_rows_html'])


class ReportIntegrityTests(TestCase):
    def setUp(self):
        self.consumer_user = User.objects.create_user(username='consumer2', password='StrongPass1!')
        self.consumer_profile = ConsumerProfile.objects.create(
            user=self.consumer_user,
            full_name='Consumer Two',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.consumer_profile,
            full_name='Consumer Two',
            status=Consumer.Statuses.ACTIVE,
        )
        self.treasurer = User.objects.create_user(username='treasurer2', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.treasurer,
            full_name='Treasurer Two',
            role=ConsumerProfile.Roles.TREASURER,
        )

    def test_reports_do_not_count_zero_due_bill_as_overdue_account(self):
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('0'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('400'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )

        self.assertEqual(billing.amount_due, Decimal('0'))
        self.assertNotEqual(billing.status, BillingRecord.Statuses.OVERDUE)

        self.client.force_login(self.treasurer)
        response = self.client.get(reverse('reports'), {'month': '2026-04'})

        self.assertEqual(response.context['overdue_bills'], 0)

    def test_soa_uses_covered_month_and_only_completed_payments_as_credit(self):
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        completed = Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('50'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
        )
        pending = Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('100'),
            payment_date=date(2026, 4, 11),
            status=Payment.Statuses.PENDING,
        )
        failed = Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('25'),
            payment_date=date(2026, 4, 12),
            status=Payment.Statuses.FAILED,
        )
        may_payment = Payment.objects.create(
            consumer=self.consumer,
            covered_month=date(2026, 5, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('30'),
            payment_date=date(2026, 4, 13),
            status=Payment.Statuses.COMPLETED,
        )

        _, _, transactions = _build_soa_transactions(date(2026, 4, 1), consumer=self.consumer)
        payment_rows = [item for item in transactions if isinstance(item['source'], Payment)]
        payment_ids = {item['source'].id for item in payment_rows}
        credits_by_id = {item['source'].id: item['credit'] for item in payment_rows}

        self.assertIn(completed.id, payment_ids)
        self.assertIn(pending.id, payment_ids)
        self.assertIn(failed.id, payment_ids)
        self.assertNotIn(may_payment.id, payment_ids)
        self.assertEqual(credits_by_id[completed.id], Decimal('50'))
        self.assertEqual(credits_by_id[pending.id], Decimal('0'))
        self.assertEqual(credits_by_id[failed.id], Decimal('0'))

    def test_soa_date_range_uses_actual_transaction_dates(self):
        inside_billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 10),
            due_date=date(2026, 4, 25),
        )
        outside_billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('12'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 20),
            due_date=date(2026, 5, 5),
        )
        inside_payment = Payment.objects.create(
            consumer=self.consumer,
            billing=inside_billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('50'),
            payment_date=date(2026, 4, 12),
            status=Payment.Statuses.COMPLETED,
        )
        outside_payment = Payment.objects.create(
            consumer=self.consumer,
            billing=inside_billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('75'),
            payment_date=date(2026, 4, 20),
            status=Payment.Statuses.COMPLETED,
        )

        _, _, transactions = _build_soa_transactions(
            consumer=self.consumer,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 15),
        )
        sources = {(item['source'].__class__, item['source'].id) for item in transactions}

        self.assertIn((BillingRecord, inside_billing.id), sources)
        self.assertIn((Payment, inside_payment.id), sources)
        self.assertNotIn((BillingRecord, outside_billing.id), sources)
        self.assertNotIn((Payment, outside_payment.id), sources)

    def test_soa_summary_groups_pending_overdue_cash_and_online(self):
        overdue_billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        Payment.objects.create(
            consumer=self.consumer,
            billing=overdue_billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('50'),
            payment_date=date(2026, 4, 5),
            status=Payment.Statuses.COMPLETED,
        )
        Payment.objects.create(
            consumer=self.consumer,
            billing=overdue_billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.GCASH,
            amount_paid=Decimal('50'),
            payment_date=date(2026, 4, 6),
            status=Payment.Statuses.COMPLETED,
        )
        Payment.objects.create(
            consumer=self.consumer,
            billing=overdue_billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('75'),
            payment_date=date(2026, 4, 7),
            status=Payment.Statuses.PENDING,
        )

        billings, payments, _ = _build_soa_transactions(date(2026, 4, 1), consumer=self.consumer)
        summary = _build_soa_summary(billings, payments)

        self.assertEqual(summary['pending_payments_count'], 1)
        self.assertEqual(summary['pending_payments_total'], Decimal('75'))
        self.assertEqual(summary['overdue_accounts_count'], 1)
        self.assertEqual(summary['overdue_accounts_total'], Decimal('100'))
        self.assertEqual(summary['cash_payments_count'], 1)
        self.assertEqual(summary['cash_payments_total'], Decimal('50'))
        self.assertEqual(summary['online_payments_count'], 1)
        self.assertEqual(summary['online_payments_total'], Decimal('50'))

    def test_reports_use_statement_month_scope_for_payments(self):
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        payment = Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('80'),
            discount_amount=Decimal('20'),
            payment_date=date(2026, 5, 2),
            status=Payment.Statuses.COMPLETED,
        )

        self.client.force_login(self.treasurer)
        response = self.client.get(reverse('reports'), {'month': '2026-04'})

        self.assertEqual(response.context['total_collected'], Decimal('100'))
        self.assertIn(payment, response.context['recent_payments'])


class ConsumerChartDataTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='consumer-chart', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Chart Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.profile,
            full_name='Chart Consumer',
            status=Consumer.Statuses.ACTIVE,
        )

    def test_build_consumer_chart_data_returns_recent_series_and_status_counts(self):
        april = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('200'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        may = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 5, 1),
            previous_reading=Decimal('10'),
            current_reading=Decimal('18'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
        )

        april.refresh_from_db()
        may.refresh_from_db()
        chart_data = build_consumer_chart_data(self.consumer)

        self.assertEqual(len(chart_data['points']), 2)
        self.assertEqual(chart_data['points'][0]['label'], 'Apr 2026')
        self.assertEqual(chart_data['points'][1]['label'], 'May 2026')
        self.assertEqual(chart_data['status_counts']['paid'], 1)
        self.assertEqual(chart_data['status_counts']['pending'], 1)
        self.assertEqual(chart_data['summary']['paid_ratio'], 50)


class DashboardPanelRegressionTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='panel-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Panel Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )

        self.reader_user = User.objects.create_user(username='panel-reader', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.reader_user,
            full_name='Panel Reader',
            role=ConsumerProfile.Roles.READER,
        )

        self.consumer_user = User.objects.create_user(username='panel-consumer', password='StrongPass1!')
        consumer_profile = ConsumerProfile.objects.create(
            user=self.consumer_user,
            full_name='Panel Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=consumer_profile,
            full_name='Panel Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('15'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )

    def test_admin_dashboard_and_live_data_render_monitoring_section(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'System Billing Snapshot')

        live_response = self.client.get(
            reverse('admin_panel_data'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(live_response.status_code, 200)
        self.assertIn('monitoring_html', live_response.json())

    def test_reader_dashboard_and_live_data_no_longer_raise_name_error(self):
        self.client.force_login(self.reader_user)

        response = self.client.get(reverse('reader_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Recent Submitted Readings')

        live_response = self.client.get(
            reverse('reader_panel_data'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(live_response.status_code, 200)
        self.assertIn('rows_html', live_response.json())

    def test_consumer_dashboard_uses_performance_trend_layout(self):
        self.client.force_login(self.consumer_user)

        response = self.client.get(reverse('consumer_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Performance Trend')
        self.assertNotContains(response, 'Billing Status Snapshot')


class AuthAndBrandingRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='brand-consumer', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Brand Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.profile,
            full_name='Brand Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        self.billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('12'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        self.payment = Payment.objects.create(
            consumer=self.consumer,
            billing=self.billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('240'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
        )
        MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 4, 30),
            previous_reading=Decimal('0'),
            current_reading=Decimal('12'),
        )

    def test_login_page_uses_updated_auth_copy(self):
        response = self.client.get(reverse('login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Welcome Back')
        self.assertContains(response, 'Continue to System')
        self.assertContains(response, 'Tabuan Water Billing logo')

    def test_signup_page_uses_tabuan_logo_visual_panel(self):
        response = self.client.get(reverse('signup'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Create Consumer Account')
        self.assertContains(response, 'Tabuan Water Billing logo')

    def test_logout_page_renders_after_post(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse('logout'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You Have Been Logged Out')
        self.assertContains(response, 'Tabuan Waterbilling.')
        self.assertContains(response, 'Go to Login')

    def test_account_center_shows_reading_meter_history(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('account_center'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Reading Meter History')
        self.assertNotContains(response, 'Booking History')

    def test_receipt_uses_tabuan_logo_branding(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('payment_receipt', args=[self.payment.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Tabuan Water Billing System')
        self.assertContains(response, 'tabuan-logo.png')
