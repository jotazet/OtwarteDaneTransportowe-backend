from django.core.cache import cache
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import (
    COMPLETED_RT_IDS_CACHE_KEY,
    COMPLETED_STATIC_IDS_CACHE_KEY,
    FeedSubmissionHistory,
    RealtimeSubmissionHistory,
    StaticFeedEntry,
)
from .tasks import validate_gtfs_feed_task


@receiver([post_save, post_delete], sender=FeedSubmissionHistory)
def invalidate_completed_static_cache(sender, **kwargs):
    """Stage changed -> drop cached published static submission IDs."""
    cache.delete(COMPLETED_STATIC_IDS_CACHE_KEY)


@receiver([post_save, post_delete], sender=RealtimeSubmissionHistory)
def invalidate_completed_realtime_cache(sender, **kwargs):
    """Stage changed -> drop cached published realtime submission IDs."""
    cache.delete(COMPLETED_RT_IDS_CACHE_KEY)


def _file_field_name(field) -> str | None:
    return getattr(field, 'name', None) or None


@receiver(pre_save, sender=StaticFeedEntry)
def capture_static_entry_file_state(sender, instance, **kwargs):
    """Store pre-save file paths so post_save can detect real changes."""
    if not instance.pk:
        instance._prev_file_name = None
        instance._prev_cached_file_name = None
        return
    try:
        old = sender.objects.only('file', 'cached_file').get(pk=instance.pk)
    except sender.DoesNotExist:
        instance._prev_file_name = None
        instance._prev_cached_file_name = None
        return
    instance._prev_file_name = _file_field_name(old.file)
    instance._prev_cached_file_name = _file_field_name(old.cached_file)


@receiver(post_save, sender=StaticFeedEntry)
def trigger_gtfs_validation(sender, instance, created, **kwargs):
    """Queue GTFS validation when file or cached_file is set or changed."""
    update_fields = kwargs.get('update_fields')
    if update_fields is not None:
        watched = {'file', 'cached_file'}
        if not (set(update_fields) & watched):
            return

    if created:
        if instance.file or instance.cached_file:
            validate_gtfs_feed_task.delay(instance.id)
        return

    prev_file = getattr(instance, '_prev_file_name', None)
    prev_cached = getattr(instance, '_prev_cached_file_name', None)
    new_file = _file_field_name(instance.file)
    new_cached = _file_field_name(instance.cached_file)

    if prev_file != new_file or prev_cached != new_cached:
        if instance.file or instance.cached_file:
            validate_gtfs_feed_task.delay(instance.id)
