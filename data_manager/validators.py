from django.conf import settings
from django.core.exceptions import ValidationError


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
