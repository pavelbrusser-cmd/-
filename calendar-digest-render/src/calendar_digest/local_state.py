"""Private snapshots used to remember the previous time of standalone events."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from icalendar import Calendar

from .calendar_collection import CalendarResource


class StateError(Exception):
    """An unreadable baseline must never silently become a first run."""


@dataclass
class Snapshot:
    current: list[CalendarResource]
    previous: list[CalendarResource]


def _key(resource: CalendarResource) -> tuple:
    events = Calendar.from_ical(resource.data).walk("VEVENT")
    identities = set()
    for event in events:
        uid = str(event.get("UID", ""))
        if not uid:
            raise StateError("В снимке найдено событие без UID.")
        rid = event.decoded("RECURRENCE-ID", None)
        if isinstance(rid, datetime) and rid.tzinfo is not None:
            rid = rid.astimezone(timezone.utc)
        identities.add((uid, str(rid)))
    if not identities:
        raise StateError("В снимке найдено событие без UID.")
    # Providers can keep a master and detached overrides in separate resources.
    masters = {identity for identity in identities if identity[1] == "None"}
    return resource.source_id, resource.calendar_id, tuple(sorted(masters or identities))


def _starts(resource: CalendarResource) -> tuple:
    """Compare actual instants, ignoring unrelated edits and time-zone spelling."""
    result = []
    for event in Calendar.from_ical(resource.data).walk("VEVENT"):
        value = event.decoded("DTSTART", None)
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        rid = event.decoded("RECURRENCE-ID", None)
        if isinstance(rid, datetime) and rid.tzinfo is not None:
            rid = rid.astimezone(timezone.utc)
        result.append((str(event.get("UID", "")), str(rid), str(value)))
    return tuple(sorted(result))


def _index(resources: list[CalendarResource]) -> dict:
    index = {}
    for resource in resources:
        key = _key(resource)
        if key in index and index[key].data != resource.data:
            raise StateError("В одном календаре несколько разных ресурсов с одинаковыми экземплярами UID.")
        index[key] = resource
    return index


def advance_snapshot(resources: list[CalendarResource], old: Snapshot) -> Snapshot:
    """Retain the last different start across repeated reads of an unchanged event."""
    latest = _index(old.current)
    remembered = _index(old.previous)
    current = _index(resources)
    previous = []
    for key, resource in current.items():
        last = latest.get(key)
        if last is None:
            continue
        if last.data != resource.data and _starts(last) != _starts(resource):
            previous.append(last)
        else:
            previous.append(remembered.get(key, last))
    return Snapshot(current=resources, previous=previous)


def read_snapshot(path: Path) -> Snapshot:
    if not path.exists():
        return Snapshot(current=[], previous=[])
    try:
        # The collector bounds one inventory to 30 MiB; state holds at most two.
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("oversized snapshot")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["schema_version"] != 1:
            raise ValueError("unknown schema")
        def decode(items):
            result = []
            for item in items:
                if set(item) != {"source_id", "calendar_id", "calendar_name", "data"}:
                    raise ValueError("invalid resource")
                if not all(isinstance(value, str) for value in item.values()):
                    raise ValueError("invalid resource field")
                result.append(CalendarResource(**item, account_email=""))
            return result
        state = Snapshot(decode(payload["current"]), decode(payload["previous"]))
        _index(state.current)
        _index(state.previous)
        return state
    except Exception:
        raise StateError("Локальный снимок календарей повреждён или недоступен; он сохранён без изменений.") from None


def snapshot_json(snapshot: Snapshot) -> str:
    def encode(resource):
        # Login comes from protected settings each run; do not copy it into state.
        return {key: value for key, value in asdict(resource).items() if key != "account_email"}
    return json.dumps({
        "schema_version": 1,
        "current": [encode(resource) for resource in snapshot.current],
        "previous": [encode(resource) for resource in snapshot.previous],
    }, ensure_ascii=False)


def write_private(path: Path, contents: str) -> None:
    """Replace one file atomically; the temporary and final file have mode 0600."""
    descriptor, temporary = tempfile.mkstemp(prefix=".digest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
