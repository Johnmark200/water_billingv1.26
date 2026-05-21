import socket
from pathlib import Path
from unittest.mock import MagicMock, patch
from datetime import date, timedelta
from decimal import Decimal
from urllib.error import URLError

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.admin.sites import site
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .forms import AdminPaymentForm, BillingRecordForm, ConsumerForm, ConsumerPaymentForm, MeterReadingForm, PortalAccountForm, SignUpForm
from .models import AreaRate, BillingRecord, Consumer, ConsumerProfile, DisconnectionRecord, MeetingMinutes, MeetingMinutesRevision, MeterReading, Notification, Payment, PaymentArrangement, SystemSettings
from .services import build_consumer_chart_data, build_consumer_payment_month_choices, create_payment_arrangement, get_consumer_outstanding_balance, get_effective_rate_per_m3, handle_meter_reading_submission, rebuild_consumer_payment_allocations, refresh_consumer_account_status, send_test_sms, sync_existing_billings_with_settings
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


class MediaServingRegressionTests(TestCase):
    def test_media_url_is_served_even_when_debug_is_false(self):
        media_dir = Path(settings.MEDIA_ROOT) / 'test-media'
        media_dir.mkdir(parents=True, exist_ok=True)
        sample_file = media_dir / 'profile-preview.txt'

        try:
            sample_file.write_text('profile image placeholder', encoding='utf-8')
            with override_settings(DEBUG=False):
                response = self.client.get(f'{settings.MEDIA_URL}test-media/profile-preview.txt')

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'profile image placeholder')
        finally:
            if sample_file.exists():
                sample_file.unlink()
            if media_dir.exists():
                try:
                    media_dir.rmdir()
                except OSError:
                    pass


class ConsumerFormTests(TestCase):
    def setUp(self):
        self.linked_user = User.objects.create_user(username='linked-user', password='StrongPass1!')
        self.linked_profile = ConsumerProfile.objects.create(
            user=self.linked_user,
            full_name='Linked Profile',
            contact='+639171234567',
            address='Original Address',
            role=ConsumerProfile.Roles.SECRETARY,
        )

    def test_exposes_unlinked_profiles_and_persists_selected_role(self):
        form = ConsumerForm(
            data={
                'profile': self.linked_profile.id,
                'linked_account_role': ConsumerProfile.Roles.CONSUMER,
                'full_name': 'Updated Consumer Name',
                'address': 'Updated Address',
                'contact_number': '+639181112222',
                'status': Consumer.Statuses.ACTIVE,
            }
        )

        self.assertIn(self.linked_profile, form.fields['profile'].queryset)
        self.assertTrue(form.is_valid(), form.errors)

        consumer = form.save()
        self.linked_profile.refresh_from_db()

        self.assertEqual(consumer.profile_id, self.linked_profile.id)
        self.assertEqual(self.linked_profile.role, ConsumerProfile.Roles.CONSUMER)
        self.assertEqual(self.linked_profile.full_name, 'Updated Consumer Name')
        self.assertEqual(self.linked_profile.address, 'Updated Address')
        self.assertEqual(self.linked_profile.contact, '+639181112222')


class MeterNumberAndAreaRateTests(TestCase):
    def test_consumer_receives_permanent_generated_meter_number(self):
        consumer = Consumer.objects.create(full_name='Maria Dela Cruz', status=Consumer.Statuses.ACTIVE)
        current_year = timezone.localdate().year

        self.assertRegex(consumer.meter_number, rf'^DELACRUZ-MTR-{current_year}\d{{6}}$')
        self.assertEqual(consumer.meter_number, f'DELACRUZ-MTR-{current_year}000001')

        next_consumer = Consumer.objects.create(full_name='Juan Santos', status=Consumer.Statuses.ACTIVE)
        self.assertEqual(next_consumer.meter_number, f'SANTOS-MTR-{current_year}000002')

        original_meter = consumer.meter_number
        consumer.full_name = 'Maria Santos'
        consumer.save(update_fields=['full_name'])
        consumer.refresh_from_db()

        self.assertEqual(consumer.meter_number, original_meter)

    def test_area_rate_resolution_and_sync_uses_most_specific_assignment(self):
        consumer = Consumer.objects.create(
            full_name='Rate Scoped Consumer',
            status=Consumer.Statuses.ACTIVE,
            barangay='Tabuan',
            village='Purok 1',
            household_code='Block 7',
        )
        billing = BillingRecord.objects.create(
            consumer=consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('50'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 30),
            due_date=date(2026, 5, 15),
        )
        AreaRate.objects.create(category=AreaRate.Categories.BARANGAY, location_name='Tabuan', rate_per_m3=Decimal('28'))
        AreaRate.objects.create(category=AreaRate.Categories.VILLAGE, location_name='Purok 1', rate_per_m3=Decimal('31'))
        AreaRate.objects.create(category=AreaRate.Categories.HOUSEHOLD, location_name='Block 7', rate_per_m3=Decimal('35'))

        self.assertEqual(get_effective_rate_per_m3(consumer), Decimal('35'))

        updated = sync_existing_billings_with_settings()
        billing.refresh_from_db()

        self.assertEqual(updated, 1)
        self.assertEqual(billing.rate_per_m3, Decimal('35'))
        self.assertEqual(billing.total_amount, Decimal('1750.00'))


