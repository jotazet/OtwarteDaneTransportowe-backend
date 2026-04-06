from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from blog.models import Post
from data_manager.models import FeedValidationReport


@receiver(post_delete, sender=Post)
def delete_post_image_on_delete(sender, instance, **kwargs):
    """Delete image file from storage when a Post instance is deleted.

    This is a safeguard in case Post.delete() is not called directly
    (e.g. bulk deletes). It complements the model's delete() override.
    """
    if instance.image:
        storage = instance.image.storage
        name = instance.image.name
        if name and storage.exists(name):
            storage.delete(name)


@receiver(pre_save, sender=Post)
def delete_old_post_image_on_change(sender, instance, **kwargs):
    """Delete old image file when a Post.image is changed or cleared.

    - If the image is being replaced: delete the previous file.
    - If the image is being cleared (set to None/"Clear" in admin): delete the previous file.
    """
    if not instance.pk:
        # New object, nothing to delete yet
        return

    try:
        old = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return

    old_file = getattr(old, 'image', None)
    new_file = getattr(instance, 'image', None)

    # If file didn't change, nothing to do
    if not old_file:
        return
    if old_file == new_file:
        return

    storage = old_file.storage
    name = old_file.name
    if name and storage.exists(name):
        storage.delete(name)


@receiver(post_delete, sender=FeedValidationReport)
def delete_validation_report_file_on_delete(sender, instance, **kwargs):
    """Delete validator report artifact from storage when the report row is deleted."""
    if instance.report_file:
        storage = instance.report_file.storage
        name = instance.report_file.name
        if name and storage.exists(name):
            storage.delete(name)
