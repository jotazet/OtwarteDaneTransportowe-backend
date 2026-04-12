from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import StaticFeedEntry
from .tasks import validate_gtfs_feed_task


@receiver(post_save, sender=StaticFeedEntry)
def trigger_gtfs_validation(sender, instance, created, **kwargs):
    if instance.file or instance.cached_file:
        validate_gtfs_feed_task.delay(instance.id)
