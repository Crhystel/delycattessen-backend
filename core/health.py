"""System health checks for the superadmin "Estado del sistema" page.

Pure infrastructure: it imports no app, only Django, kombu and the standard library.
Every check is self-contained, time-boxed and never raises: a broken component is
reported as an error instead of breaking the page. The SMTP check logs in to the mail
server (without sending anything), so it only runs on demand.
"""
import os
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass

import django
from django.conf import settings
from django.core.cache import cache
from django.core.mail import get_connection
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

OK, WARNING, ERROR, INFO = 'ok', 'warning', 'error', 'info'
LAST_RESULT_CACHE_KEY = 'core.health.last'
LAST_RESULT_TTL = 600


@dataclass
class Check:
    name: str
    status: str
    detail: str
    elapsed_ms: int = 0


def _timed(name, func):
    started = time.perf_counter()
    try:
        status, detail = func()
    except Exception as exc:  # noqa: BLE001 - a failing probe is a result, not a crash
        status, detail = ERROR, f'{type(exc).__name__}: {exc}'[:200]
    return Check(name, status, detail, int((time.perf_counter() - started) * 1000))


# ------------------------------------------------------------------ probes
def _database():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return OK, f'{connection.vendor} responde'


def _cache():
    key = f'core.health.{uuid.uuid4().hex}'
    cache.set(key, 'ok', 5)
    if cache.get(key) != 'ok':
        return ERROR, 'la caché no devolvió lo que se guardó'
    cache.delete(key)
    backend = settings.CACHES['default']['BACKEND']
    if 'locmem' in backend.lower():
        return WARNING, ('Caché en memoria de cada proceso (falta REDIS_URL): los códigos de recuperación '
                         'y los QR fallan con varios procesos.')
    return OK, backend.rsplit('.', 1)[-1]


def _broker_connection():
    from kombu import Connection
    return Connection(settings.CELERY_BROKER_URL, connect_timeout=3)


def _broker():
    with _broker_connection() as conn:
        conn.ensure_connection(max_retries=1, timeout=3)
    return OK, 'RabbitMQ acepta conexiones'


def _worker_queue():
    """Celery remote control is disabled in this project, so the proof that a worker is
    alive is that the queue has a consumer."""
    queue = getattr(settings, 'CELERY_TASK_DEFAULT_QUEUE', 'celery')
    with _broker_connection() as conn:
        conn.ensure_connection(max_retries=1, timeout=3)
        result = conn.default_channel.queue_declare(queue=queue, passive=True)
    messages, consumers = result.message_count, result.consumer_count
    if consumers == 0:
        return ERROR, f'Sin workers escuchando la cola «{queue}» ({messages} mensaje(s) esperando).'
    if messages > 100:
        return WARNING, f'{consumers} worker(s), pero hay {messages} mensajes acumulados.'
    return OK, f'{consumers} worker(s) · {messages} mensaje(s) en cola'


def _migrations():
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if pending:
        names = ', '.join(f'{m.app_label}.{m.name}' for m, _backwards in pending[:3])
        return ERROR, f'{len(pending)} migración(es) pendiente(s): {names}'
    return OK, 'la base de datos está al día'


def _media():
    root = str(settings.MEDIA_ROOT)
    os.makedirs(root, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=root, prefix='.health-', delete=True) as probe:
        probe.write(b'ok')
    usage = shutil.disk_usage(root)
    free_gb = usage.free / 1024 ** 3
    detail = f'escribible · {free_gb:.1f} GB libres de {usage.total / 1024 ** 3:.0f} GB'
    if usage.free / usage.total < 0.1 or free_gb < 1:
        return WARNING, f'Poco espacio: {detail}'
    return OK, detail


def _smtp():
    backend = settings.EMAIL_BACKEND
    if 'smtp' not in backend.lower():
        return WARNING, f'El correo no usa SMTP ({backend.rsplit(".", 1)[-1]}): no se envía nada real.'
    mail = get_connection(timeout=5)
    mail.open()  # connects, STARTTLS and login, without sending anything
    mail.close()
    return OK, f'{settings.EMAIL_HOST}:{settings.EMAIL_PORT} acepta la sesión'


def _versions():
    version = os.environ.get('APP_VERSION', 'sin definir')
    import celery
    return INFO, f'app {version} · Django {django.get_version()} · Celery {celery.__version__} · Python {sys.version.split()[0]}'


# --------------------------------------------------------------------- API
def run_checks(include_smtp=False):
    """All checks as a list of `Check`. The result is remembered for the alerts panel."""
    checks = [
        _timed('Base de datos', _database),
        _timed('Caché', _cache),
        _timed('Broker de mensajes (RabbitMQ)', _broker),
        _timed('Workers de Celery', _worker_queue),
        _timed('Migraciones', _migrations),
        _timed('Archivos subidos (media)', _media),
    ]
    if include_smtp:
        checks.append(_timed('Correo (SMTP)', _smtp))
    checks.append(_timed('Versiones', _versions))
    cache.set(LAST_RESULT_CACHE_KEY, [(c.name, c.status, c.detail) for c in checks], LAST_RESULT_TTL)
    return checks


def check_workers():
    """Only the Celery workers probe, for the scheduled alert (check_celery_workers)."""
    return _timed('Workers de Celery', _worker_queue)


def overall_status(checks):
    statuses = {c.status for c in checks}
    if ERROR in statuses:
        return ERROR
    return WARNING if WARNING in statuses else OK


def last_known_problems():
    """Problems from the most recent run of the status page (no checks are executed here)."""
    last = cache.get(LAST_RESULT_CACHE_KEY) or []
    return [(name, status, detail) for name, status, detail in last if status in (ERROR, WARNING)]
