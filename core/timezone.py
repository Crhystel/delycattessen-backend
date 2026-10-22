import zoneinfo
from datetime import datetime

from django.conf import settings

LOCAL_TIMEZONE = zoneinfo.ZoneInfo(settings.LOCAL_TIME_ZONE)


def local_now():
    """Current moment in the business time zone."""
    return datetime.now(LOCAL_TIMEZONE)


def local_today():
    """Current date in the business time zone (cut-off dates, daily limits)."""
    return local_now().date()
