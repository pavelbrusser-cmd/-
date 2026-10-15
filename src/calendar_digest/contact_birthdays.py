"""Pure annual reminders from the minimal iCloud contact birthday projection.

The birth year is deliberately absent. February 29 produces reminders only in
leap years, never an inferred February 28 or March 1. Contact names are not used
as identity, and no cross-source or namesake deduplication is attempted.
"""

from datetime import date, datetime, time, timedelta, timezone
import os
from typing import TYPE_CHECKING, Iterable, Mapping
from zoneinfo import ZoneInfo

from .contact_identity import canonical_contact_book_id

if TYPE_CHECKING:
    from .contact_birthdays_source import ContactBirthday
    from .reminders import ReminderPlan


MOSCOW = ZoneInfo("Europe/Moscow")
MAX_CONTACT_BIRTHDAYS = 10_000


def contact_birthdays_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """The optional source stays off until explicitly enabled."""
    environment = os.environ if environment is None else environment
    value = environment.get("ICLOUD_CONTACT_BIRTHDAYS_ENABLED", "false")
    if value not in {"true", "false"}:
        from .reminders import ReminderError

        raise ReminderError("ICLOUD_CONTACT_BIRTHDAYS_ENABLED должен быть true или false.")
    return value == "true"


def plan_contact_birthdays(
    contacts: Iterable["ContactBirthday"], *, window_start: datetime, window_end: datetime,
    morning_time: time = time(9),
) -> "ReminderPlan":
    """Plan D−3, D−1 and D0 at Moscow morning time within [start, end).

    Every annual occurrence has ``birthday:YYYY`` identity. A name or date
    correction therefore updates pending work without replaying a rule already
    accepted for the same contact and year.
    """
    from .reminders import MAX_WINDOW_DAYS, Reminder, ReminderError, ReminderPlan

    try:
        for instant in (window_start, window_end):
            if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
                raise ReminderError("Период дней рождения должен содержать часовой пояс.")
        duration = window_end.astimezone(timezone.utc) - window_start.astimezone(timezone.utc)
        if not timedelta(0) < duration <= timedelta(days=MAX_WINDOW_DAYS):
            raise ReminderError("Период дней рождения должен быть положительным и не длиннее 31 суток.")
        if not isinstance(morning_time, time) or morning_time.tzinfo is not None:
            raise ReminderError("Утреннее время дней рождения задаётся без часового пояса.")
        start, end = window_start.astimezone(MOSCOW), window_end.astimezone(MOSCOW)
        last_date = end.date() + timedelta(days=3)
        summary = {
            "series_total": 0, "series_without_policy": 0, "candidate_occurrences": 0,
            "cancelled_occurrences": 0, "unclassified_occurrences": 0,
            "timezone_assumed_occurrences": 0, "reminders_total": 0,
            "by_category": {"birthdays": 0, "control": 0, "periodic_control": 0},
            "by_rule": {"d3": 0, "d1": 0, "d0": 0, "h3": 0, "h1": 0},
            "classification_evidence": {"contacts": 0},
        }
        reminders = []
        seen = {}
        for index, contact in enumerate(contacts, 1):
            if index > MAX_CONTACT_BIRTHDAYS:
                raise ReminderError("Число дней рождения превышает лимит; план не сформирован.")
            if any(not isinstance(getattr(contact, field, None), str) or not getattr(contact, field).strip()
                   for field in ("book_id", "uid", "name")):
                raise ReminderError("Неполная запись дня рождения; план не сформирован.")
            if type(contact.month) is not int or type(contact.day) is not int:
                raise ReminderError("Некорректная дата дня рождения; план не сформирован.")
            date(2000, contact.month, contact.day)  # Accept February 29, reject impossible dates.
            # Cached records created before canonical IDs were introduced may
            # contain a routing URL. Normalize pure-planner inputs as well as
            # live collection so these records have exactly the same identity.
            book_id = canonical_contact_book_id(contact.book_id)
            identity = (book_id, contact.uid)
            details = (contact.name, contact.month, contact.day)
            if identity in seen:
                if seen[identity] != details:
                    raise ReminderError("Противоречивые записи одного дня рождения; план не сформирован.")
                continue
            seen[identity] = details
            summary["series_total"] += 1
            for year in range(start.year, last_date.year + 1):
                try:
                    birthday = date(year, contact.month, contact.day)
                except ValueError:
                    # Only February 29 of a non-leap year can reach this path.
                    continue
                if not start.date() <= birthday <= last_date:
                    continue
                summary["candidate_occurrences"] += 1
                summary["classification_evidence"]["contacts"] += 1
                morning = datetime.combine(birthday, morning_time, MOSCOW)
                for rule, advance in (("d3", 3), ("d1", 1), ("d0", 0)):
                    notify_at = morning - timedelta(days=advance)
                    if not start <= notify_at < end:
                        continue
                    reminders.append(Reminder(
                        source_id="icloud_contacts", calendar_id=book_id,
                        calendar_name="Дни рождения · iCloud Contacts", uid=contact.uid,
                        recurrence_id=f"birthday:{year}", title=f"День рождения · {contact.name}",
                        event_start=birthday.isoformat(), all_day=True, category="birthdays",
                        rule=rule, notify_at=notify_at.isoformat(), classification_evidence=("contacts",),
                    ))
                    summary["by_rule"][rule] += 1
        reminders.sort(key=lambda item: (item.notify_at, item.stable_id))
        summary["reminders_total"] = len(reminders)
        summary["by_category"]["birthdays"] = len(reminders)
        return ReminderPlan(reminders, summary)
    except ReminderError:
        raise
    except Exception:
        raise ReminderError("Не удалось безопасно сформировать напоминания о днях рождения.") from None
