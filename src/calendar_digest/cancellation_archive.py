"""Durable, confirmed cancellation records independent of calendar snapshots.

The original resource is a private recovery copy. Digest reads never load it:
only a small validated event record belonging to a completed action is visible.
"""

from dataclasses import asdict, fields, replace
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

from icalendar import Calendar

from .action_store import StoreError
from .selection import (
    DigestEvent, MOSCOW, _is_recurring, _read_series, _resource_uids, select_day,
)


_EVENT_FIELDS = frozenset(field.name for field in fields(DigestEvent))
_REQUIRED_TEXT = frozenset({
    "source_id", "calendar_id", "uid", "start", "end", "event_status", "participation",
})
_TEXT = _REQUIRED_TEXT | {"calendar_name", "title", "account_email"}
_OPTIONAL_TEXT = frozenset({
    "recurrence_id", "original_start", "original_end", "move_basis", "location",
})
_BOOLEANS = frozenset({"all_day", "recurring", "multi_day", "timezone_assumed"})
_ERROR = "Не удалось прочитать или сохранить архив отменённых событий."


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _moment(value: str) -> date | datetime:
    if "T" in value:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError
        return moment.astimezone(MOSCOW)
    moment = date.fromisoformat(value)
    if moment.isoformat() != value:
        raise ValueError
    return moment


def _interval(start: str, end: str | None):
    begin = _moment(start)
    finish = _moment(end) if end is not None else begin
    if type(begin) is not type(finish) or finish < begin:
        raise ValueError
    if type(begin) is date and end is not None and finish == begin:
        raise ValueError
    return begin, finish


def _event(payload: dict, *, cancelled=True) -> DigestEvent:
    try:
        if (not isinstance(payload, dict)
                or set(payload) not in (_EVENT_FIELDS, _EVENT_FIELDS - {"account_email"})):
            raise ValueError
        payload = {"account_email": "", **payload}
        for name in _TEXT:
            if not isinstance(payload[name], str) or (name in _REQUIRED_TEXT and not payload[name].strip()):
                raise ValueError
        for name in _OPTIONAL_TEXT:
            if payload[name] is not None and not isinstance(payload[name], str):
                raise ValueError
        if any(type(payload[name]) is not bool for name in _BOOLEANS):
            raise ValueError
        if cancelled and payload["event_status"] != "CANCELLED":
            raise ValueError
        begin, _ = _interval(payload["start"], payload["end"])
        if payload["all_day"] != (type(begin) is date):
            raise ValueError
        if payload["recurrence_id"] is not None:
            if type(_moment(payload["recurrence_id"])) is not type(begin) or not payload["recurring"]:
                raise ValueError
        if payload["original_start"] is not None:
            _interval(payload["original_start"], payload["original_end"])
            if payload["move_basis"] not in {"previous_snapshot", "recurrence_id"}:
                raise ValueError
        elif payload["original_end"] is not None or payload["move_basis"] is not None:
            raise ValueError
        return DigestEvent(**payload)
    except (TypeError, ValueError, OverflowError):
        raise StoreError(_ERROR) from None


def digest_event_json(event: DigestEvent, *, cancelled=False) -> str:
    try:
        if not isinstance(event, DigestEvent):
            raise ValueError
        payload = asdict(event)
        _event(payload, cancelled=cancelled)
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise StoreError(_ERROR) from None


def cancellation_json(event: DigestEvent) -> str:
    return digest_event_json(event, cancelled=True)


def validate_context(encoded: str, target: dict) -> DigestEvent:
    """A signed action's private context must describe its exact snapshot."""
    try:
        if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError
        event = _event(json.loads(encoded, object_pairs_hook=_object), cancelled=False)
        expected = {
            "source_id": event.source_id, "calendar_id": event.calendar_id,
            "uid": event.uid, "recurrence_id": event.recurrence_id,
            "expected_start": event.start, "expected_end": event.end,
            "expected_event_status": event.event_status,
            "expected_participation": event.participation,
        }
        if target != expected:
            raise ValueError
        return event
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError(_ERROR) from None


def enrich_cancellation(event: DigestEvent, encoded: str, target: dict) -> DigestEvent:
    context = validate_context(encoded, target)
    if event.original_start is None and context.original_start is not None:
        return replace(
            event, original_start=context.original_start, original_end=context.original_end,
            move_basis=context.move_basis,
            timezone_assumed=event.timezone_assumed or context.timezone_assumed,
        )
    return event


