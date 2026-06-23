"""Transparent application-level encryption for sensitive feed credentials.

``EncryptedCharField`` stores its value encrypted at rest (Fernet) while exposing
plaintext to the application. Design goals:

* Backwards compatible: existing plaintext rows are read back unchanged, so the
  field can be rolled out without a data migration. New writes are encrypted.
* Safe in dev/tests: when ``FEED_AUTH_ENCRYPTION_KEY`` is unset, values are stored
  as plaintext (no hard dependency on a key for local work). Production should
  always set the key.
"""
from django.conf import settings
from django.db import models


class EncryptedCharField(models.TextField):
    """A text column whose contents are encrypted at rest with Fernet."""

    PREFIX = 'fernet$'

    def _fernet(self):
        key = getattr(settings, 'FEED_AUTH_ENCRYPTION_KEY', None)
        if not key:
            return None
        from cryptography.fernet import Fernet
        return Fernet(key if isinstance(key, bytes) else key.encode())

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == '':
            return value
        if value.startswith(self.PREFIX):
            return value  # already encrypted (e.g. re-saved DB value)
        fernet = self._fernet()
        if fernet is None:
            return value  # no key configured: store as-is (dev fallback)
        token = fernet.encrypt(value.encode()).decode()
        return self.PREFIX + token

    def from_db_value(self, value, expression, connection):
        return self._decrypt(value)

    def to_python(self, value):
        return self._decrypt(super().to_python(value))

    def _decrypt(self, value):
        if not value or not isinstance(value, str):
            return value
        if not value.startswith(self.PREFIX):
            return value  # legacy plaintext
        fernet = self._fernet()
        if fernet is None:
            return value  # cannot decrypt without the key
        from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
        try:
            return fernet.decrypt(value[len(self.PREFIX):].encode()).decode()
        except InvalidToken:
            return value
