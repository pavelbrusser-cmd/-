"""Shared read-only CalDAV inspection, independent of the account provider."""

from dataclasses import dataclass
from datetime import datetime

from caldav import DAVClient

from .readonly_caldav import CalendarConnectionError


@dataclass(frozen=True)
class CalendarInfo:
    id: str | None
    name: str


@dataclass(frozen=True)
class CalendarProbe:
    id: str | None
    name: str
    supported_components: list[str]
    resource_count: int | None
    vevent_components: int | None


def validate_probe_interval(*, start: datetime, end: datetime) -> None:
    """Check the query interval before opening an authenticated connection."""
    if any(value.tzinfo is None or value.utcoffset() is None for value in (start, end)):
        raise CalendarConnectionError(
            "Для проверки нужен интервал с указанным часовым поясом."
        )
    if end <= start:
        raise CalendarConnectionError("Конец интервала проверки должен быть позже начала.")


def probe_client_calendars(
    client: DAVClient, *, start: datetime, end: datetime
) -> list[CalendarProbe]:
    """Count raw resources and VEVENT components, without expanding occurrences.

    The caller validates the interval before connecting. This inspection does
    not determine which events the future digest should include.
    """
    result = []
    for calendar in client.get_calendars():
        components = sorted(calendar.get_supported_components())
        resources = event_components = None
        if "VEVENT" in components:
            records = calendar.search(start=start, end=end, event=True, expand=False)
            resources = len(records)
            event_components = 0
            for record in records:
                events = record.icalendar_instance.walk("VEVENT")
                if not events:
                    raise CalendarConnectionError("CalDAV вернул запись без события VEVENT.")
                event_components += len(events)
        result.append(CalendarProbe(
            id=calendar.id,
            name=calendar.get_display_name() or "Без названия",
            supported_components=components,
            resource_count=resources,
            vevent_components=event_components,
        ))
    return result
