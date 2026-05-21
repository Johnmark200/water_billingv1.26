from django.core.management.base import BaseCommand

from billing.models import Consumer
from billing.services import refresh_consumer_account_status


class Command(BaseCommand):
    help = 'Refresh overdue warnings, disconnection countdowns, and admin escalation for consumer accounts.'

    def handle(self, *args, **options):
        monitored = 0
        escalated = 0

        for consumer in Consumer.objects.all().order_by('full_name'):
            refresh_consumer_account_status(consumer)
            consumer.refresh_from_db()
            monitored += 1
            if consumer.account_status == Consumer.AccountStatuses.FOR_DISCONNECTION:
                escalated += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Overdue monitoring complete. Processed {monitored} consumers; {escalated} are for disconnection.'
            )
        )
