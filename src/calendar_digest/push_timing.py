"""Shared Moscow delivery window for automatic phone reminders.

This is a persistent service policy, independent of the host/iPhone time zone.
Keep this module standard-library-only for the lightweight queue and scheduler.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
NOT_BEFORE = time(7)


def _local(moment: datetime) -> datetime:
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Для времени push требуется дата с часовым поясом.")
    return moment.astimezone(MOSCOW)


def normalize_notification_time(moment: datetime) -> datetime:
    """Defer 00:00–06:59 notifications to 07:00 of the same Moscow date.

    This also applies when the event itself starts before 07:00: the owner has
    explicitly chosen a later reminder rather than an earlier evening message.
    """
    local = _local(moment)
    earliest = datetime.combine(local.date(), NOT_BEFORE, MOSCOW)
    return max(local, earliest)


def delivery_allowed(moment: datetime) -> bool:
    """Check actual dispatch time, including retries and midnight boundaries."""
    return _local(moment).time() >= NOT_BEFORE


def reminder_was_deferred(event_start: str, rule: str, notify_at: str) -> bool:
    """Avoid labelling a shifted hourly reminder as an exact one/three hours."""
    if rule not in {"h1", "h3"}:
        return False
    original = _local(datetime.fromisoformat(event_start)) - timedelta(hours=int(rule[1:]))
    scheduled = _local(datetime.fromisoformat(notify_at))
    allowed = normalize_notification_time(original)
    return allowed > original and scheduled >= allowed
