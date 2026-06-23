from django.conf import settings
from django.core.exceptions import ValidationError


def validate_image_file_size(value):
    """Reject blog images larger than ``MAX_IMAGE_FILE_SIZE_BYTES``."""
    limit = getattr(settings, 'MAX_IMAGE_FILE_SIZE_BYTES', 10 * 1024 * 1024)
    size = getattr(value, 'size', None)
    if size is not None and size > limit:
        raise ValidationError(
            f'Image is too large ({size} bytes). Maximum allowed is {limit} bytes.'
        )
