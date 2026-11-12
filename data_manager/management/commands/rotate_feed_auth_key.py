"""
Re-encrypt every stored feed credential with the current FEED_AUTH_ENCRYPTION_KEY.

Run after moving the previous key into FEED_AUTH_ENCRYPTION_OLD_KEYS (docs/deployment.md,
"Rotating FEED_AUTH_ENCRYPTION_KEY"). Rows written before encryption was enabled are
encrypted too. A credential none of the keys decrypts is left as it is and listed by
model and id; the command then fails, and the old keys must stay until it is resolved.

Usage:
    python manage.py rotate_feed_auth_key
"""

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from data_manager.fields import EncryptedCharField, UndecryptableValue


class Command(BaseCommand):
    help = 'Re-encrypt stored feed credentials with the current FEED_AUTH_ENCRYPTION_KEY.'

    def handle(self, *args, **options):
        if not settings.FEED_AUTH_ENCRYPTION_KEY:
            raise CommandError('FEED_AUTH_ENCRYPTION_KEY is not set.')
        rotated, undecryptable = 0, []
        for model in apps.get_models():
            for field in model._meta.local_concrete_fields:
                if not isinstance(field, EncryptedCharField):
                    continue
                with transaction.atomic():
                    rows = (
                        model.objects.select_for_update()
                        .exclude(**{f'{field.attname}__isnull': True})
                        .exclude(**{field.attname: ''})
                        .values_list('pk', field.attname)
                    )
                    for pk, value in rows:
                        if isinstance(value, UndecryptableValue):
                            undecryptable.append(f'{model._meta.label}.{field.name} id={pk}')
                            continue
                        model.objects.filter(pk=pk).update(**{field.attname: value})
                        rotated += 1
        self.stdout.write(f'Re-encrypted {rotated} credential(s).')
        if undecryptable:
            raise CommandError(
                'Cannot decrypt with the configured keys: ' + ', '.join(undecryptable)
            )
