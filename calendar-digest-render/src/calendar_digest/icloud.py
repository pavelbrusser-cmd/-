"""Read-only iCloud discovery and connectivity checks."""

from dataclasses import dataclass, field
from datetime import datetime
import os

from .calendar_probe import (
    CalendarInfo,
    CalendarProbe,
    probe_client_calendars,
    validate_probe_interval,
)
from .readonly_caldav import (
    CalendarConnectionError as ICloudError,
    ReadOnlyCalDAVSession,
    connect_readonly,
)


@dataclass(frozen=True)
class Credentials:
    username: str = field(repr=False)
    app_password: str = field(repr=False)

    @classmethod
    def from_environment(cls) -> "Credentials":
        names = ("ICLOUD_USERNAME", "ICLOUD_APP_PASSWORD")
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise ICloudError(
                "Добавьте значения в защищённые настройки среды: " + ", ".join(missing)
            )
        return cls(os.environ[names[0]].strip(), os.environ[names[1]])


class ICloudReadSession(ReadOnlyCalDAVSession):
    """Validate every prepared request, including requests following redirects."""

    def __init__(self):
        super().__init__(
            hostname_pattern=r"(?:caldav|p[0-9]+-caldav)\.icloud\.com",
            initial_host="caldav.icloud.com", provider="iCloud",
        )


def _icloud_client(credentials: Credentials):
    return connect_readonly(
        username=credentials.username, password=credentials.app_password,
        url="https://caldav.icloud.com/",
        hostname_pattern=r"(?:caldav|p[0-9]+-caldav)\.icloud\.com", provider="iCloud",
        session=ICloudReadSession(),
    )


def discover_calendars(credentials: Credentials) -> list[CalendarInfo]:
    """List calendars without downloading or changing events."""
    with _icloud_client(credentials) as client:
        return [
            CalendarInfo(id=calendar.id, name=calendar.get_display_name() or "Без названия")
            for calendar in client.get_calendars()
        ]


def probe_calendars(
    credentials: Credentials, *, start: datetime, end: datetime
) -> list[CalendarProbe]:
    """Read a bounded sample and return counts, never event contents.

    Counts describe CalDAV resources and VEVENT components, not occurrences.
    This connection check does not define the future digest selection rules.
    """
    validate_probe_interval(start=start, end=end)
    with _icloud_client(credentials) as client:
        return probe_client_calendars(client, start=start, end=end)
