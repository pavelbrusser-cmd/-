"""Read-only calendar discovery and connectivity checks for Mail.ru accounts."""

from dataclasses import dataclass, field
from datetime import datetime
import os

from .calendar_probe import (
    CalendarInfo,
    CalendarProbe,
    probe_client_calendars,
    validate_probe_interval,
)
from .readonly_caldav import CalendarConnectionError, connect_readonly


MailRuError = CalendarConnectionError


@dataclass(frozen=True)
class MailRuCredentials:
    username: str = field(repr=False)
    app_password: str = field(repr=False)

    @classmethod
    def from_environment(cls, account: int = 1) -> "MailRuCredentials":
        if type(account) is not int or account not in (1, 2, 3):
            raise MailRuError("Номер аккаунта Mail.ru должен быть 1, 2 или 3.")
        names = (f"MAILRU_{account}_USERNAME", f"MAILRU_{account}_APP_PASSWORD")
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise MailRuError(
                "Добавьте значения в защищённые настройки среды: " + ", ".join(missing)
            )
        return cls(os.environ[names[0]].strip(), os.environ[names[1]])


def _mailru_client(credentials: MailRuCredentials):
    return connect_readonly(
        username=credentials.username,
        password=credentials.app_password,
        url="https://calendar.mail.ru/principals/",
        hostname_pattern=r"calendar\.mail\.ru",
        provider="Mail.ru",
    )


def discover_calendars(credentials: MailRuCredentials) -> list[CalendarInfo]:
    """List calendars without downloading or changing events."""
    with _mailru_client(credentials) as client:
        return [
            CalendarInfo(id=calendar.id, name=calendar.get_display_name() or "Без названия")
            for calendar in client.get_calendars()
        ]


def probe_calendars(
    credentials: MailRuCredentials, *, start: datetime, end: datetime
) -> list[CalendarProbe]:
    """Read a bounded sample and return raw counts, never event contents.

    Counts describe CalDAV resources and VEVENT components, not occurrences.
    This connection check does not define the future digest selection rules.
    """
    validate_probe_interval(start=start, end=end)
    with _mailru_client(credentials) as client:
        return probe_client_calendars(client, start=start, end=end)
