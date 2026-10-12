"""Times are stored in UTC and shown in the practice's local time zone."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import current_app


def _tz():
    return ZoneInfo(current_app.config["TIMEZONE"])


def to_local(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_tz())


def local_day_start_utc(day):
    """Midnight of ``day`` in local time, as a naive UTC datetime for querying."""
    local = datetime(day.year, day.month, day.day, tzinfo=_tz())
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def today():
    """Today's date in the practice's time zone (tests may pin it with the TODAY setting)."""
    pinned = current_app.config.get("TODAY")
    if pinned:
        return pinned
    return datetime.now(_tz()).date()
