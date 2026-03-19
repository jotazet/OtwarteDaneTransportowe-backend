"""
Celery application for OtwarteDaneTransportowe.

Broker:  Redis  (CELERY_BROKER_URL)
Backend: Redis  (CELERY_RESULT_BACKEND)

Scheduler: django-celery-beat przechowuje harmonogram w bazie danych Django,
           dzięki czemu interwały dla poszczególnych endpointów RT są dynamiczne
           i nie wymagają restartu workera po zmianie.
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'OtwarteDaneTransportowe.settings')

app = Celery('OtwarteDaneTransportowe')

# Całą konfigurację czytamy z settings.py (klucze z prefiksem CELERY_)
app.config_from_object('django.conf:settings', namespace='CELERY')

# Automatycznie wykrywaj tasks.py we wszystkich INSTALLED_APPS
app.autodiscover_tasks()

