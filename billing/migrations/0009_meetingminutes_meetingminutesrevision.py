from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0008_payment_payment_option'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MeetingMinutes',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=255)),
                ('meeting_date', models.DateField(default=django.utils.timezone.localdate)),
                ('meeting_time', models.TimeField(blank=True, null=True)),
                ('location', models.CharField(blank=True, max_length=255)),
                ('attendees', models.TextField(blank=True)),
                ('agenda', models.TextField(blank=True)),
                ('discussion_points', models.TextField(blank=True)),
                ('resolutions', models.TextField(blank=True)),
                ('action_items', models.TextField(blank=True)),
                ('additional_notes', models.TextField(blank=True)),
                ('status', models.CharField(choices=[('draft', 'Draft'), ('approved', 'Approved')], default='draft', max_length=20)),
                ('approved_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('secretary', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='secretary_meeting_minutes', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-meeting_date', '-updated_at'],
            },
        ),
        migrations.CreateModel(
            name='MeetingMinutesRevision',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision_number', models.PositiveIntegerField()),
                ('change_summary', models.CharField(blank=True, max_length=255)),
                ('changed_fields', models.JSONField(blank=True, default=list)),
                ('snapshot', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('edited_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='meeting_minutes_revisions', to=settings.AUTH_USER_MODEL)),
                ('meeting_minutes', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='revisions', to='billing.meetingminutes')),
            ],
            options={
                'ordering': ['-revision_number', '-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='meetingminutesrevision',
            constraint=models.UniqueConstraint(fields=('meeting_minutes', 'revision_number'), name='unique_meeting_minutes_revision_number'),
        ),
    ]
