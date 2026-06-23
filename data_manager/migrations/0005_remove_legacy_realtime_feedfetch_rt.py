# Generated manually — breaking change: legacy RealtimeFeedEntry/RealtimeEndpoint removed.

import data_manager.models
import django.db.models.deletion
from django.db import migrations, models


def delete_invalid_static_and_fetch_errors(apps, schema_editor):
    FeedSubmission = apps.get_model('data_manager', 'FeedSubmission')
    FeedFetchError = apps.get_model('data_manager', 'FeedFetchError')
    FeedFetchError.objects.all().delete()
    FeedSubmission.objects.filter(data_type__in=['gbfs', 'siri', 'gtfs_rt']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('data_manager', '0004_realtime_submission_flow'),
    ]

    operations = [
        migrations.RunPython(delete_invalid_static_and_fetch_errors, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='feedfetcherror',
            name='endpoint',
        ),
        migrations.DeleteModel(
            name='RealtimeEndpoint',
        ),
        migrations.DeleteModel(
            name='RealtimeFeedEntry',
        ),
        migrations.AlterField(
            model_name='feedsubmission',
            name='data_type',
            field=models.CharField(
                choices=[('gtfs', 'GTFS'), ('netex', 'NeTEx'), ('other', 'Other')],
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='realtimeendpointrt',
            name='cached_at',
            field=models.DateTimeField(blank=True, help_text='When the cached copy was last downloaded.', null=True),
        ),
        migrations.AddField(
            model_name='realtimeendpointrt',
            name='cached_file',
            field=models.FileField(
                blank=True,
                help_text='Server-cached copy when hide_original=True.',
                null=True,
                storage=data_manager.models.OverwriteStorage(),
                upload_to=data_manager.models.realtime_feed_cached_file_path,
            ),
        ),
        migrations.AddField(
            model_name='feedfetcherror',
            name='endpoint_rt',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='fetch_errors',
                to='data_manager.realtimeendpointrt',
            ),
        ),
    ]
