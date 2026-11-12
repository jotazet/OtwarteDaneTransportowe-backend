"""
Celery application for OtwarteDaneTransportowe.

Broker:  Redis  (CELERY_BROKER_URL)
Backend: Redis  (CELERY_RESULT_BACKEND)

Scheduler: django-celery-beat keeps the schedule in the Django database, so the
           intervals of realtime endpoints change without restarting the worker.
"""

import os

from celery import Celery

# Without DJANGO_SETTINGS_MODULE it fails closed on the production settings; development
# sets it (docker-compose.yml through .env.example, pytest through pytest.ini).
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OtwarteDaneTransportowe.settings_prod')

app = Celery('OtwarteDaneTransportowe')

# All configuration comes from the Django settings (keys prefixed with CELERY_).
app.config_from_object('django.conf:settings', namespace='CELERY')

# Discover tasks.py in every INSTALLED_APPS entry.
app.autodiscover_tasks()
