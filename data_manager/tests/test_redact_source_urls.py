"""Source URLs reach logs and stored error messages without their credentials."""

import logging
from datetime import time

import pytest
import requests
from urllib3 import HTTPSConnectionPool
from urllib3.exceptions import MaxRetryError

from cases.models import TransportOrganization
from data_manager.models import FeedFetchError, FeedSubmission, StaticFeedEntry
from data_manager.net_security import redact_url, redact_urls
from data_manager.scheduler import FETCH_PERMANENT, FETCH_TRANSIENT, _fetch_static_entry
from OtwarteDaneTransportowe.auth_roles import ROLE_DATA_PROVIDER

SOURCE = 'https://feeduser:s3cret-pass@example.org:8443/gtfs.zip?token=s3cret-token#frag'
SECRETS = ('feeduser', 's3cret-pass', 's3cret-token')


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        (SOURCE, 'https://example.org:8443/gtfs.zip'),
        ('https://example.org/rt.pb?key=abc&format=pb', 'https://example.org/rt.pb'),
        ('https://user@zażółć.pl/feed.zip?token=x', 'https://zażółć.pl/feed.zip'),
        ('http://[2001:db8::1]:8080/a?b=c', 'http://[2001:db8::1]:8080/a'),
        ('https://example.org/gtfs.zip', 'https://example.org/gtfs.zip'),
        ('/gtfs.zip?token=x', '/gtfs.zip'),
        ('http://[::1/broken', '<invalid URL>'),
    ],
)
def test_redact_url(url, expected):
    assert redact_url(url) == expected


def test_redact_urls_in_request_error_texts():
    http_error = f'404 Client Error: Not Found for url: {SOURCE}'
    assert redact_urls(http_error) == (
        '404 Client Error: Not Found for url: https://example.org:8443/gtfs.zip'
    )
    pool_error = str(MaxRetryError(HTTPSConnectionPool('example.org'), '/gtfs.zip?token=t0k'))
    assert 't0k' not in redact_urls(pool_error)
    assert 'with url: /gtfs.zip (Caused by' in redact_urls(pool_error)
    assert redact_urls('Read timed out. (read timeout=60)') == 'Read timed out. (read timeout=60)'


@pytest.fixture
def entry(make_user):
    owner = make_user(ROLE_DATA_PROVIDER, 'owner')
    org = TransportOrganization.objects.create(region='R', transport_organization='Org')
    submission = FeedSubmission.objects.create(
        transport_organization=org, submitted_by=owner, data_type='gtfs', name='Feed'
    )
    return StaticFeedEntry.objects.create(
        submission=submission, url=SOURCE, hide_original=True, download_time_1=time(3, 0)
    )


def _http_error():
    response = requests.Response()
    response.status_code = 404
    response.reason = 'Not Found'
    response.url = SOURCE
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        return exc


def _connection_error():
    return requests.ConnectionError(
        MaxRetryError(HTTPSConnectionPool('example.org', 8443), '/gtfs.zip?token=s3cret-token')
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ('make_error', 'outcome'),
    [(_http_error, FETCH_PERMANENT), (_connection_error, FETCH_TRANSIENT)],
)
def test_fetch_failure_stores_and_logs_no_secret(entry, monkeypatch, caplog, make_error, outcome):
    def failing_download(url, **kwargs):
        raise make_error()

    monkeypatch.setattr('data_manager.scheduler.safe_download', failing_download)

    with caplog.at_level(logging.INFO, logger='data_manager'):
        assert _fetch_static_entry(entry) == outcome

    entry.refresh_from_db()
    error = FeedFetchError.objects.get(static_entry=entry)
    assert 'example.org' in error.message
    stored = (error.message, entry.fetch_pause_reason, caplog.text)
    assert not any(secret in text for text in stored for secret in SECRETS)