class PortalAccountFormTests(TestCase):
    def test_creates_reader_account_with_credentials_and_profile(self):
        form = PortalAccountForm(
            data={
                'role': ConsumerProfile.Roles.READER,
                'username': 'reader-form',
                'full_name': 'Reader Form',
                'email': 'reader@example.com',
                'contact': '+639171234568',
                'address': 'Reader Street',
                'password1': 'StrongPass1!',
                'password2': 'StrongPass1!',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        profile = ConsumerProfile.objects.get(user=user)

        self.assertEqual(profile.role, ConsumerProfile.Roles.READER)
        self.assertEqual(profile.full_name, 'Reader Form')
        self.assertTrue(user.check_password('StrongPass1!'))

    def test_creates_admin_account_as_staff(self):
        form = PortalAccountForm(
            data={
                'role': ConsumerProfile.Roles.ADMIN,
                'username': 'admin-form',
                'full_name': 'Admin Form',
                'email': 'admin@example.com',
                'contact': '+639171234569',
                'address': 'Admin Street',
                'password1': 'StrongPass1!',
                'password2': 'StrongPass1!',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        profile = ConsumerProfile.objects.get(user=user)

        self.assertTrue(user.is_staff)
        self.assertEqual(profile.role, ConsumerProfile.Roles.ADMIN)


class AddConsumerPageAccountCreationTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='page-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Page Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )

    def test_admin_can_create_secretary_account_from_add_account_page(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('add_consumer'),
            data={
                'action': 'create_portal_account',
                'role': ConsumerProfile.Roles.SECRETARY,
                'username': 'secretary-page',
                'full_name': 'Secretary Page',
                'email': 'secretary@example.com',
                'contact': '+639171234570',
                'address': 'Office',
                'password1': 'StrongPass1!',
                'password2': 'StrongPass1!',
            },
        )

        self.assertEqual(response.status_code, 302)
        created_user = User.objects.get(username='secretary-page')
        created_profile = ConsumerProfile.objects.get(user=created_user)
        self.assertEqual(created_profile.role, ConsumerProfile.Roles.SECRETARY)


class StaffAccountStatusManagementTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='status-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Status Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.secretary_user = User.objects.create_user(username='status-secretary', password='StrongPass1!')
        self.secretary_profile = ConsumerProfile.objects.create(
            user=self.secretary_user,
            full_name='Status Secretary',
            email='secretary-status@example.com',
            contact='+639171234580',
            role=ConsumerProfile.Roles.SECRETARY,
        )

    def test_admin_panel_lists_staff_accounts_with_status_controls(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_panel'), {'month': '2026-05'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Staff Account Management')
        self.assertContains(response, 'Status Secretary')
        self.assertContains(response, 'status-secretary')
        self.assertContains(response, 'Active')
        self.assertContains(response, f'name="next" value="{reverse("admin_panel")}?month=2026-05"')

    def test_admin_can_mark_staff_account_inactive_and_active(self):
        self.client.force_login(self.admin_user)

        inactive_response = self.client.post(
            reverse('update_staff_account_status', args=[self.secretary_profile.id]),
            data={'account_status': 'inactive', 'next': reverse('admin_panel')},
            follow=True,
        )
        self.secretary_user.refresh_from_db()
        self.assertFalse(self.secretary_user.is_active)
        self.assertContains(inactive_response, 'marked inactive')

        active_response = self.client.post(
            reverse('update_staff_account_status', args=[self.secretary_profile.id]),
            data={'account_status': 'active', 'next': reverse('admin_panel')},
            follow=True,
        )
        self.secretary_user.refresh_from_db()
        self.assertTrue(self.secretary_user.is_active)
        self.assertContains(active_response, 'marked active')

    def test_inactive_staff_user_is_logged_out_and_redirected(self):
        self.secretary_user.is_active = False
        self.secretary_user.save(update_fields=['is_active'])
        self.client.force_login(self.secretary_user)

        response = self.client.get(reverse('secretary_panel'), follow=True)

        self.assertRedirects(response, f"{reverse('login')}?next={reverse('secretary_panel')}")


@override_settings(
    SMS_DELIVERY_PROVIDER='sms_api_ph',
    SMS_API_TIMEOUT=10,
    SMS_API_RETRY_ATTEMPTS=2,
    SMS_API_PH_ENDPOINT='https://dashboard.philsms.com/api/v3/',
    SMS_API_PH_API_KEY='2740|test-token',
    SMS_API_PH_RECIPIENT_FIELD='recipient',
    SMS_API_PH_MESSAGE_FIELD='message',
    SMS_API_PH_SENDER_ID='TABUAN',
    SMS_API_PH_MESSAGE_TYPE='plain',
)
class SMSDeliveryErrorTests(TestCase):
    def test_dns_failure_logs_provider_aware_message(self):
        with patch('billing.services.request.urlopen', side_effect=URLError(socket.gaierror(11001, 'getaddrinfo failed'))):
            result = send_test_sms('+639171234567', 'Test DNS failure')

        self.assertEqual(result.status, Notification.Statuses.FAILED)
        self.assertIn('Unable to reach SMS API PH', result.response_message)
        self.assertIn('DNS lookup failed', result.response_message)
        self.assertIn('dashboard.philsms.com', result.response_message)

    def test_timeout_is_retried_before_logging_failure(self):
        successful_response = MagicMock()
        successful_response.read.return_value = b'{"status":"success","message_id":"msg-123"}'
        successful_context = MagicMock()
        successful_context.__enter__.return_value = successful_response
        successful_context.__exit__.return_value = False

        with patch(
            'billing.services.request.urlopen',
            side_effect=[TimeoutError('The read operation timed out'), successful_context],
        ) as mocked_urlopen:
            result = send_test_sms('+639171234567', 'Retry on timeout')

        self.assertEqual(mocked_urlopen.call_count, 2)
        self.assertEqual(result.status, Notification.Statuses.SENT)
        self.assertIn('msg-123', result.response_message)


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


class ConsumerPaymongoFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='portal-consumer', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Portal Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.profile,
            full_name='Portal Consumer',
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

    def test_consumer_payment_form_forces_online_method_and_selected_wallet(self):
        form = ConsumerPaymentForm(
            data={
                'covered_month': '2026-04',
                'payment_option': Payment.PaymentOptions.FULL,
                'amount_paid': '0',
                'online_wallet': 'gcash',
            },
            consumer=self.consumer,
            system_settings=SystemSettings.load(),
        )

        self.assertTrue(form.is_valid(), form.errors)
        payment = form.save()

        self.assertEqual(payment.payment_method, Payment.Methods.ONLINE)
        self.assertEqual(payment.gateway, Payment.Methods.GCASH)
        self.assertEqual(payment.amount_paid, Decimal('600'))
        self.assertEqual(payment.display_payment_method, 'GCash')


class BulkSelectivePaymentFormTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='cashier-bulk', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.user,
            full_name='Cashier Bulk',
            role=ConsumerProfile.Roles.TREASURER,
        )
        self.consumer = Consumer.objects.create(
            full_name='Bulk Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        self.march = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 3, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            service_charge=Decimal('0'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
        )
        self.april = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('10'),
            current_reading=Decimal('20'),
            rate_per_m3=Decimal('20'),
            service_charge=Decimal('100'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        self.may = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 5, 1),
            previous_reading=Decimal('20'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('20'),
            service_charge=Decimal('200'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
        )

    def test_admin_full_bulk_payment_uses_total_outstanding_balance(self):
        form = AdminPaymentForm(
            data={
                'consumer': self.consumer.id,
                'billing': self.may.id,
                'payment_method': Payment.Methods.CASH,
                'payment_option': Payment.PaymentOptions.FULL,
                'amount_paid': '0',
                'discount_amount': '0',
                'payment_date': '2026-05-20',
                'status': Payment.Statuses.COMPLETED,
                'reference_number': '',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['settlement_scope'], Payment.SettlementScopes.BULK)
        self.assertEqual(form.cleaned_data['amount_paid'], Decimal('2100.00'))

    def test_admin_selective_payment_uses_selected_months_only(self):
        form = AdminPaymentForm(
            data={
                'consumer': self.consumer.id,
                'billing': self.april.id,
                'selected_billings': [str(self.march.id), str(self.april.id)],
                'payment_method': Payment.Methods.CASH,
                'payment_option': Payment.PaymentOptions.PARTIAL,
                'amount_paid': '0',
                'discount_amount': '0',
                'payment_date': '2026-05-20',
                'status': Payment.Statuses.COMPLETED,
                'reference_number': '',
                'arrangement_note': 'Approved selective settlement.',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['settlement_scope'], Payment.SettlementScopes.SELECTIVE)
        self.assertEqual(form.cleaned_data['amount_paid'], Decimal('1300.00'))


class SelectiveArrangementWorkflowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='arrangement-consumer', password='StrongPass1!')
        self.profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Arrangement Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=self.profile,
            full_name='Arrangement Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        self.march = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 3, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
        )
        self.april = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('10'),
            current_reading=Decimal('20'),
            rate_per_m3=Decimal('20'),
            service_charge=Decimal('100'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        self.may = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 5, 1),
            previous_reading=Decimal('20'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('20'),
            service_charge=Decimal('200'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
        )

    @patch('billing.views.is_paymongo_configured', return_value=True)
    def test_consumer_partial_request_creates_pending_arrangement(self, mocked_paymongo_ready):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse('consumer_payment'),
            data={
                'covered_month': '2026-05',
                'payment_option': Payment.PaymentOptions.PARTIAL,
                'selected_billings': [str(self.march.id), str(self.april.id)],
                'amount_paid': '0',
                'arrangement_note': 'Request to settle two months first.',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Payment.objects.count(), 0)
        arrangement = PaymentArrangement.objects.get()
        self.assertEqual(arrangement.status, PaymentArrangement.Statuses.PENDING)
        self.assertEqual(arrangement.requested_amount, Decimal('1300.00'))

    def test_selective_arrangement_allocation_only_applies_to_selected_months(self):
        payment = Payment.objects.create(
            consumer=self.consumer,
            billing=self.may,
            covered_month=date(2026, 5, 1),
            payment_method=Payment.Methods.CASH,
            payment_option=Payment.PaymentOptions.PARTIAL,
            settlement_scope=Payment.SettlementScopes.SELECTIVE,
            amount_paid=Decimal('1500.00'),
            payment_date=date(2026, 5, 20),
            status=Payment.Statuses.COMPLETED,
        )
        arrangement = create_payment_arrangement(
            self.consumer,
            [self.april, self.may],
            requested_by=self.user,
            requested_amount=payment.amount_paid,
            outstanding_balance=get_consumer_outstanding_balance(self.consumer),
            notes='Approved selective payment.',
            status=PaymentArrangement.Statuses.COMPLETED,
            approved_by=self.user,
            payment=payment,
        )

        rebuild_consumer_payment_allocations(self.consumer)
        self.march.refresh_from_db()
        self.april.refresh_from_db()
        self.may.refresh_from_db()
        arrangement.refresh_from_db()

        self.assertEqual(self.march.amount_paid, Decimal('0'))
        self.assertEqual(self.april.amount_paid, self.april.total_amount)
        self.assertEqual(self.may.amount_paid, self.may.total_amount)
        self.assertEqual(arrangement.payment_id, payment.id)


class DisconnectionMonitoringWorkflowTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='disco-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Disconnection Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer = Consumer.objects.create(
            full_name='For Monitoring',
            status=Consumer.Statuses.ACTIVE,
        )
        for month in (3, 4, 5):
            BillingRecord.objects.create(
                consumer=self.consumer,
                billing_month=date(2026, month, 1),
                previous_reading=Decimal('0'),
                current_reading=Decimal('10'),
                rate_per_m3=Decimal('20'),
                amount_paid=Decimal('0'),
                billing_date=date(2026, month, 1),
                due_date=date(2026, month, 15),
            )

    def test_refresh_moves_overdue_account_to_for_disconnection(self):
        self.consumer.warning_issued_at = timezone.now() - timedelta(days=16)
        self.consumer.disconnection_scheduled_for = timezone.localdate() - timedelta(days=1)
        self.consumer.save(update_fields=['warning_issued_at', 'disconnection_scheduled_for'])

        refresh_consumer_account_status(self.consumer)
        self.consumer.refresh_from_db()

        self.assertEqual(self.consumer.account_status, Consumer.AccountStatuses.FOR_DISCONNECTION)
        self.assertTrue(
            DisconnectionRecord.objects.filter(
                consumer=self.consumer,
                status=DisconnectionRecord.Statuses.FOR_DISCONNECTION,
            ).exists()
        )

    def test_monitor_overdue_accounts_command_updates_consumer_statuses(self):
        call_command('monitor_overdue_accounts')
        self.consumer.refresh_from_db()

        self.assertEqual(self.consumer.account_status, Consumer.AccountStatuses.DELINQUENT)
        self.assertIsNotNone(self.consumer.warning_issued_at)


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

    def test_rejects_lower_than_previous_reading(self):
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'reading_date': '2026-04-30',
                'current_reading': '90',
                'notes': '',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('Current reading cannot be lower than the previous reading.', form.errors['current_reading'])

    def test_allows_large_reading_jump(self):
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'reading_date': '2026-04-30',
                'current_reading': '700',
                'notes': '',
            }
        )

        self.assertTrue(form.is_valid())

    def test_usage_applies_minimum_billable_volume(self):
        reading = MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 4, 30),
            previous_reading=Decimal('100'),
            current_reading=Decimal('120'),
        )
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('100'),
            current_reading=Decimal('120'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 30),
            due_date=date(2026, 5, 15),
        )

        self.assertEqual(reading.usage_m3, Decimal('30'))
        self.assertEqual(billing.usage_m3, Decimal('30'))
        self.assertEqual(billing.total_amount, Decimal('600'))


