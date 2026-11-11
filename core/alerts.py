"""Alerts shown at the top of the superadmin index.

`core` imports no app, so each app registers its own providers (functions that return
`Alert`s) from an `alerts.py` loaded in its AppConfig.ready(). The combined result is
cached for a minute: the index is opened often and the checks are plain counts.
"""
import logging
from dataclasses import dataclass

from django.core.cache import cache

from . import health

logger = logging.getLogger(__name__)

ERROR, WARNING, INFO = 'error', 'warning', 'info'
CACHE_KEY = 'core.alerts'
CACHE_SECONDS = 60

_providers = []


@dataclass(frozen=True)
class Alert:
    level: str
    title: str
    count: int = 0
    url: str = ''
    hint: str = ''


def register_alert(provider):
    """Decorator: `provider()` returns an iterable of `Alert` (empty when all is fine)."""
    if provider not in _providers:
        _providers.append(provider)
    return provider


def _health_alerts():
    # Only what the "Estado del sistema" page last saw: the index never runs slow probes.
    for name, status, detail in health.last_known_problems():
        yield Alert(ERROR if status == health.ERROR else WARNING, f'{name}: {detail}')


def collect_alerts(force=False):
    """Alerts from every provider, errors first. A broken provider is logged and skipped."""
    cached = None if force else cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    alerts = []
    for provider in [_health_alerts, *_providers]:
        try:
            alerts.extend(provider())
        except Exception:  # noqa: BLE001 - one failing check must not hide the others
            logger.exception('Alert provider %s failed', getattr(provider, '__name__', provider))
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    alerts.sort(key=lambda alert: order.get(alert.level, 3))
    cache.set(CACHE_KEY, alerts, CACHE_SECONDS)
    return alerts