def validate_backup(data: str, event: DigestEvent) -> str:
    try:
        if not isinstance(data, str) or not 0 < len(data.encode("utf-8")) <= 4 * 1024 * 1024:
            raise ValueError
        if _resource_uids(data) != {event.uid}:
            raise ValueError
        calendar = Calendar.from_ical(data)
        if calendar.errors or any(item.errors or "ATTENDEE" in item for item in calendar.walk("VEVENT")):
            raise ValueError
        moment = _moment(event.start)
        day = moment.date() if isinstance(moment, datetime) else moment
        resource = SimpleNamespace(
            source_id=event.source_id, calendar_id=event.calendar_id,
            calendar_name=event.calendar_name, account_email="", data=data,
        )
        matches = [item for item in select_day([resource], day) if _identity(item) == _identity(event)]
        if len(matches) != 1 or (matches[0].start, matches[0].end, matches[0].participation) != (event.start, event.end, event.participation):
            raise ValueError
        return matches[0].event_status
    except Exception:
        raise StoreError(_ERROR) from None


def _confirmed_events(state_dir: Path) -> list[DigestEvent]:
    path = Path(state_dir).expanduser().absolute() / "actions.sqlite3"
    connection = None
    try:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return []
        if not stat.S_ISREG(mode):
            raise StoreError(_ERROR)
        # mode=ro prevents a preview from creating a database or needing a key.
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cancellation_archive'"
        ).fetchone() is None:
            return []
        cursor = connection.execute(
            "SELECT c.event_json FROM cancellation_archive c JOIN actions a ON a.id=c.action_id "
            "WHERE a.state='done' AND a.action='cancel' ORDER BY c.staged_at,a.rowid"
        )
        # Repeated cancellations after a restoration retain all backups while
        # only the newest successful record of the same occurrence is rendered.
        events = {}
        for (encoded,) in cursor:
            if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > 64 * 1024:
                raise StoreError(_ERROR)
            event = _event(json.loads(encoded, object_pairs_hook=_object))
            events[_identity(event)] = event
        return list(events.values())
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        raise StoreError(_ERROR) from None
    finally:
        if connection is not None:
            connection.close()


def _identity(event: DigestEvent):
    return event.source_id, event.calendar_id, event.uid, event.recurrence_id


def _intersects(event: DigestEvent, day: date) -> bool:
    day_start = datetime.combine(day, time.min, MOSCOW)
    day_end = day_start + timedelta(days=1)
    for begin, end in ((event.start, event.end), (event.original_start, event.original_end)):
        if begin is None:
            continue
        start, finish = _interval(begin, end)
        if type(start) is date:
            start = datetime.combine(start, time.min, MOSCOW)
            finish = datetime.combine(finish, time.min, MOSCOW)
        intersects = (day_start <= start < day_end) if start == finish else (start < day_end and finish > day_start)
        if intersects:
            return True
    return False


def _restored_identities(resources, cancellations):
    wanted = {}
    for event in cancellations:
        wanted.setdefault(_identity(event)[:3], []).append(event)
    groups = {}
    calendars = {key[:2] for key in wanted}
    # Index UID lines, retaining raw-resource references only. Parse full
    # iCalendar graphs solely for archived UIDs, one series at a time.
    for resource in resources:
        if (resource.source_id, resource.calendar_id) not in calendars:
            continue
        for uid in _resource_uids(resource.data):
            key = (resource.source_id, resource.calendar_id, uid)
            if key in wanted:
                groups.setdefault(key, []).append(resource)
    restored = set()
    for key, group in groups.items():
        series = _read_series(group, only_key=key)[key]
        master = series.components.get(None)
        dates = {}
        for event in wanted[key]:
            if event.recurrence_id in series.components:
                # Includes explicit overrides moved outside the requested day.
                restored.add(_identity(event))
            elif event.recurrence_id is not None and _is_recurring(master):
                moment = _moment(event.recurrence_id)
                day = moment.date() if isinstance(moment, datetime) else moment
                if day not in dates:
                    dates[day] = {_identity(item) for item in select_day(group, day)}
                if _identity(event) in dates[day]:
                    restored.add(_identity(event))
    return restored


def merge_cancellations(events, resources, day: date, state_dir: Path) -> list[DigestEvent]:
    """Include confirmed local cancellations unless that exact event is live.

    A live event wins even after moving outside this digest's day. Recurring
    masters do not hide archived instances still excluded by EXDATE.
    """
    archived = [event for event in _confirmed_events(state_dir) if _intersects(event, day)]
    if not archived:
        return events
    restored = _restored_identities(resources, archived)
    merged = {_identity(event): event for event in archived if _identity(event) not in restored}
    merged.update((_identity(event), event) for event in events)
    return sorted(merged.values(), key=lambda event: (
        not event.all_day, event.start, event.source_id, event.calendar_id,
        event.uid, event.recurrence_id or "",
    ))
