"""Codes for automatic rejections of realtime and static submissions (ADR 0006).

Stored in ``RealtimeSubmissionHistory.reasons`` and
``FeedSubmissionHistory.reasons`` as a list of
``{"code": ..., "details": {...}}`` and translated by the frontend. Codes are
API contract: listed in docs/api/feeds.md and never changed once released.
"""

STATIC_SUBMISSION_MISSING = 'realtime.static_submission_missing'
STATIC_SUBMISSION_NOT_PUBLISHED = 'realtime.static_submission_not_published'
NO_ENDPOINTS = 'realtime.no_endpoints'

# Per-endpoint codes; details always carry ``endpoint_type``.
ENDPOINT_HTTP_ERROR = 'realtime.endpoint_http_error'  # + ``status``
ENDPOINT_CONTENT_EMPTY = 'realtime.endpoint_content_empty'
ENDPOINT_CONTENT_INVALID = 'realtime.endpoint_content_invalid'  # + ``protocol``
ENDPOINT_TOO_LARGE = 'realtime.endpoint_too_large'  # + ``limit_bytes``
ENDPOINT_URL_BLOCKED = 'realtime.endpoint_url_blocked'
ENDPOINT_TIMEOUT = 'realtime.endpoint_timeout'
ENDPOINT_UNREACHABLE = 'realtime.endpoint_unreachable'
ENDPOINT_FETCH_FAILED = 'realtime.endpoint_fetch_failed'
ENDPOINT_CACHE_FAILED = 'realtime.endpoint_cache_failed'

# Static (GTFS) validation; validation that could not run is in validation_errors.py.
GTFS_FEED_INVALID = 'gtfs_validation.feed_invalid'  # + ``error_count``, ``notices``
GTFS_DOWNLOAD_HTTP_ERROR = 'gtfs_validation.download_http_error'  # + ``status``
GTFS_DOWNLOAD_TOO_LARGE = 'gtfs_validation.download_too_large'  # + ``limit_bytes``
GTFS_DOWNLOAD_URL_BLOCKED = 'gtfs_validation.download_url_blocked'
GTFS_DOWNLOAD_TIMEOUT = 'gtfs_validation.download_timeout'
GTFS_DOWNLOAD_UNREACHABLE = 'gtfs_validation.download_unreachable'
GTFS_DOWNLOAD_FAILED = 'gtfs_validation.download_failed'


def rejection_reason(code: str, **details) -> dict:
    return {'code': code, 'details': details}
