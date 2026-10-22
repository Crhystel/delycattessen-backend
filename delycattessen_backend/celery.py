import os 
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'delycattessen_backend.settings')
app= Celery('delycattessen_backend')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()