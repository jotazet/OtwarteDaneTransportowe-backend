from django.db import migrations
from django.utils import timezone


def schedule_daily_retry(apps, schema_editor):
    """Give feeds auto-paused before the daily retry existed a retry time.

    They were stored with next_fetch_after=null, which the dispatchers never
    pick up. Due at once: each is tried at the next dispatcher tick and, if it
    fails again, once a day from then on. Not reversed: the old code ignores
    next_fetch_after of an auto-paused feed.
    """
    now = timezone.now()
    for model_name in ('StaticFeedEntry', 'RealtimeEndpointRT'):
        apps.get_model('data_manager', model_name).objects.filter(
            fetch_status='auto_paused', next_fetch_after__isnull=True
        ).update(next_fetch_after=now)


class Migration(migrations.Migration):
    dependencies = [
        ('data_manager', '0016_static_entry_cache_validators'),
    ]

    operations = [
        migrations.RunPython(schedule_daily_retry, migrations.RunPython.noop),
    ]
