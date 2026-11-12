import contextlib
import os

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from planner.models import PlannerFeed
from planner.pipeline import snapshot_path


@receiver(post_delete, sender=PlannerFeed)
def remove_snapshot_of_deleted_feed(sender, instance, **kwargs):
    """Delete the feed's last good version once its deletion commits.

    Only the pipeline reads snapshots, to fall back to a feed's last good version;
    the serving graph and the one kept for a rollback hold their own copies. A
    deletion that rolls back keeps the file.
    """
    snapshot = snapshot_path(instance)
    transaction.on_commit(lambda: _remove(snapshot, f'{snapshot}.tmp'), robust=True)


def _remove(*paths) -> None:
    for path in paths:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
