"""Codes for static (GTFS) validation that could not run (ADR 0006).

Stored in ``StaticFeedEntry.validation_message`` when ``validation_status`` is
``error`` and translated by the frontend. They mean a failure on ODT's side,
not in the feed: the submission keeps its stage and
``manage.py retry_gtfs_validation`` re-runs the validation. Codes are API
contract: listed in docs/api/feeds.md and never changed once released.
"""

FILE_MISSING = 'gtfs_validation.file_missing'
VALIDATOR_UNAVAILABLE = 'gtfs_validation.validator_unavailable'
VALIDATOR_FAILED = 'gtfs_validation.validator_failed'
TIMEOUT = 'gtfs_validation.timeout'
CREDENTIAL_UNDECRYPTABLE = 'gtfs_validation.credential_undecryptable'
