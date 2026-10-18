"""Refresh the private calendar cache and dispatch due encrypted reminders.

Every delivery batch follows a successful complete metadata synchronization.
The shared calendar gate bounds memory use alongside email preparation and
keeps local calendar actions from racing this batch. No email state is changed.
"""

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path

from .calendar_operation_lock import calendar_operation_lock
from .calendar_sync import CachedResource, sync_calendars, to_calendar_resources
from .contact_birthdays import contact_birthdays_enabled, plan_contact_birthdays
from .contact_birthdays_source import BirthdayCollection, collect_contact_birthdays
from .push_queue import (
    HORIZON_DAYS, OVERDUE_GRACE_SECONDS, QueueError, schedule_store,
)
from .push_transport import PushoverSettings, PushError, PushUncertain, send_notification
from .push_timing import MOSCOW, delivery_allowed, reminder_was_deferred
from .reminders import combine_reminder_plans, plan_reminders
from .runtime import push_schedule_enabled


CONTROL_SOURCE = "mailru_2"
MAX_BATCH = 50
POLICY_VERSION = "3"


class PushCycleError(Exception):
    """A safe automatic delivery diagnostic without event text or credentials."""


class _ClaimSkipped(Exception):
    """No HTTP submission: the due window or fresh-sync window has closed."""


def _now():
    return datetime.now(timezone.utc)


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PushCycleError("Для уведомлений требуется время с часовым поясом.")
    return value.astimezone(timezone.utc)


def _plain(value, limit):
    value = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in str(value))
    value = " ".join(value.split())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def notification_text(reminder: dict) -> tuple[str, str]:
    """Keep source text plain, bounded, and encrypted by the transport."""
    labels = {"birthdays": "День рождения", "control": "Поручение",
              "periodic_control": "Периодическое поручение", "google_events": "Событие Google"}
    reasons = {"d3": "за 3 дня", "d1": "за 1 день", "d0": "сегодня",
               "h3": "за 3 часа", "h1": "за 1 час"}
    try:
        reason = reasons[reminder["rule"]]
        if reminder["rule"] in {"h1", "h3"} and reminder_was_deferred(
                reminder["event_start"], reminder["rule"], reminder["notify_at"]):
            reason = "перенесено на 07:00 МСК"
        title = f"{labels[reminder['category']]} · {reason}"
        if reminder["all_day"]:
            event_time = date.fromisoformat(reminder["event_start"]).strftime("%d.%m.%Y") + " · весь день"
        else:
            event_time = datetime.fromisoformat(reminder["event_start"]).astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
        text = _plain(reminder["title"], 700) or "Без названия"
        calendar = _plain(reminder["calendar_name"], 150)
        return title, f"{text}\n{event_time}\nКалендарь: {calendar}"
    except (KeyError, ValueError, TypeError):
        raise PushCycleError("Не удалось оформить напоминание; отправка не выполнялась.") from None


def _refresh(store, *, clock):
    use_contacts = contact_birthdays_enabled()
    previous = [CachedResource(**item) for item in store.load_cache()]
    observed_at = _instant(clock())
    birthdays = collect_contact_birthdays() if use_contacts else BirthdayCollection([], 0)
    contacts = sorted((asdict(item) for item in birthdays.contacts),
                      key=lambda item: (item["book_id"], item["uid"]))
    synchronized = sync_calendars(previous)
    moment = _instant(clock())
    summary = store.summary(moment)
    horizon = moment + timedelta(days=HORIZON_DAYS)
    planned_until = summary.get("planned_until")
    needs_plan = (
        synchronized.changed != 0 or planned_until is None
        or summary.get("contacts_enabled", False) != use_contacts
        or store.load_contacts() != contacts
        or summary.get("policy_version") != POLICY_VERSION
        or datetime.fromisoformat(planned_until) < moment + timedelta(days=HORIZON_DAYS - 1)
    )
    counts = {"changed": synchronized.changed, "fetched": synchronized.fetched,
              "deleted": synchronized.deleted}
    if needs_plan:
        activated = summary.get("activated_at")
        start = moment
        if activated is not None:
            start = max(datetime.fromisoformat(activated), moment - timedelta(seconds=OVERDUE_GRACE_SECONDS))
        plan = plan_reminders(to_calendar_resources(synchronized.resources),
                              window_start=start, window_end=horizon, control_source=CONTROL_SOURCE)
        if use_contacts:
            plan = combine_reminder_plans(plan, plan_contact_birthdays(
                birthdays.contacts, window_start=start, window_end=horizon,
            ))
        store.commit_plan(
            [asdict(item) for item in synchronized.resources],
            [dict(asdict(item), stable_id=item.stable_id) for item in plan.reminders],
            now=moment, horizon=horizon, calendar_count=synchronized.calendar_count,
            sync_summary=counts, policy_version=POLICY_VERSION, observed_at=observed_at,
            contacts=contacts, contacts_enabled=use_contacts, addressbook_count=birthdays.addressbooks,
        )
    else:
        store.mark_sync(moment, synchronized.calendar_count, counts, observed_at=observed_at,
                        contacts_enabled=use_contacts, addressbook_count=birthdays.addressbooks)
    return counts


