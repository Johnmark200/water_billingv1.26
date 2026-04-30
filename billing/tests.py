from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from .forms import MeterReadingForm, SignUpForm
from .models import BillingRecord, Consumer, ConsumerProfile, MeterReading, Payment
from .services import build_consumer_payment_month_choices


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

    def test_rejects_lower_than_previous_reading(self):
        form = MeterReadingForm(
            data={
                'consumer': self.consumer.id,
                'reading_date': '2026-04-30',
                'current_reading': '90',
                'notes': '',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn(
            'Current reading cannot be lower than the previous reading shown for this consumer.',
            form.errors['current_reading'],
        )

    def test_rejects_unrealistic_reading_jump(self):
        form = MeterReadingForm(
            data={
                'consumer': self.consumer.id,
                'reading_date': '2026-04-30',
                'current_reading': '700',
                'notes': '',
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn('The reading jump looks unrealistic (600', form.errors['current_reading'][0])
