"""Deleting a PlannerFeed removes its snapshot (last good version) from disk."""

import os
from pathlib import Path

import pytest
from django.db import transaction

from planner import pipeline, services
from planner.models import GraphBuild

pytestmark = pytest.mark.django_db


def build_now() -> GraphBuild:
    build = services.request_build(GraphBuild.TRIGGER_MANUAL)
    pipeline.run_build(build.id)
    build.refresh_from_db()
    return build


def test_deleting_a_feed_removes_only_its_snapshot_and_keeps_the_served_graph(
    make_feed, fake_otp, django_capture_on_commit_callbacks
):
    krk, waw = make_feed('krk'), make_feed('waw')
    build = build_now()
    krk_snapshot, waw_snapshot = (
        Path(pipeline.snapshot_path(krk)),
        Path(pipeline.snapshot_path(waw)),
    )
    assert krk_snapshot.exists() and waw_snapshot.exists()

    with django_capture_on_commit_callbacks(execute=True):
        krk.delete()

    assert not krk_snapshot.exists()
    assert waw_snapshot.exists()
    assert os.path.exists(os.path.join(pipeline.graph_dir(build), 'graph.obj'))


def test_deleting_the_submission_removes_the_snapshot_of_its_feed(
    make_feed, planner_data, django_capture_on_commit_callbacks
):
    feed = make_feed('krk')
    snapshot = Path(pipeline.snapshot_path(feed))
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b'zip')
    Path(f'{snapshot}.tmp').write_bytes(b'zip')  # left by a crash before the rename

    with django_capture_on_commit_callbacks(execute=True):
        feed.submission.delete()

    assert not snapshot.exists()
    assert not Path(f'{snapshot}.tmp').exists()


def test_rolled_back_deletion_keeps_the_snapshot(
    make_feed, planner_data, django_capture_on_commit_callbacks
):
    feed = make_feed('krk')
    snapshot = Path(pipeline.snapshot_path(feed))
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b'zip')

    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError), transaction.atomic():
            feed.delete()
            raise RuntimeError

    assert snapshot.exists()


def test_deleting_a_feed_that_never_built_is_fine(
    make_feed, planner_data, django_capture_on_commit_callbacks
):
    feed = make_feed('krk')

    with django_capture_on_commit_callbacks(execute=True):
        feed.delete()

    assert not os.path.exists(pipeline.snapshot_path(feed))
