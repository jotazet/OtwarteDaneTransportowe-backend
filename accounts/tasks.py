from celery import shared_task
from django.core.management import call_command


@shared_task(name='accounts.flush_expired_tokens')
def flush_expired_tokens_task():
    """Delete expired outstanding and blacklisted JWTs (simplejwt keeps them forever)."""
    call_command('flushexpiredtokens')
