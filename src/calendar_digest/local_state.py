"""Private snapshots used to remember the previous time of standalone events."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from icalendar import Calendar
import ijson

from .calendar_collection import CalendarResource


class StateError(Exception):
    """An unreadable baseline must never silently become a first run."""


@dataclass
class Snapshot:
    current: list[CalendarResource]
    previous: list[CalendarResource]


def _metadata(data: str, cache=None) -> tuple:
    if cache is not None and data in cache:
        return cache[data]
    events = Calendar.from_ical(data).walk("VEVENT")
    identities = set()
    starts = []
    for event in events:
        uid = str(event.get("UID", ""))
        if not uid:
            raise StateError("В снимке найдено событие без UID.")
        rid = event.decoded("RECURRENCE-ID", None)
        if isinstance(rid, datetime) and rid.tzinfo is not None:
            rid = rid.astimezone(timezone.utc)
        identities.add((uid, str(rid)))
        start = event.decoded("DTSTART", None)
        if isinstance(start, datetime) and start.tzinfo is not None:
            start = start.astimezone(timezone.utc)
        starts.append((uid, str(rid), str(start)))
    if not identities:
        raise StateError("В снимке найдено событие без UID.")
    # Providers can keep a master and detached overrides in separate resources.
    masters = {identity for identity in identities if identity[1] == "None"}
    result = tuple(sorted(masters or identities)), tuple(sorted(starts))
    if cache is not None:
        cache[data] = result
    return result


def _key(resource: CalendarResource, cache=None) -> tuple:
    return resource.source_id, resource.calendar_id, _metadata(resource.data, cache)[0]


def _starts(resource: CalendarResource, cache=None) -> tuple:
    """Compare actual instants, ignoring unrelated edits and time-zone spelling."""
    return _metadata(resource.data, cache)[1]


def _index(resources: list[CalendarResource], cache=None) -> dict:
    index = {}
    for resource in resources:
        key = _key(resource, cache)
        if key in index and index[key].data != resource.data:
            raise StateError("В одном календаре несколько разных ресурсов с одинаковыми экземплярами UID.")
        index[key] = resource
    return index


def advance_snapshot(resources: list[CalendarResource], old: Snapshot) -> Snapshot:
    """Retain the last different start across repeated reads of an unchanged event."""
    cache = {}
    latest = _index(old.current, cache)
    remembered = _index(old.previous, cache)
    current = _index(resources, cache)
    previous = []
    for key, resource in current.items():
        last = latest.get(key)
        if last is None:
            continue
        if last.data != resource.data and _starts(last, cache) != _starts(resource, cache):
            previous.append(last)
        else:
            previous.append(remembered.get(key, last))
    return Snapshot(current=resources, previous=previous)


def _stream_snapshot(stream) -> Snapshot:
    """Read one resource at a time, including the older duplicated history format."""
    tokens = iter(ijson.parse(stream))

    def expect(prefix, event):
        token = next(tokens)
        if token[:2] != (prefix, event):
            raise ValueError("invalid snapshot structure")
        return token[2]

    expect("", "start_map")
    seen = set()
    resources = {}
    fields = {"source_id", "calendar_id", "calendar_name", "data"}
    while True:
        token = next(tokens)
        if token[:2] == ("", "end_map"):
            break
        if token[:2] != ("", "map_key"):
            raise ValueError("invalid snapshot key")
        name = token[2]
        if name not in {"schema_version", "current", "previous"} or name in seen:
            raise ValueError("unknown or duplicate snapshot key")
        seen.add(name)
        if name == "schema_version":
            if expect(name, "number") != 1:
                raise ValueError("unknown snapshot version")
            continue
        expect(name, "start_array")
        resources[name] = []
        while True:
            token = next(tokens)
            if token[:2] == (name, "end_array"):
                break
            prefix = name + ".item"
            if token[:2] != (prefix, "start_map"):
                raise ValueError("invalid snapshot resource")
            item = {}
            while True:
                token = next(tokens)
                if token[:2] == (prefix, "end_map"):
                    break
                if token[:2] != (prefix, "map_key"):
                    raise ValueError("invalid resource key")
                key = token[2]
                if key not in fields or key in item:
                    raise ValueError("unknown or duplicate resource key")
                item[key] = expect(prefix + "." + key, "string")
            if set(item) != fields:
                raise ValueError("incomplete snapshot resource")
            resources[name].append(CalendarResource(**item, account_email=""))
    if seen != {"schema_version", "current", "previous"} or next(tokens, None) is not None:
        raise ValueError("incomplete snapshot or trailing data")
    return Snapshot(resources["current"], resources["previous"])


def read_snapshot(path: Path) -> Snapshot:
    if not path.exists():
        return Snapshot(current=[], previous=[])
    try:
        # The collector bounds one inventory to 30 MiB; state holds at most two.
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("oversized snapshot")
        with path.open("rb") as stream:
            state = _stream_snapshot(stream)
        cache = {}
        _index(state.current, cache)
        _index(state.previous, cache)
        return state
    except Exception:
        raise StateError("Локальный снимок календарей повреждён или недоступен; он сохранён без изменений.") from None


def _snapshot_payload(snapshot: Snapshot) -> dict:
    def encode(resource):
        # Login comes from protected settings each run; do not copy it into state.
        return {key: value for key, value in asdict(resource).items() if key != "account_email"}
    # Identical historical resources add no information. Their current copy
    # remains available to advance_snapshot when a later move is detected.
    current_data = {(item.source_id, item.calendar_id, item.data) for item in snapshot.current}
    return {
        "schema_version": 1,
        "current": [encode(resource) for resource in snapshot.current],
        "previous": [encode(resource) for resource in snapshot.previous
                     if (resource.source_id, resource.calendar_id, resource.data) not in current_data],
    }


def snapshot_json(snapshot: Snapshot) -> str:
    return json.dumps(_snapshot_payload(snapshot), ensure_ascii=False)


def write_snapshot(path: Path, snapshot: Snapshot) -> None:
    """Atomically stream the snapshot instead of allocating one large JSON string."""
    payload = _snapshot_payload(snapshot)
    descriptor, temporary = tempfile.mkstemp(prefix=".digest-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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
