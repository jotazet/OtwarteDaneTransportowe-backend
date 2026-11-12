"""Application-level encryption of feed source credentials.

``EncryptedCharField`` stores its value encrypted at rest (Fernet) while exposing
plaintext to the application:

* Values assigned by the application are always encrypted with
  ``FEED_AUTH_ENCRYPTION_KEY``. Only values read from the database carry the
  ``fernet$`` prefix; input that merely starts with it is encrypted like any other.
* Stored tokens are decrypted with ``FEED_AUTH_ENCRYPTION_KEY`` or any of
  ``FEED_AUTH_ENCRYPTION_OLD_KEYS`` (``MultiFernet``), so the key can be rotated:
  ``manage.py rotate_feed_auth_key`` re-encrypts every row with the current key.
* A stored token none of the keys decrypts is read as an ``UndecryptableValue``,
  never as text, so it cannot be sent upstream as if it were the credential.
  ``data_manager.scheduler._build_auth_headers`` refuses it with
  ``CredentialUndecryptable``; saving the row again keeps the stored token.
* Rows written before encryption was enabled (no prefix) are read back unchanged
  and encrypted on their next save or by the rotate command.
* Without a key (development only: ``settings_prod`` requires one) values are
  stored as plaintext and a warning is logged once.
"""

import functools
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import models

logger = logging.getLogger(__name__)

PREFIX = 'fernet$'
CREDENTIAL_UNDECRYPTABLE = 'fetch.credential_undecryptable'


class UndecryptableValue:
    """A stored credential that none of the configured keys can decrypt."""

    __slots__ = ('stored',)

    def __init__(self, stored: str):
        self.stored = stored

    def __repr__(self) -> str:
        return '<UndecryptableValue>'


class CredentialUndecryptable(Exception):
    """Raised instead of using an ``UndecryptableValue`` as a credential."""

    def __init__(self):
        super().__init__(CREDENTIAL_UNDECRYPTABLE)


@functools.cache
def _log_once(level: int, message: str) -> None:
    logger.log(level, message)


@functools.lru_cache(maxsize=4)
def _multi_fernet(key: str, old_keys: tuple[str, ...]) -> MultiFernet:
    return MultiFernet([Fernet(k) for k in (key, *old_keys)])


def _fernet() -> MultiFernet | None:
    key = settings.FEED_AUTH_ENCRYPTION_KEY
    if not key:
        return None
    return _multi_fernet(key, tuple(settings.FEED_AUTH_ENCRYPTION_OLD_KEYS))


class EncryptedCharField(models.TextField):
    """A text column whose contents are encrypted at rest with Fernet."""

    def get_prep_value(self, value):
        if isinstance(value, UndecryptableValue):
            return value.stored  # keep it: it decrypts again once the key is fixed
        value = super().get_prep_value(value)
        if not value:
            return value
        fernet = _fernet()
        if fernet is None:
            _log_once(
                logging.WARNING,
                'FEED_AUTH_ENCRYPTION_KEY is not set: feed credentials are stored as plaintext',
            )
            return value
        return PREFIX + fernet.encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if not value or not value.startswith(PREFIX):
            return value  # NULL, empty, or written before encryption was enabled
        fernet = _fernet()
        if fernet is not None:
            try:
                return fernet.decrypt(value[len(PREFIX) :].encode()).decode()
            except InvalidToken:
                pass
        _log_once(
            logging.ERROR,
            'A stored feed credential cannot be decrypted with FEED_AUTH_ENCRYPTION_KEY or '
            f'FEED_AUTH_ENCRYPTION_OLD_KEYS; fetches that need it fail with {CREDENTIAL_UNDECRYPTABLE}',
        )
        return UndecryptableValue(value)

    def to_python(self, value):
        # Model.full_clean() runs this on the attribute; a str() would replace the
        # stored token with its repr on the next save.
        if isinstance(value, UndecryptableValue):
            return value
        return super().to_python(value)
