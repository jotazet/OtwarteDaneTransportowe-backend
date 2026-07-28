import json

from django.conf import settings
from django.core.exceptions import ValidationError


class InvalidFeedContent(Exception):
    """Fetched bytes do not look like the expected feed format.

    Raised by the fetch helpers BEFORE the cached file is overwritten, so a
    published feed keeps serving its last good copy when the upstream starts
    returning garbage (HTML error pages, truncated bodies, ...).
    """


# Standard ZIP local-file-header magics (regular, empty archive, spanned).
_ZIP_MAGICS = (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')


def looks_like_zip(data: bytes) -> bool:
    return data[:4] in _ZIP_MAGICS


def _looks_like_html(data: bytes) -> bool:
    head = data.lstrip()[:15].lower()
    return head.startswith(b'<html') or head.startswith(b'<!doctype')


def validate_static_feed_content(data_type: str, content: bytes) -> None:
    """Sanity-check fetched static feed bytes before caching them.

    GTFS is by definition a ZIP archive; NeTEx is XML, commonly distributed
    as either a ZIP or a plain XML document. ``data_type='other'`` is left
    unchecked apart from the empty/HTML guards.
    """
    if not content:
        raise InvalidFeedContent('Fetched content is empty.')
    if _looks_like_html(content):
        raise InvalidFeedContent('Fetched content looks like an HTML page, not a feed.')
    if data_type == 'gtfs' and not looks_like_zip(content):
        raise InvalidFeedContent('Fetched content is not a ZIP archive (GTFS requires a ZIP).')
    if data_type == 'netex' and not (looks_like_zip(content) or content.lstrip()[:1] == b'<'):
        raise InvalidFeedContent('Fetched content is neither a ZIP archive nor XML (NeTEx).')


def validate_realtime_feed_content(protocol: str, content: bytes) -> None:
    """Sanity-check fetched realtime feed bytes before caching them.

    GTFS-RT is binary protobuf (never starts with '<'); SIRI is XML (only an
    HTML page is rejected); every GBFS file is a JSON document.
    """
    if not content:
        raise InvalidFeedContent('Fetched content is empty.')
    if protocol == 'gbfs':
        try:
            json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise InvalidFeedContent('Fetched content is not valid JSON (GBFS).') from exc
    elif protocol == 'gtfs_rt':
        if content.lstrip()[:1] == b'<':
            raise InvalidFeedContent('Fetched content looks like HTML/XML, not a GTFS-RT protobuf.')
    elif protocol == 'siri':
        if _looks_like_html(content):
            raise InvalidFeedContent('Fetched content looks like an HTML page, not a SIRI document.')


def validate_uploaded_gtfs_zip(file_obj) -> None:
    """Reject a user-uploaded GTFS file that is not a ZIP archive.

    Reads only the first 4 bytes and rewinds, so the subsequent storage write
    still sees the full stream.
    """
    head = file_obj.read(4)
    file_obj.seek(0)
    if not looks_like_zip(head):
        raise ValidationError('Uploaded GTFS file is not a ZIP archive.')


def validate_feed_file_size(value):
    """Reject feed files larger than ``MAX_FEED_FILE_SIZE_BYTES``.

    Guards against disk-fill / zip-bomb style uploads. Also copied into the DRF
    serializer field automatically (ModelSerializer propagates model validators).
    """
    limit = getattr(settings, 'MAX_FEED_FILE_SIZE_BYTES', 200 * 1024 * 1024)
    size = getattr(value, 'size', None)
    if size is not None and size > limit:
        raise ValidationError(
            f'File is too large ({size} bytes). Maximum allowed is {limit} bytes.'
        )