def _send_due(store, settings, *, clock):
    accepted = failed = uncertain = 0
    refresh_required = False
    if not delivery_allowed(_instant(clock())):
        return {"accepted": 0, "failed": 0, "uncertain": 0, "refresh_required": False}
    if not store.summary(_instant(clock()))["sync_fresh_for_delivery"]:
        return {"accepted": 0, "failed": 0, "uncertain": 0, "refresh_required": True}
    for reminder in store.due(_instant(clock()), limit=MAX_BATCH):
        title, message = notification_text(reminder)
        identifier = reminder["stable_id"]
        claimed = False

        def before_send():
            nonlocal claimed
            # Claim checks the actual Moscow delivery window again after device
            # validation, including a midnight crossed during that request.
            if not store.claim(identifier, now=_instant(clock())):
                raise _ClaimSkipped()
            claimed = True

        try:
            result = send_notification(settings, title=title, message=message, before_send=before_send)
            if not claimed or result.get("state") != "accepted" or result.get("encrypted") is not True:
                raise PushUncertain("Не подтверждён результат защищённой отправки.")
        except _ClaimSkipped:
            refresh_required = True
            break
        except PushUncertain:
            store.complete(identifier, "uncertain", now=_instant(clock()))
            uncertain += 1
            break
        except PushError as error:
            store.complete(identifier, "failed", now=_instant(clock()), retryable=error.retryable)
            failed += 1
            break
        except QueueError:
            # A checkpoint may have committed before storage reported failure.
            # Do not overwrite it; recovery turns an interrupted send uncertain.
            raise
        except Exception:
            store.complete(identifier, "uncertain" if claimed else "failed",
                           now=_instant(clock()), retryable=not claimed)
            raise PushCycleError("Отправка уведомлений остановлена; проверьте push-status.") from None
        try:
            store.complete(identifier, "accepted", now=_instant(clock()))
        except Exception:
            raise PushCycleError("Pushover принял уведомление, но квитанция не сохранена; повтор запрещён.") from None
        accepted += 1
    return {"accepted": accepted, "failed": failed, "uncertain": uncertain,
            "refresh_required": refresh_required}


def _cycle(directory, *, dispatch, clock):
    directory = Path(directory)
    started = _instant(clock())
    settings = PushoverSettings.from_environment() if dispatch else None
    gate_directory = Path(os.environ.get("DIGEST_STATE_DIR") or directory)
    with calendar_operation_lock(gate_directory), schedule_store(directory, now=started) as store:
        if dispatch:
            store.activate(started)
        changes = _refresh(store, clock=clock)
        outcome = _send_due(store, settings, clock=clock) if dispatch else {
            "accepted": 0, "failed": 0, "uncertain": 0, "refresh_required": False,
        }
        return {"mode": "automatic" if dispatch else "prepare", "sent": outcome["accepted"] > 0,
                **outcome, "sync": changes, "queue": store.summary(_instant(clock()))}


def prepare_push(directory: Path, *, clock=_now) -> dict:
    """Warm the calendar cache and future queue without activation or sending."""
    return _cycle(directory, dispatch=False, clock=clock)


def run_push_cycle(directory: Path, *, clock=_now) -> dict:
    """Only an explicitly enabled schedule may perform real automatic sends."""
    if not push_schedule_enabled():
        raise PushCycleError("Автоматические push-уведомления выключены: PUSH_SCHEDULE_ENABLED=false.")
    return _cycle(directory, dispatch=True, clock=clock)


def automatic_push_status(directory: Path, *, now=None) -> dict:
    """Report queue and heartbeat without credentials, event text, or network."""
    from .push_scheduler import push_scheduler_status

    moment = _instant(now or _now())
    try:
        with schedule_store(directory, now=moment) as store:
            queue = store.summary(moment)
    except QueueError as error:
        if not error.retryable:
            raise
        queue = {"busy": True}
    return {"enabled": push_schedule_enabled(), **push_scheduler_status(directory, now=moment),
            "queue": queue}
