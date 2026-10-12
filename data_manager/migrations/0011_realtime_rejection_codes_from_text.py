"""Convert legacy realtime rejection texts to codes (ADR 0006).

Before codes, validate_realtime_submission_task wrote prose (partly Polish) to
RealtimeSubmissionHistory.cause. A row is converted only when its whole text
matches formats that task is known to have produced; anything else (reviewer
text, messages of the removed RT validator, raw exception text) stays as is.
The text is kept in ``cause``, so the reverse only clears ``reasons``.

Codes and formats are copied here on purpose: a migration must not change when
data_manager/rejection_reasons.py does.
"""

import re

from django.db import migrations

WHOLE_TEXT = {
    'Brak powiązania ze statycznym zgłoszeniem.': 'realtime.static_submission_missing',
    'Feed statyczny musi być opublikowany przed walidacją realtime.': (
        'realtime.static_submission_not_published'
    ),
    'Brak zdefiniowanych endpointów.': 'realtime.no_endpoints',
}

ENDPOINT_TYPES = {
    'trip_update',
    'vehicle_position',
    'service_alert',
    'sx',
    'sm',
    'vm',
    'et',
    'gm',
    'gbfs',
    'gbfs_versions',
    'system_information',
    'vehicle_types',
    'station_information',
    'station_status',
    'free_bike_status',
    'system_hours',
    'system_alerts',
}

ENDPOINT_MESSAGES = {
    'nie udało się pobrać cache': 'realtime.endpoint_cache_failed',
    'Fetched content is empty.': 'realtime.endpoint_content_empty',
    'Total download time budget exceeded.': 'realtime.endpoint_timeout',
    'odpowiedź nie jest poprawnym JSON': 'realtime.endpoint_content_invalid',
    'Fetched content is not valid JSON (GBFS).': 'realtime.endpoint_content_invalid',
    'Fetched content looks like HTML/XML, not a GTFS-RT protobuf.': (
        'realtime.endpoint_content_invalid'
    ),
    'GTFS-RT feed is missing header.gtfs_realtime_version.': 'realtime.endpoint_content_invalid',
    'Fetched content looks like an HTML page, not a SIRI document.': (
        'realtime.endpoint_content_invalid'
    ),
}

ENDPOINT_MESSAGE_PREFIXES = {
    'blocked URL (': 'realtime.endpoint_url_blocked',
    'Fetched content is not a valid GTFS-RT protobuf feed (': 'realtime.endpoint_content_invalid',
}

ENDPOINT_PART = re.compile(r'(?P<endpoint_type>[a-z_]+): (?P<message>.+)', re.DOTALL)
HTTP_STATUS = re.compile(r'HTTP (?P<status>\d{3})')
TOO_LARGE = re.compile(r'blocked URL \(Response body exceeds the (?P<limit>\d+) byte limit\.\)')


def _endpoint_reason(part: str, protocol: str) -> dict | None:
    match = ENDPOINT_PART.fullmatch(part)
    if not match or match['endpoint_type'] not in ENDPOINT_TYPES:
        return None
    details = {'endpoint_type': match['endpoint_type']}
    message = match['message']

    status = HTTP_STATUS.fullmatch(message)
    if status:
        details['status'] = int(status['status'])
        return {'code': 'realtime.endpoint_http_error', 'details': details}

    too_large = TOO_LARGE.fullmatch(message)
    if too_large:
        details['limit_bytes'] = int(too_large['limit'])
        return {'code': 'realtime.endpoint_too_large', 'details': details}

    code = ENDPOINT_MESSAGES.get(message) or next(
        (c for prefix, c in ENDPOINT_MESSAGE_PREFIXES.items() if message.startswith(prefix)),
        None,
    )
    if code is None:
        return None
    if code == 'realtime.endpoint_content_invalid':
        details['protocol'] = protocol
    return {'code': code, 'details': details}


def reasons_from_text(cause: str, protocol: str) -> list[dict] | None:
    """Reasons for a legacy cause text, or None unless every part is recognised."""
    if cause in WHOLE_TEXT:
        return [{'code': WHOLE_TEXT[cause], 'details': {}}]
    reasons = [_endpoint_reason(part, protocol) for part in cause.split(' | ')]
    if None in reasons:
        return None
    return reasons


def forwards(apps, schema_editor):
    History = apps.get_model('data_manager', 'RealtimeSubmissionHistory')
    rows = (
        History.objects.filter(event_type='rejected', reasons__isnull=True)
        .exclude(cause__isnull=True)
        .exclude(cause='')
        .select_related('submission')
    )
    converted = []
    for row in rows.iterator(chunk_size=500):
        reasons = reasons_from_text(row.cause, row.submission.protocol)
        if reasons is not None:
            row.reasons = reasons
            converted.append(row)
    History.objects.bulk_update(converted, ['reasons'], batch_size=500)


def backwards(apps, schema_editor):
    History = apps.get_model('data_manager', 'RealtimeSubmissionHistory')
    # Only converted rows have both; the text in ``cause`` was never touched.
    # New rows may store an empty cause as '' (not NULL), so exclude it too.
    History.objects.filter(cause__isnull=False, reasons__isnull=False).exclude(cause='').update(
        reasons=None
    )


class Migration(migrations.Migration):
    dependencies = [
        ('data_manager', '0010_realtimesubmissionhistory_reasons'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
