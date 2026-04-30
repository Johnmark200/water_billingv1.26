from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_auditlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='payment_option',
            field=models.CharField(
                choices=[('full', 'Full Payment'), ('partial', 'Partial Payment')],
                default='full',
                max_length=20,
            ),
        ),
    ]
