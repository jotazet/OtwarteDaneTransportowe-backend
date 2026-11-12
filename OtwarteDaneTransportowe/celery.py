"""
Celery application for OtwarteDaneTransportowe.

Broker:  Redis  (CELERY_BROKER_URL)
Results: none   (CELERY_TASK_IGNORE_RESULT, no result backend)

Scheduler: django-celery-beat's database scheduler (CELERY_BEAT_SCHEDULER). The
           fixed dispatchers come from CELERY_BEAT_SCHEDULE; the planner graph
           build is a PeriodicTask edited in the admin panel. Realtime endpoints
           are not on the beat schedule: each one reschedules its own next fetch
           (data_manager/tasks.py).
"""

import os

from celery import Celery

# Without DJANGO_SETTINGS_MODULE it fails closed on the production settings.
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OtwarteDaneTransportowe.settings_prod')

app = Celery('OtwarteDaneTransportowe')

# All configuration comes from the Django settings (keys prefixed with CELERY_).
app.config_from_object('django.conf:settings', namespace='CELERY')

# Discover tasks.py in every INSTALLED_APPS entry.
app.autodiscover_tasks()
