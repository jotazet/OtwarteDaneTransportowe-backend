from django.db.models.signals import post_delete, pre_save
from django.dispatch import receiver

from blog.models import Post
from data_manager.models import FeedValidationReport, RealtimeEndpointRT, StaticFeedEntry


def _delete_file(field_file) -> None:
    """Delete a FieldFile's storage object if it exists (no-op when empty)."""
    name = getattr(field_file, 'name', None)
    if not name:
        return
    storage = field_file.storage
    if storage.exists(name):
        storage.delete(name)


def _delete_replaced_files(sender, instance, field_names, update_fields) -> None:
    """Delete storage files whose model field is being repointed to a new name.

    Only fields listed in ``update_fields`` are considered (when given): the
    in-memory instance may hold a stale copy of fields it is NOT saving — e.g.
    ``cached_file`` refreshed concurrently by the fetch scheduler — and
    deleting based on that stale diff would remove a live file. Same-name
    replacement is skipped too: OverwriteStorage rewrites the path in place.
    """
    if not instance.pk:
        return
    if update_fields is not None:
        field_names = [f for f in field_names if f in update_fields]
    if not field_names:
        return
    try:
        old = sender.objects.only(*field_names).get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    for field_name in field_names:
        old_file = getattr(old, field_name)
        old_name = getattr(old_file, 'name', None)
        new_name = getattr(getattr(instance, field_name), 'name', None)
        if old_name and old_name != new_name:
            _delete_file(old_file)


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


@receiver(post_delete, sender=StaticFeedEntry)
def delete_static_entry_files_on_delete(sender, instance, **kwargs):
    """Reclaim the uploaded file, the cached copy and the validation report.

    The validation_report FK is SET_NULL, so without this the report row (and
    its report.json) would be orphaned whenever an entry or its submission is
    deleted. Cascade deletes emit post_delete per instance, so this also
    covers FeedSubmission deletion.
    """
    _delete_file(instance.file)
    _delete_file(instance.cached_file)
    if instance.validation_report_id:
        instance.validation_report.delete()


@receiver(pre_save, sender=StaticFeedEntry)
def delete_replaced_static_entry_files(sender, instance, update_fields=None, **kwargs):
    _delete_replaced_files(sender, instance, ('file', 'cached_file'), update_fields)


@receiver(post_delete, sender=RealtimeEndpointRT)
def delete_rt_endpoint_cache_on_delete(sender, instance, **kwargs):
    """Reclaim the cached RT file (covers the delete+recreate endpoint update path)."""
    _delete_file(instance.cached_file)


@receiver(pre_save, sender=RealtimeEndpointRT)
def delete_replaced_rt_endpoint_cache(sender, instance, update_fields=None, **kwargs):
    _delete_replaced_files(sender, instance, ('cached_file',), update_fields)
