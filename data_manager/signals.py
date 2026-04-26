from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import StaticFeedEntry
from .tasks import validate_gtfs_feed_task


@receiver(post_save, sender=StaticFeedEntry)
def trigger_gtfs_validation(sender, instance, created, **kwargs):
    # Avoid task storms: only trigger when the underlying file fields change.
    update_fields = kwargs.get('update_fields')
    if update_fields is not None:
        watched = {'file', 'cached_file'}
        if not (set(update_fields) & watched):
            return

    if created:
        if instance.file or instance.cached_file:
            validate_gtfs_feed_task.delay(instance.id)
        return

    if not instance.pk:
        return

    try:
        old = sender.objects.only('file', 'cached_file').get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    old_file = getattr(old.file, 'name', None)
    new_file = getattr(instance.file, 'name', None)
    old_cached = getattr(old.cached_file, 'name', None)
    new_cached = getattr(instance.cached_file, 'name', None)

    if old_file != new_file or old_cached != new_cached:
        if instance.file or instance.cached_file:
            validate_gtfs_feed_task.delay(instance.id)
