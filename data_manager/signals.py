from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import StaticFeedEntry, RealtimeFeedEntry
from .tasks import validate_gtfs_feed_task, validate_gtfs_rt_task

@receiver(post_save, sender=StaticFeedEntry)
def trigger_gtfs_validation(sender, instance, created, **kwargs):
    """
    Trigger validation when a StaticFeedEntry is created or updated with a file.
    """
    if instance.file or instance.cached_file:
        validate_gtfs_feed_task.delay(instance.id)

@receiver(post_save, sender=RealtimeFeedEntry)
def trigger_gtfs_rt_validation(sender, instance, created, **kwargs):
    """
    Trigger validation when a RealtimeFeedEntry is created or updated.
    """
    if instance.protocol == RealtimeFeedEntry.PROTOCOL_GTFS_RT:
        validate_gtfs_rt_task.delay(instance.id)