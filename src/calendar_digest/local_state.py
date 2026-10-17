"""Private snapshots for standalone moves and coherent former series versions."""

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from icalendar import Calendar, vText
from icalendar.parser import Contentlines
import ijson

from .calendar_collection import CalendarResource


class StateError(Exception):
    """An unreadable baseline must never silently become a first run."""


MAX_SNAPSHOT_BYTES = 192 * 1024 * 1024
MAX_SERIES_HISTORY_BYTES = 30 * 1024 * 1024


@dataclass
class Snapshot:
    current: list[CalendarResource]
    previous: list[CalendarResource]
    series_previous: list[CalendarResource] = field(default_factory=list)


def _metadata(data: str, cache=None) -> tuple:
    if cache is not None and data in cache:
        return cache[data]
    events = Calendar.from_ical(data).walk("VEVENT")
    identities = set()
    starts = []
    masters_info = []
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
        if rid is None:
            masters_info.append((uid, str(start), bool(event.get("RRULE") or event.get("RDATE"))))
    if not identities:
        raise StateError("В снимке найдено событие без UID.")
    # Providers can keep a master and detached overrides in separate resources.
    masters = {identity for identity in identities if identity[1] == "None"}
    result = (
        tuple(sorted(masters or identities)), tuple(sorted(starts)),
        tuple(sorted(masters_info)), frozenset(uid for uid, _ in identities),
    )
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


def _series_index(resources, cache):
    """Keep raw references and compact anchor metadata, not parsed calendars."""
    groups, masters = {}, {}
    for resource in _index(resources, cache).values():
        metadata = _metadata(resource.data, cache)
        for uid in metadata[3]:
            groups.setdefault((resource.source_id, resource.calendar_id, uid), []).append(resource)
        for uid, start, recurring in metadata[2]:
            key = resource.source_id, resource.calendar_id, uid
            anchor = start, recurring
            if key in masters and masters[key] != anchor:
                raise StateError("В снимке обнаружены противоречивые основные версии серии.")
            masters[key] = anchor
    return groups, masters


def _scoped_resource(resource, uid, cache):
    """Split aggregate ICS only when needed; ordinary resources stay byte exact.

    Preserve non-VEVENT components such as VTIMEZONE. Preserve the lexical
    DURATION too: icalendar otherwise turns PT24H into P1D, which changes the
    interval across daylight-saving transitions.
    """
    if _metadata(resource.data, cache)[3] == {uid}:
        return resource
    calendar = Calendar.from_ical(resource.data)
    durations = []
    stack = []
    for line in Contentlines.from_ical(resource.data):
        if not line:
            continue
        name, _, value = line.parts()
        name = name.upper()
        if name == "BEGIN":
            stack.append(str(value).upper())
            if stack[-1] == "VEVENT":
                durations.append([])
        elif name == "END":
            if not stack or stack.pop() != str(value).upper():
                raise StateError("В снимке обнаружена некорректная структура календаря.")
        elif name == "DURATION" and stack and stack[-1] == "VEVENT":
            durations[-1].append(str(value))
    if stack:
        raise StateError("В снимке обнаружена некорректная структура календаря.")
    for event, values in zip(calendar.walk("VEVENT"), durations, strict=True):
        if values:
            original = event["DURATION"]
            originals = original if isinstance(original, list) else [original]
            restored = []
            for value, prop in zip(values, originals, strict=True):
                duration = vText(value)
                duration.params = deepcopy(prop.params)
                restored.append(duration)
            event["DURATION"] = restored if isinstance(original, list) else restored[0]
    calendar.subcomponents = [
        component for component in calendar.subcomponents
        if component.name != "VEVENT" or str(component.get("UID", "")) == uid
    ]
    scoped = replace(resource, data=calendar.to_ical().decode("utf-8"))
    if _metadata(scoped.data, cache)[3] != {uid}:
        raise StateError("Не удалось выделить полную прежнюю версию серии.")
    return scoped


def _advance_series_history(resources, old, cache):
    current, current_masters = _series_index(resources, cache)
    latest, latest_masters = _series_index(old.current, cache)
    remembered, _ = _series_index(old.series_previous, cache)
    result = []
    total_bytes = 0
    for key in current:
        before, after = latest_masters.get(key), current_masters.get(key)
        changed = (before is not None and after is not None and before[0] != after[0]
                   and (before[1] or after[1]))
        # Capture from one complete observed inventory. Never reconstruct a
        # former series from ordinary previous records of different vintages.
        group = latest.get(key, ()) if changed else remembered.get(key, ())
        for resource in group:
            scoped = _scoped_resource(resource, key[2], cache)
            total_bytes += len(scoped.data.encode("utf-8"))
            if total_bytes > MAX_SERIES_HISTORY_BYTES:
                raise StateError("История серий превышает допустимый размер; прежний снимок сохранён.")
            result.append(scoped)
    return result


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
    return Snapshot(
        current=resources, previous=previous,
        series_previous=_advance_series_history(resources, old, cache),
    )


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
    schema_version = None
    resources = {}
    fields = {"source_id", "calendar_id", "calendar_name", "data"}
    while True:
        token = next(tokens)
        if token[:2] == ("", "end_map"):
            break
        if token[:2] != ("", "map_key"):
            raise ValueError("invalid snapshot key")
        name = token[2]
        if name not in {"schema_version", "current", "previous", "series_previous"} or name in seen:
            raise ValueError("unknown or duplicate snapshot key")
        seen.add(name)
        if name == "schema_version":
            schema_version = expect(name, "number")
            if type(schema_version) is not int or schema_version not in {1, 2}:
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
    required = {"schema_version", "current", "previous"}
    if schema_version == 2:
        required.add("series_previous")
    if seen != required or next(tokens, None) is not None:
        raise ValueError("incomplete snapshot or trailing data")
    return Snapshot(resources["current"], resources["previous"], resources.get("series_previous", []))


def read_snapshot(path: Path) -> Snapshot:
    if not path.exists():
        return Snapshot(current=[], previous=[])
    try:
        # Three bounded inventories plus JSON escaping. The writer enforces
        # the same limit before replacement, so it cannot create unreadable state.
        if path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("oversized snapshot")
        with path.open("rb") as stream:
            state = _stream_snapshot(stream)
        cache = {}
        _index(state.current, cache)
        _index(state.previous, cache)
        _index(state.series_previous, cache)
        if any(len(_metadata(item.data, cache)[3]) != 1 for item in state.series_previous):
            raise ValueError("ambiguous historical series scope")
        groups, masters = _series_index(state.series_previous, cache)
        if set(groups) != set(masters):
            raise ValueError("incomplete historical series")
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
        "schema_version": 2,
        "current": [encode(resource) for resource in snapshot.current],
        "previous": [encode(resource) for resource in snapshot.previous
                     if (resource.source_id, resource.calendar_id, resource.data) not in current_data],
        # Unchanged detached overrides are still essential to the old series.
        "series_previous": [encode(resource) for resource in snapshot.series_previous],
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
            if stream.tell() > MAX_SNAPSHOT_BYTES:
                raise StateError("Снимок календарей превышает допустимый размер; прежний файл сохранён.")
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