class ReaderMeterReadingSubmissionTests(TestCase):
    def setUp(self):
        self.reader_user = User.objects.create_user(username='reader-submit', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.reader_user,
            full_name='Reader Submit',
            role=ConsumerProfile.Roles.READER,
        )
        self.consumer = Consumer.objects.create(
            full_name='Submission Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 3, 31),
            previous_reading=Decimal('0'),
            current_reading=Decimal('100'),
        )
        settings_obj = SystemSettings.load()
        settings_obj.rate_per_m3 = Decimal('20.00')
        settings_obj.billing_due_days = 15
        settings_obj.save()

    @patch('billing.services.notify_consumer')
    @patch('billing.services.notify_roles')
    def test_submission_creates_pending_billing_with_generated_amount(self, mocked_notify_roles, mocked_notify_consumer):
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'reading_date': '2026-04-30',
                'current_reading': '145',
                'notes': 'April route',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)

        reading, billing = handle_meter_reading_submission(form, self.reader_user)

        self.assertEqual(reading.consumer, self.consumer)
        self.assertEqual(reading.previous_reading, Decimal('100'))
        self.assertEqual(reading.current_reading, Decimal('145'))
        self.assertEqual(reading.usage_m3, Decimal('45'))
        self.assertEqual(billing.consumer, self.consumer)
        self.assertEqual(billing.billing_month, date(2026, 4, 1))
        self.assertEqual(billing.usage_m3, Decimal('45'))
        self.assertEqual(billing.total_amount, Decimal('900.00'))
        self.assertEqual(billing.previous_arrears, Decimal('0'))
        self.assertEqual(billing.statement_total_snapshot, Decimal('900.00'))
        self.assertEqual(billing.amount_paid, Decimal('0'))
        self.assertEqual(billing.status, BillingRecord.Statuses.PENDING)
        mocked_notify_roles.assert_called_once()
        self.assertEqual(mocked_notify_consumer.call_count, 2)

    @patch('billing.services.notify_consumer')
    @patch('billing.services.notify_roles')
    def test_submission_attaches_existing_payment_but_keeps_billing_pending(self, mocked_notify_roles, mocked_notify_consumer):
        Payment.objects.create(
            consumer=self.consumer,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('900.00'),
            payment_date=date(2026, 4, 5),
            status=Payment.Statuses.COMPLETED,
        )
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'reading_date': '2026-04-30',
                'current_reading': '145',
                'notes': 'Advance-paid month',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)

        reading, billing = handle_meter_reading_submission(form, self.reader_user)
        advance_payment = Payment.objects.get(consumer=self.consumer, covered_month=date(2026, 4, 1))

        self.assertEqual(reading.usage_m3, Decimal('45'))
        self.assertEqual(billing.total_amount, Decimal('900.00'))
        self.assertEqual(billing.amount_paid, Decimal('900.00'))
        self.assertEqual(billing.status, BillingRecord.Statuses.PENDING)
        self.assertEqual(advance_payment.billing_id, billing.id)
        mocked_notify_roles.assert_called_once()
        self.assertEqual(mocked_notify_consumer.call_count, 2)

    @patch('billing.services.notify_consumer')
    @patch('billing.services.notify_roles')
    def test_submission_carries_previous_arrears_into_billing_snapshot(self, mocked_notify_roles, mocked_notify_consumer):
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 3, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('10'),
            service_charge=Decimal('200'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
        )
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'reading_date': '2026-04-30',
                'current_reading': '145',
                'notes': 'Arrears month',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)

        _, billing = handle_meter_reading_submission(form, self.reader_user)

        self.assertEqual(billing.previous_arrears, Decimal('500.00'))
        self.assertEqual(billing.total_amount, Decimal('900.00'))
        self.assertEqual(billing.statement_total_snapshot, Decimal('1400.00'))
        mocked_notify_roles.assert_called_once()
        self.assertEqual(mocked_notify_consumer.call_count, 2)

    def test_submission_rejects_duplicate_billing_cycle_reading(self):
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'reading_date': '2026-03-30',
                'current_reading': '130',
                'notes': 'Duplicate cycle attempt',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            'A meter reading for this billing cycle already exists. Use the edit action to correct the submitted reading.',
            form.non_field_errors(),
        )

    def test_submission_rejects_meter_and_consumer_account_mismatch(self):
        other_consumer = Consumer.objects.create(
            full_name='Other Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        form = MeterReadingForm(
            data={
                'meter_number': self.consumer.meter_number,
                'consumer_id': str(other_consumer.id),
                'consumer_account': f'ACC-{other_consumer.id:05d}',
                'registered_connection': other_consumer.full_name,
                'reading_date': '2026-04-30',
                'current_reading': '145',
                'notes': 'Mismatch attempt',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            'The selected consumer account does not match the entered meter number.',
            form.errors['consumer_account'],
        )


class RunningBalanceAllocationTests(TestCase):
    def setUp(self):
        self.consumer = Consumer.objects.create(
            full_name='Allocation Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        self.first_bill = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 1, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('10'),
            service_charge=Decimal('200'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 1, 1),
            due_date=date(2026, 1, 15),
        )
        self.second_bill = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 2, 1),
            previous_reading=Decimal('30'),
            current_reading=Decimal('60'),
            rate_per_m3=Decimal('10'),
            service_charge=Decimal('300'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 2, 1),
            due_date=date(2026, 2, 15),
        )
        self.third_bill = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 3, 1),
            previous_reading=Decimal('60'),
            current_reading=Decimal('90'),
            rate_per_m3=Decimal('10'),
            service_charge=Decimal('400'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
        )

    def test_completed_payment_is_applied_to_oldest_unpaid_balances_first(self):
        payment = Payment.objects.create(
            consumer=self.consumer,
            billing=self.third_bill,
            covered_month=date(2026, 3, 1),
            payment_method=Payment.Methods.CASH,
            amount_paid=Decimal('650'),
            payment_date=date(2026, 3, 20),
            status=Payment.Statuses.COMPLETED,
        )

        self.first_bill.refresh_from_db()
        self.second_bill.refresh_from_db()
        self.third_bill.refresh_from_db()

        self.assertEqual(self.first_bill.amount_paid, Decimal('500.00'))
        self.assertEqual(self.first_bill.status, BillingRecord.Statuses.PAID)
        self.assertEqual(self.second_bill.amount_paid, Decimal('150.00'))
        self.assertEqual(self.second_bill.status, BillingRecord.Statuses.PARTIALLY_PAID)
        self.assertEqual(self.third_bill.amount_paid, Decimal('0'))
        self.assertEqual(self.third_bill.status, BillingRecord.Statuses.PENDING)
        self.assertEqual(payment.allocations.count(), 2)
        self.assertEqual(get_consumer_outstanding_balance(self.consumer), Decimal('1150.00'))


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

    def test_rejects_current_reading_lower_than_previous_reading(self):
        form = BillingRecordForm(
            data={
                'consumer': self.consumer.id,
                'billing_month': '2026-05-01',
                'previous_reading': '120',
                'current_reading': '110',
                'rate_per_m3': '20',
                'amount_paid': '0',
                'status': BillingRecord.Statuses.PENDING,
                'billing_date': '2026-05-01',
                'due_date': '2026-05-15',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('Current reading cannot be lower than the previous reading.', form.errors['current_reading'])


class PaymentLegacyBillingSafetyTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='legacy-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Legacy Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer = Consumer.objects.create(
            full_name='Legacy Billing Consumer',
            status=Consumer.Statuses.ACTIVE,
        )

    def test_cash_payment_does_not_revalidate_unchanged_legacy_readings(self):
        billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        BillingRecord.objects.filter(pk=billing.pk).update(
            previous_reading=Decimal('120'),
            current_reading=Decimal('110'),
            usage_m3=Decimal('30'),
            total_amount=Decimal('600'),
        )

        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse('payments'),
            data={
                'consumer': self.consumer.id,
                'billing': billing.id,
                'payment_method': Payment.Methods.CASH,
                'payment_option': Payment.PaymentOptions.FULL,
                'amount_paid': '0',
                'discount_amount': '0',
                'payment_date': '2026-04-10',
                'status': Payment.Statuses.COMPLETED,
                'reference_number': '',
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        billing.refresh_from_db()
        self.assertEqual(billing.amount_paid, Decimal('600'))
        self.assertEqual(billing.status, BillingRecord.Statuses.PAID)


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
        self.assertContains(response, 'Billing Monitoring')
        self.assertContains(response, 'Pending Consumer')
        self.assertContains(response, 'Overdue Consumer')
        self.assertNotContains(response, 'Meeting Minutes')

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


class SecretaryMeetingMinutesTests(TestCase):
    def setUp(self):
        self.secretary = User.objects.create_user(username='minutes-secretary', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.secretary,
            full_name='Minutes Secretary',
            role=ConsumerProfile.Roles.SECRETARY,
        )
        self.other_secretary = User.objects.create_user(username='other-secretary', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.other_secretary,
            full_name='Other Secretary',
            role=ConsumerProfile.Roles.SECRETARY,
        )

    def test_secretary_can_create_minutes_with_initial_revision(self):
        self.client.force_login(self.secretary)

        response = self.client.post(
            reverse('secretary_panel'),
            data={
                'title': 'May Water District Meeting',
                'meeting_date': '2026-05-07',
                'meeting_time': '09:00',
                'location': 'Barangay Hall',
                'attendees': 'Secretary\nTreasurer',
                'agenda': '1. Collections update',
                'discussion_points': 'Collections improved versus April.',
                'resolutions': 'Approve next billing reminder cycle.',
                'action_items': 'Secretary - circulate minutes - May 08',
                'additional_notes': 'Prepared in dashboard editor.',
                'change_summary': 'Created the first draft.',
                'minutes_action': 'save',
            },
        )

        self.assertEqual(response.status_code, 302)
        minutes = MeetingMinutes.objects.get(secretary=self.secretary, title='May Water District Meeting')
        self.assertEqual(minutes.status, MeetingMinutes.Statuses.DRAFT)
        self.assertEqual(minutes.revisions.count(), 1)
        self.assertEqual(minutes.revisions.first().change_summary, 'Created the first draft.')

    def test_secretary_only_can_open_own_minutes_document_endpoint(self):
        minutes = MeetingMinutes.objects.create(
            secretary=self.secretary,
            title='Owned Minutes',
            meeting_date=date(2026, 5, 7),
            location='Office',
            attendees='Secretary',
            agenda='1. Agenda',
            discussion_points='Discussion',
            resolutions='Resolution',
            action_items='Action',
        )

        self.client.force_login(self.other_secretary)
        response = self.client.get(reverse('secretary_meeting_minutes_detail', args=[minutes.id]))

        self.assertEqual(response.status_code, 404)

    def test_final_approval_locks_future_edits(self):
        minutes = MeetingMinutes.objects.create(
            secretary=self.secretary,
            title='Approval Flow',
            meeting_date=date(2026, 5, 7),
            location='Office',
            attendees='Secretary',
            agenda='1. Agenda',
            discussion_points='Discussion',
            resolutions='Resolution',
            action_items='Action',
        )
        minutes.record_revision(edited_by=self.secretary, change_summary='Created draft.', changed_fields=['created'])

        self.client.force_login(self.secretary)
        approve_response = self.client.post(
            reverse('secretary_panel'),
            data={
                'minutes_id': minutes.id,
                'title': minutes.title,
                'meeting_date': '2026-05-07',
                'meeting_time': '',
                'location': minutes.location,
                'attendees': minutes.attendees,
                'agenda': minutes.agenda,
                'discussion_points': minutes.discussion_points,
                'resolutions': minutes.resolutions,
                'action_items': minutes.action_items,
                'additional_notes': '',
                'change_summary': 'Ready for final approval.',
                'minutes_action': 'approve',
            },
        )

        self.assertEqual(approve_response.status_code, 302)
        minutes.refresh_from_db()
        self.assertEqual(minutes.status, MeetingMinutes.Statuses.APPROVED)
        self.assertEqual(minutes.revisions.count(), 2)

        edit_response = self.client.post(
            reverse('secretary_panel'),
            data={
                'minutes_id': minutes.id,
                'title': 'Edited After Approval',
                'meeting_date': '2026-05-07',
                'meeting_time': '',
                'location': minutes.location,
                'attendees': minutes.attendees,
                'agenda': minutes.agenda,
                'discussion_points': minutes.discussion_points,
                'resolutions': minutes.resolutions,
                'action_items': minutes.action_items,
                'additional_notes': '',
                'change_summary': 'Should be blocked.',
                'minutes_action': 'save',
            },
        )

        self.assertEqual(edit_response.status_code, 302)
        minutes.refresh_from_db()
        self.assertEqual(minutes.title, 'Approval Flow')
        self.assertEqual(minutes.revisions.count(), 2)

    def test_secretary_can_export_own_minutes_as_pdf(self):
        minutes = MeetingMinutes.objects.create(
            secretary=self.secretary,
            title='PDF Export Minutes',
            meeting_date=date(2026, 5, 7),
            location='Office',
            attendees='Secretary',
            agenda='1. Agenda',
            discussion_points='Discussion content for PDF export.',
            resolutions='Resolution',
            action_items='Action',
            additional_notes='Note',
        )

        self.client.force_login(self.secretary)
        response = self.client.get(reverse('secretary_meeting_minutes_export_pdf', args=[minutes.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('.pdf', response['Content-Disposition'])
        self.assertGreater(len(response.content), 100)


class GoogleConsumerLoginTests(TestCase):
    def test_google_callback_creates_consumer_account_from_gmail_details(self):
        session = self.client.session
        session['google_oauth_state'] = 'state-123'
        session.save()

        with patch('billing.views._google_oauth_exchange_code', return_value={'access_token': 'token-123'}), patch(
            'billing.views._google_oauth_fetch_userinfo',
            return_value={
                'email': 'consumer@gmail.com',
                'email_verified': True,
                'name': 'Consumer Gmail',
                'given_name': 'Consumer',
                'family_name': 'Gmail',
            },
        ):
            response = self.client.get(
                reverse('google_login_callback'),
                {'state': 'state-123', 'code': 'code-123'},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email='consumer@gmail.com')
        profile = ConsumerProfile.objects.get(user=user)
        consumer = Consumer.objects.get(profile=profile)
        self.assertEqual(profile.role, ConsumerProfile.Roles.CONSUMER)
        self.assertEqual(profile.full_name, 'Consumer Gmail')
        self.assertEqual(consumer.full_name, 'Consumer Gmail')

    def test_google_callback_rejects_staff_email_for_consumer_login(self):
        staff_user = User.objects.create_user(
            username='secretary-login',
            email='staff@gmail.com',
            password='StrongPass1!',
        )
        ConsumerProfile.objects.create(
            user=staff_user,
            full_name='Staff Login',
            email='staff@gmail.com',
            role=ConsumerProfile.Roles.SECRETARY,
        )
        session = self.client.session
        session['google_oauth_state'] = 'state-456'
        session.save()

        with patch('billing.views._google_oauth_exchange_code', return_value={'access_token': 'token-456'}), patch(
            'billing.views._google_oauth_fetch_userinfo',
            return_value={
                'email': 'staff@gmail.com',
                'email_verified': True,
                'name': 'Staff Login',
                'given_name': 'Staff',
                'family_name': 'Login',
            },
        ):
            response = self.client.get(
                reverse('google_login_callback'),
                {'state': 'state-456', 'code': 'code-456'},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Use the regular staff login instead')


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
            amount_paid=Decimal('600'),
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
        self.assertEqual(summary['overdue_accounts_total'], Decimal('500'))
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
            amount_paid=Decimal('600'),
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

    def test_consumer_dashboard_uses_monitoring_only_layout(self):
        self.client.force_login(self.consumer_user)

        response = self.client.get(reverse('consumer_panel'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Billing Summary')
        self.assertContains(response, 'Outstanding Balance')
        self.assertContains(response, 'Consumption')
        self.assertContains(response, 'Billing Alerts')
        self.assertNotContains(response, 'Performance Trend')
        self.assertNotContains(response, 'Submit Payment')


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
        self.assertContains(response, 'Hi There')
        self.assertContains(response, 'Log In')
        self.assertContains(response, 'Tabuan Water Billing logo')

    def test_signup_page_uses_tabuan_logo_visual_panel(self):
        response = self.client.get(reverse('signup'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Create Consumer Account')
        self.assertContains(response, 'Tabuan Water Billing logo')

    def test_login_page_exposes_forgot_password_link(self):
        response = self.client.get(reverse('login'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('password_reset'))
        self.assertContains(response, 'Forgot Password?')

    def test_logout_page_renders_after_post(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse('logout'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'You are now logged out')
        self.assertContains(response, 'Signed out successfully.')
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

    def test_password_change_request_form_uses_confirm_password_label(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('password_change'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Confirm Password')

    def test_password_reset_form_renders(self):
        response = self.client.get(reverse('password_reset'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Forgot Password')
        self.assertContains(response, 'Send Reset Link')

    @patch('billing.views.send_user_security_otp')
    def test_password_change_otp_updates_only_logged_in_account(self, mocked_send_otp):
        self.profile.email = 'brand@example.com'
        self.profile.contact = '+639171234567'
        self.profile.save(update_fields=['email', 'contact'])
        self.user.email = 'brand@example.com'
        self.user.save(update_fields=['email'])
        other_user = User.objects.create_user(username='other-user', password='OtherPass1!')
        ConsumerProfile.objects.create(
            user=other_user,
            full_name='Other User',
            email='other@example.com',
            contact='+639171234568',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        mocked_send_otp.return_value = MagicMock(status=Notification.Statuses.SENT, response_message='OTP sent')

        self.client.force_login(self.user)
        request_response = self.client.post(
            reverse('password_change'),
            data={
                'current_password': 'StrongPass1!',
                'new_password1': 'BrandNewPass1!',
                'new_password2': 'BrandNewPass1!',
                'otp_channel': 'email',
            },
        )

        self.assertRedirects(request_response, reverse('password_change_verify'))
        session = self.client.session
        token = session.get('password_change_otp_token')
        self.assertTrue(token)

        from django.core.cache import cache
        payload = cache.get(f'password_change_otp:{token}')
        self.assertIsNotNone(payload)

        verify_response = self.client.post(
            reverse('password_change_verify'),
            data={'otp_code': payload['otp_code']},
        )

        self.assertRedirects(verify_response, reverse('password_change_done'))
        self.user.refresh_from_db()
        other_user.refresh_from_db()
        self.assertTrue(self.user.check_password('BrandNewPass1!'))
        self.assertTrue(other_user.check_password('OtherPass1!'))


class OnlinePaymentMethodDisplayTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='online-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.user,
            full_name='Online Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer = Consumer.objects.create(
            full_name='Online Consumer',
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

    def test_admin_payment_form_requires_online_channel_for_online_method(self):
        form = AdminPaymentForm(
            data={
                'consumer': self.consumer.id,
                'billing': self.billing.id,
                'payment_method': Payment.Methods.ONLINE,
                'payment_option': Payment.PaymentOptions.FULL,
                'amount_paid': '200',
                'discount_amount': '0',
                'payment_date': '2026-04-10',
                'status': Payment.Statuses.COMPLETED,
                'reference_number': 'PAY-REF-1',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('Choose the online payment channel for this transaction.', form.errors['online_channel'])

    def test_admin_payment_form_keeps_cash_method_and_wallet_only_online_choices(self):
        form = AdminPaymentForm()

        self.assertEqual(
            form.fields['payment_method'].choices,
            [
                (Payment.Methods.CASH, Payment.Methods.CASH.label),
                (Payment.Methods.ONLINE, Payment.Methods.ONLINE.label),
            ],
        )
        self.assertEqual(
            form.fields['online_channel'].choices,
            [
                (Payment.Methods.GCASH, Payment.Methods.GCASH.label),
                (Payment.Methods.PAYMAYA, 'Maya'),
            ],
        )

    def test_admin_payment_form_saves_online_channel_and_display_method(self):
        form = AdminPaymentForm(
            data={
                'consumer': self.consumer.id,
                'billing': self.billing.id,
                'payment_method': Payment.Methods.ONLINE,
                'online_channel': Payment.Methods.PAYMAYA,
                'payment_option': Payment.PaymentOptions.FULL,
                'amount_paid': '200',
                'discount_amount': '0',
                'payment_date': '2026-04-10',
                'status': Payment.Statuses.COMPLETED,
                'reference_number': 'PAY-REF-2',
            }
        )

        self.assertTrue(form.is_valid(), form.errors)
        payment = form.save()

        self.assertEqual(payment.gateway, Payment.Methods.PAYMAYA)
        self.assertEqual(payment.display_payment_method, 'Maya')

    def test_paymongo_response_fallback_displays_wallet_label(self):
        payment = Payment.objects.create(
            consumer=self.consumer,
            billing=self.billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('600'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
            gateway='paymongo',
            gateway_response={
                'payment_intent': {
                    'attributes': {
                        'payment_method_allowed': ['paymaya'],
                    }
                }
            },
        )

        self.assertEqual(payment.display_payment_method, 'Maya')


class ReceiptActionRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='receipt-consumer', password='StrongPass1!')
        profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Receipt Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=profile,
            full_name='Receipt Consumer',
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
        self.payment = Payment.objects.create(
            consumer=self.consumer,
            billing=self.billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('600'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
            gateway=Payment.Methods.GCASH,
            reference_number='pi_123456789',
        )

    def test_consumer_payment_page_receipts_offer_view_and_print_links(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('consumer_payment'))
        receipt_url = reverse('payment_receipt', args=[self.payment.id])

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, receipt_url)
        self.assertContains(response, f'{receipt_url}?print=1')
        self.assertContains(response, 'Print')

    def test_receipt_page_enables_auto_print_mode(self):
        self.client.force_login(self.user)

        response = self.client.get(f"{reverse('payment_receipt', args=[self.payment.id])}?print=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Print Receipt')
        self.assertContains(response, "shouldPrintOnLoad = true")

    def test_receipt_displays_arrears_and_outstanding_breakdown(self):
        self.billing.previous_arrears = Decimal('300')
        self.billing.service_charge = Decimal('50')
        self.billing.penalty_amount = Decimal('25')
        self.billing.save()
        self.payment.amount_paid = Decimal('240')
        self.payment.save()

        self.client.force_login(self.user)
        response = self.client.get(reverse('payment_receipt', args=[self.payment.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Previous Arrears')
        self.assertContains(response, 'Current Bill')
        self.assertContains(response, 'Total Outstanding Before Payment')
        self.assertContains(response, 'Remaining Outstanding')

    def test_receipt_shows_warning_notice_for_three_unpaid_cycles(self):
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 3, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 5, 1),
            previous_reading=Decimal('10'),
            current_reading=Decimal('20'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 6, 1),
            previous_reading=Decimal('20'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 6, 1),
            due_date=date(2026, 6, 15),
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse('payment_receipt', args=[self.payment.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'WARNING NOTICE')
        self.assertContains(response, 'possible disconnection within 15 days')


class ConsumerDashboardMonitoringTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='monitor-consumer', password='StrongPass1!')
        profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Monitor Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=profile,
            full_name='Monitor Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
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
        self.completed_payment = Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('600'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.COMPLETED,
            gateway=Payment.Methods.GCASH,
            reference_number='pi_completed',
        )
        self.pending_payment = Payment.objects.create(
            consumer=self.consumer,
            billing=billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('600'),
            payment_date=date(2026, 4, 11),
            status=Payment.Statuses.PENDING,
            gateway=Payment.Methods.PAYMAYA,
            gateway_redirect_url='https://checkout.paymongo.com/test-session',
            reference_number='pi_pending',
        )

    def test_consumer_payment_page_shows_dedicated_payment_methods(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('consumer_payment'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment Method Selection')
        self.assertContains(response, 'GCash')
        self.assertContains(response, 'Bank')
        self.assertContains(response, 'Cash')

    def test_consumer_dashboard_and_payment_page_are_separated(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('consumer_panel'))
        payment_response = self.client.get(reverse('consumer_payment'))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Payment Status Tracking')
        self.assertNotContains(response, 'Upload Proof of Payment')
        self.assertContains(payment_response, 'Payment Status Tracking')
        self.assertContains(payment_response, 'Upload Proof of Payment')
        self.assertContains(payment_response, 'Pending Verification')

    def test_consumer_panel_data_only_returns_active_monitoring_rows(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('consumer_panel_data'), HTTP_X_REQUESTED_WITH='XMLHttpRequest')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Continue Checkout', response.json()['payment_rows_html'])
        self.assertNotIn('pi_completed', response.json()['payment_rows_html'])
        self.assertIn('Outstanding Balance', response.json()['comparison_html'])
        self.assertIn('Payment Status', response.json()['comparison_html'])

    def test_consumer_dashboard_shows_balance_overview_cards(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('consumer_panel'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Billing Summary')
        self.assertContains(response, 'Outstanding Balance')
        self.assertContains(response, 'Current Bill')
        self.assertContains(response, 'Total Amount to Pay')
        self.assertContains(response, 'Consumption (m³)')
        self.assertContains(response, 'Payment Status')


class ConsumerDashboardDelinquentWarningTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='delinquent-consumer', password='StrongPass1!')
        profile = ConsumerProfile.objects.create(
            user=self.user,
            full_name='Delinquent Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=profile,
            full_name='Delinquent Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 3, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 3, 1),
            due_date=date(2026, 3, 15),
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('10'),
            current_reading=Decimal('20'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 1),
            due_date=date(2026, 4, 15),
        )
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 5, 1),
            previous_reading=Decimal('20'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
        )

    def test_consumer_dashboard_shows_warning_after_three_unpaid_cycles(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse('consumer_panel'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Warning Notice')
        self.assertContains(response, 'Delinquent Account')
        self.assertContains(response, '3 unpaid billing cycles')
        self.assertContains(response, 'March 2026')
        self.assertContains(response, 'April 2026')
        self.assertContains(response, 'May 2026')


class PaymentCheckoutGuideTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='checkout-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Checkout Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer_user = User.objects.create_user(username='checkout-consumer', password='StrongPass1!')
        consumer_profile = ConsumerProfile.objects.create(
            user=self.consumer_user,
            full_name='Checkout Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=consumer_profile,
            full_name='Checkout Consumer',
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

    def test_payments_page_contains_checkout_guidance_for_online_methods(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('payments'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Payment method')
        self.assertContains(response, 'Choose E-Wallet')
        self.assertContains(response, 'Save Cash Payment')
        self.assertContains(response, 'select the E-wallets tab')
        self.assertContains(response, 'authenticate the payment through OTP or app PIN')
        self.assertContains(response, 'Selected e-wallet')
        self.assertNotContains(response, '<label for="id_online_channel">', html=False)

    def test_consumer_payment_page_contains_payment_workflow_sections(self):
        self.client.force_login(self.consumer_user)

        response = self.client.get(reverse('consumer_payment'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Outstanding Balance')
        self.assertContains(response, 'Upload Proof of Payment')
        self.assertContains(response, 'Payment Status Tracking')


class StaffPaymongoCheckoutTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='staff-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Staff Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer_user = User.objects.create_user(username='staff-consumer', password='StrongPass1!')
        consumer_profile = ConsumerProfile.objects.create(
            user=self.consumer_user,
            full_name='Staff Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
            contact='+639171234567',
        )
        self.consumer = Consumer.objects.create(
            profile=consumer_profile,
            full_name='Staff Consumer',
            contact_number='+639171234567',
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

    def test_staff_online_payment_redirects_to_paymongo_and_stores_reference(self):
        self.client.force_login(self.admin_user)
        fake_checkout = {
            'attached_intent': {
                'id': 'pi_staff_123',
                'attributes': {
                    'status': 'awaiting_payment_method',
                    'payment_method_allowed': ['gcash'],
                },
            },
            'payment_method': {
                'attributes': {
                    'type': 'gcash',
                }
            },
            'redirect_url': 'https://checkout.paymongo.com/staff-session',
        }

        with patch('billing.views.is_paymongo_configured', return_value=True), patch(
            'billing.views.create_paymongo_ewallet_payment',
            return_value=fake_checkout,
        ):
            response = self.client.post(
                reverse('payments'),
                data={
                    'consumer': self.consumer.id,
                    'billing': self.billing.id,
                    'payment_method': Payment.Methods.ONLINE,
                    'online_channel': Payment.Methods.GCASH,
                    'payment_option': Payment.PaymentOptions.FULL,
                    'amount_paid': '0',
                    'discount_amount': '0',
                    'payment_date': '2026-04-10',
                    'status': Payment.Statuses.COMPLETED,
                    'reference_number': '',
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://checkout.paymongo.com/staff-session')

        payment = Payment.objects.get(consumer=self.consumer, payment_method=Payment.Methods.ONLINE)
        self.assertEqual(payment.status, Payment.Statuses.PENDING)
        self.assertEqual(payment.gateway, Payment.Methods.GCASH)
        self.assertEqual(payment.reference_number, 'pi_staff_123')
        self.assertEqual(payment.gateway_reference, 'pi_staff_123')
        self.assertEqual(payment.gateway_redirect_url, 'https://checkout.paymongo.com/staff-session')

    def test_staff_can_open_paymongo_processing_page_for_online_payment(self):
        self.client.force_login(self.admin_user)
        payment = Payment.objects.create(
            consumer=self.consumer,
            billing=self.billing,
            covered_month=date(2026, 4, 1),
            payment_method=Payment.Methods.ONLINE,
            amount_paid=Decimal('600'),
            payment_date=date(2026, 4, 10),
            status=Payment.Statuses.PENDING,
            gateway=Payment.Methods.PAYMAYA,
            reference_number='pi_staff_456',
            gateway_reference='pi_staff_456',
        )

        response = self.client.get(reverse('paymongo_success', args=[payment.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('paymongo_verify', args=[payment.id]))
        self.assertContains(response, reverse('payments'))


class ReaderPanelPreviewTests(TestCase):
    def setUp(self):
        self.reader_user = User.objects.create_user(username='reader-preview', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.reader_user,
            full_name='Reader Preview',
            role=ConsumerProfile.Roles.READER,
        )
        consumer_user = User.objects.create_user(username='preview-consumer', password='StrongPass1!')
        consumer_profile = ConsumerProfile.objects.create(
            user=consumer_user,
            full_name='Preview Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=consumer_profile,
            full_name='Preview Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        settings_obj = SystemSettings.load()
        settings_obj.rate_per_m3 = Decimal('27.50')
        settings_obj.save()
        MeterReading.objects.create(
            consumer=self.consumer,
            reading_date=date(2026, 3, 31),
            previous_reading=Decimal('0'),
            current_reading=Decimal('100'),
        )

    def test_reader_panel_exposes_amount_preview_and_current_rate(self):
        self.client.force_login(self.reader_user)

        panel_response = self.client.get(reverse('reader_panel'))
        context_response = self.client.get(
            reverse('reader_reading_context'),
            {'meter_number': self.consumer.meter_number, 'reading_date': '2026-04-30'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(panel_response.status_code, 200)
        self.assertContains(panel_response, 'Estimated Amount')
        self.assertContains(panel_response, 'Billable Usage')
        self.assertContains(panel_response, 'Consumer account')
        self.assertContains(panel_response, 'Registered connection')
        self.assertEqual(context_response.status_code, 200)
        self.assertEqual(context_response.json()['consumer_id'], self.consumer.id)
        self.assertIn('Preview Consumer', context_response.json()['registered_connection'])
        self.assertEqual(Decimal(context_response.json()['previous_reading']), Decimal('100'))
        self.assertEqual(Decimal(context_response.json()['rate_per_m3']), Decimal('27.50'))

    def test_reader_sidebar_link_targets_submit_meter_reading_section(self):
        self.client.force_login(self.reader_user)

        response = self.client.get(reverse('reader_panel'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'{reverse("reader_panel")}#submit-meter-reading')
        self.assertContains(response, 'id="submit-meter-reading"', html=False)


class AdminPanelMonthFilterTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='admin-month', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Admin Month',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.secretary_user = User.objects.create_user(username='minutes-owner', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.secretary_user,
            full_name='Minutes Owner',
            role=ConsumerProfile.Roles.SECRETARY,
        )
        self.consumer = Consumer.objects.create(
            full_name='Month Filter Consumer',
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
        BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 5, 1),
            previous_reading=Decimal('10'),
            current_reading=Decimal('30'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 5, 1),
            due_date=date(2026, 5, 15),
        )
        self.meeting_minutes = MeetingMinutes.objects.create(
            secretary=self.secretary_user,
            title='Admin Oversight Minutes',
            meeting_date=date(2026, 5, 7),
            location='Office',
            attendees='Secretary',
            agenda='1. Agenda',
            discussion_points='Oversight discussion',
            resolutions='Resolution',
            action_items='Action',
        )
        self.meeting_minutes.record_revision(
            edited_by=self.secretary_user,
            change_summary='Created oversight draft.',
            changed_fields=['created'],
        )

    def test_admin_panel_filters_statistics_by_selected_month(self):
        self.client.force_login(self.admin_user)

        page_response = self.client.get(reverse('admin_panel'), {'month': '2026-04'})
        data_response = self.client.get(
            reverse('admin_panel_data'),
            {'month': '2026-04'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context['selected_month'], date(2026, 4, 1))
        self.assertEqual(page_response.context['monthly_billed'], Decimal('600'))
        self.assertContains(page_response, 'Statistics Month')
        self.assertEqual(data_response.status_code, 200)
        self.assertEqual(data_response.json()['monthly_billed'], '600.00')
        self.assertIn('April 2026', data_response.json()['billing_rows_html'])
        self.assertNotIn('May 2026', data_response.json()['billing_rows_html'])

    def test_admin_panel_uses_payment_date_for_live_collection_totals(self):
        april_billing = BillingRecord.objects.get(consumer=self.consumer, billing_month=date(2026, 4, 1))
        payment = Payment.objects.create(
            consumer=self.consumer,
            billing=april_billing,
            payment_method=Payment.Methods.CASH,
            payment_option=Payment.PaymentOptions.FULL,
            amount_paid=Decimal('75'),
            payment_date=date(2026, 5, 10),
            covered_month=date(2026, 4, 1),
            status=Payment.Statuses.COMPLETED,
        )

        self.client.force_login(self.admin_user)
        page_response = self.client.get(reverse('admin_panel'), {'month': '2026-05'})
        data_response = self.client.get(
            reverse('admin_panel_data'),
            {'month': '2026-05'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(page_response.status_code, 200)
        self.assertEqual(page_response.context['monthly_collected'], Decimal('75'))
        self.assertIn(payment, page_response.context['recent_payments'])
        self.assertEqual(data_response.status_code, 200)
        self.assertEqual(data_response.json()['monthly_collected'], '75.00')
        self.assertIn('Month Filter Consumer', data_response.json()['payment_rows_html'])

    def test_admin_panel_excludes_meeting_minutes_monitoring_widget(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('admin_panel'), {'month': '2026-05'})
        data_response = self.client.get(
            reverse('admin_panel_data'),
            {'month': '2026-05'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Meeting Minutes Oversight')
        self.assertNotContains(response, 'Admin Oversight Minutes')
        self.assertEqual(data_response.status_code, 200)
        self.assertNotIn('minutes_monitoring_html', data_response.json())

    def test_admin_panel_displays_outbound_notification_logs(self):
        Notification.objects.create(
            consumer=self.consumer,
            channel=Notification.Channels.EMAIL,
            notification_type=Notification.Types.BILL_DUE,
            title='Water bill due notice',
            message='Statement of account sent to the consumer email.',
            status=Notification.Statuses.SENT,
            response_message='Delivered by SMTP provider.',
        )
        Notification.objects.create(
            consumer=self.consumer,
            channel=Notification.Channels.SMS,
            notification_type=Notification.Types.PAYMENT,
            title='Payment status update',
            message='Payment notification sent to the registered mobile number.',
            status=Notification.Statuses.FAILED,
            response_message='Provider timeout.',
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('admin_panel'), {'month': '2026-05'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Outbound Email and SMS Logs')
        self.assertContains(response, 'Water bill due notice')
        self.assertContains(response, 'Payment status update')
        self.assertContains(response, 'Delivered by SMTP provider.')
        self.assertContains(response, 'Provider timeout.')

    def test_admin_panel_filters_outbound_notification_logs(self):
        Notification.objects.create(
            consumer=self.consumer,
            channel=Notification.Channels.EMAIL,
            notification_type=Notification.Types.BILL_DUE,
            title='Email due notice',
            message='Email notification for the due bill.',
            status=Notification.Statuses.SENT,
        )
        Notification.objects.create(
            consumer=self.consumer,
            channel=Notification.Channels.SMS,
            notification_type=Notification.Types.PAYMENT,
            title='SMS payment update',
            message='SMS notification for the latest payment.',
            status=Notification.Statuses.FAILED,
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(
            reverse('admin_panel'),
            {
                'month': '2026-05',
                'notification_channel': Notification.Channels.SMS,
                'notification_status': Notification.Statuses.FAILED,
                'notification_query': 'payment',
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'SMS payment update')
        self.assertNotContains(response, 'Email due notice')


class AdminRegistrationTests(TestCase):
    def test_meeting_minutes_models_are_registered_in_django_admin(self):
        self.assertIn(MeetingMinutes, site._registry)
        self.assertIn(MeetingMinutesRevision, site._registry)


class SettingsPropagationTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_user(username='settings-admin', password='StrongPass1!')
        ConsumerProfile.objects.create(
            user=self.admin_user,
            full_name='Settings Admin',
            role=ConsumerProfile.Roles.ADMIN,
        )
        self.consumer_user = User.objects.create_user(username='settings-consumer', password='StrongPass1!')
        consumer_profile = ConsumerProfile.objects.create(
            user=self.consumer_user,
            full_name='Settings Consumer',
            role=ConsumerProfile.Roles.CONSUMER,
        )
        self.consumer = Consumer.objects.create(
            profile=consumer_profile,
            full_name='Settings Consumer',
            status=Consumer.Statuses.ACTIVE,
        )
        self.billing = BillingRecord.objects.create(
            consumer=self.consumer,
            billing_month=date(2026, 4, 1),
            previous_reading=Decimal('0'),
            current_reading=Decimal('10'),
            rate_per_m3=Decimal('20'),
            amount_paid=Decimal('0'),
            billing_date=date(2026, 4, 5),
            due_date=date(2026, 4, 20),
        )

    def test_settings_changes_recalculate_existing_billings_across_panels(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse('payment_settings'),
            data={
                'rate_per_m3': '35.00',
                'billing_due_days': '10',
                'enable_cash_payments': 'on',
                'enable_online_payments': 'on',
                'notify_by_email': 'on',
                'payment_gateway_notes': 'Updated panel settings',
            },
            follow=True,
        )

        self.billing.refresh_from_db()
        self.assertEqual(self.billing.rate_per_m3, Decimal('35.00'))
        self.assertEqual(self.billing.total_amount, Decimal('1050.00'))
        self.assertEqual(self.billing.due_date, date(2026, 4, 15))
        self.assertContains(response, 'recalculated across all panels')

        self.client.force_login(self.consumer_user)
        consumer_response = self.client.get(reverse('consumer_panel'))
        self.assertEqual(consumer_response.context['billing_records'][0].total_amount, Decimal('1050.00'))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse('admin_panel'), {'month': '2026-04'})
        self.assertEqual(admin_response.context['monthly_billed'], Decimal('1050.00'))
