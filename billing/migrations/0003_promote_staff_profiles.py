from django.conf import settings
from django.db import migrations
from django.db.models import Q


def promote_staff_profiles(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split('.')
    User = apps.get_model(app_label, model_name)
    ConsumerProfile = apps.get_model('billing', 'ConsumerProfile')

    for user in User.objects.filter(Q(is_staff=True) | Q(is_superuser=True)):
        full_name = f'{getattr(user, "first_name", "")} {getattr(user, "last_name", "")}'.strip() or user.username
        profile, created = ConsumerProfile.objects.get_or_create(
            user=user,
            defaults={
                'full_name': full_name,
                'email': getattr(user, 'email', '') or '',
                'role': 'admin',
            },
        )

        changed = created
        if profile.role != 'admin':
            profile.role = 'admin'
            changed = True
        if not profile.full_name:
            profile.full_name = full_name
            changed = True
        if getattr(user, 'email', None) and not profile.email:
            profile.email = user.email
            changed = True
        if changed:
            profile.save()


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0002_systemsettings_alter_consumerprofile_role_and_more'),
    ]

    operations = [
        migrations.RunPython(promote_staff_profiles, migrations.RunPython.noop),
    ]
