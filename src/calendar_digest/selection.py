"""Select Moscow-day events and explicit moves without discarding invitations."""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import re
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar, Event, vText
from icalendar.parser import Contentlines
import recurring_ical_events


MOSCOW = ZoneInfo("Europe/Moscow")


class SelectionError(Exception):
    """A selection failure whose message contains no event or account data."""


class CalendarResourceLike(Protocol):
    source_id: str
    calendar_id: str
    calendar_name: str
    account_email: str
    data: str | bytes


@dataclass(frozen=True)
class DigestEvent:
    source_id: str
    calendar_id: str
    calendar_name: str
    uid: str
    recurrence_id: str | None
    title: str
    start: str
    end: str
    all_day: bool
    event_status: str
    participation: str
    recurring: bool
    multi_day: bool
    original_start: str | None = None
    original_end: str | None = None
    move_basis: str | None = None
    location: str | None = None
    timezone_assumed: bool = False


@dataclass(frozen=True)
class SeriesInterval:
    start: str
    end: str | None
    all_day: bool


@dataclass(frozen=True)
class SeriesChangeNotice:
    """Compare actual day slots without claiming an occurrence-to-occurrence move."""

    source_id: str
    calendar_id: str
    calendar_name: str
    uid: str
    title: str
    old_anchor_start: str
    old_anchor_end: str | None
    new_anchor_start: str
    new_anchor_end: str | None
    old_recurring: bool
    new_recurring: bool
    old_intervals: list[SeriesInterval]
    new_intervals: list[SeriesInterval]


@dataclass(frozen=True)
class SelectionResult:
    events: list[DigestEvent]
    series_changes: list[SeriesChangeNotice]


@dataclass
class _Component:
    event: Event
    start: date | datetime
    end: date | datetime
    rid: date | datetime | None
    assumed: bool
    inferred_start: bool = False
    duration_days: int = 0


@dataclass
class _Series:
    source_id: str
    calendar_id: str
    calendar_name: str
    account_email: str
    uid: str
    components: dict[str | None, _Component] = field(default_factory=dict)


@dataclass(frozen=True)
class _PreviousMaster:
    start: date | datetime
    end: date | datetime
    recurring: bool
    assumed: bool


class _UncachedSeries(recurring_ical_events.Series):
    """Keep recurrence rules intact without caching every historical date."""

    class RecurrenceRules(recurring_ical_events.Series.RecurrenceRules):
        def rrulestr(self, text):
            original = super().rrulestr(text)
            rule = original.replace(cache=False)
            rule.until = original.until
            rule.string = getattr(original, "string", text)
            return rule


def _values(event, name):
    value = event.get(name)
    return value if isinstance(value, list) else ([] if value is None else [value])


def _single(event, name, required=False):
    values = _values(event, name)
    if len(values) > 1 or (required and not values):
        raise SelectionError("Событие содержит отсутствующее или неоднозначное обязательное поле.")
    return values[0] if values else None


def _temporal(value, params=None):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            if params and params.get("TZID"):
                raise SelectionError("Не удалось определить часовой пояс события.")
            return value.replace(tzinfo=MOSCOW), True
        return value, False
    if type(value) is date:
        return value, False
    raise SelectionError("Событие содержит некорректную дату или время.")


def _iso(value):
    return value.astimezone(MOSCOW).isoformat() if isinstance(value, datetime) else value.isoformat()


def _instant(value):
    return value.astimezone(MOSCOW) if isinstance(value, datetime) else datetime.combine(value, time.min, MOSCOW)


def _same_type(left, right):
    return isinstance(left, datetime) == isinstance(right, datetime)


def _interval(event):
    start = _single(event, "DTSTART", required=True).dt
    end_prop = _single(event, "DTEND")
    duration_prop = _single(event, "DURATION")
    if end_prop is not None and duration_prop is not None:
        raise SelectionError("Событие одновременно задаёт конец и длительность.")
    if end_prop is not None:
        end = end_prop.dt
    elif duration_prop is not None:
        duration = duration_prop.dt
        if not isinstance(duration, timedelta) or duration < timedelta(0):
            raise SelectionError("Событие содержит некорректную длительность.")
        if not isinstance(start, datetime) and (duration.seconds or duration.microseconds):
            raise SelectionError("Длительность события на весь день должна состоять из целых дней.")
        end = start + duration
    else:
        end = start if isinstance(start, datetime) else start + timedelta(days=1)
    if not _same_type(start, end) or _instant(end) < _instant(start):
        raise SelectionError("Событие содержит некорректный интервал времени.")
    if not isinstance(start, datetime) and end == start:
        raise SelectionError("Событие на весь день имеет пустой интервал.")
    return start, end


def _normalize(raw, *, duration_days=0):
    event = deepcopy(raw)
    if event.errors:
        raise SelectionError("Не удалось разобрать поля события iCalendar.")
    uid = _single(event, "UID", required=True)
    if not str(uid).strip():
        raise SelectionError("Событие не содержит идентификатор UID.")
    assumed = False
    for name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
        prop = _single(event, name)
        if prop is not None:
            if name == "RECURRENCE-ID" and prop.params.get("RANGE"):
                raise SelectionError("Переносы серии с RECURRENCE-ID RANGE пока не поддерживаются.")
            value, floating = _temporal(prop.dt, prop.params)
            prop.dt = value
            assumed |= floating
    for name in ("EXDATE", "RDATE"):
        for prop in _values(event, name):
            for item in prop.dts:
                value = item.dt
                if isinstance(value, tuple):
                    if name != "RDATE" or len(value) != 2:
                        raise SelectionError("Событие содержит некорректную дату повторения.")
                    begin, floating = _temporal(value[0], prop.params)
                    assumed |= floating
                    if isinstance(value[1], timedelta):
                        finish = begin + value[1]
                    else:
                        finish, floating = _temporal(value[1], prop.params)
                        assumed |= floating
                    if not _same_type(begin, finish) or _instant(finish) <= _instant(begin):
                        raise SelectionError("Событие содержит некорректный период повторения.")
                    item.dt = (begin, finish)
                else:
                    item.dt, floating = _temporal(value, prop.params)
                    assumed |= floating
    rid_prop = _single(event, "RECURRENCE-ID")
    rid = rid_prop.dt if rid_prop is not None else None
    inferred = False
    if "DTSTART" not in event and str(event.get("STATUS", "")).upper() == "CANCELLED" and rid is not None:
        event.add("DTSTART", rid)
        inferred = True
    start, end = _interval(event)
    if rid is not None and not _same_type(start, rid):
        raise SelectionError("Дата экземпляра и RECURRENCE-ID имеют разные типы.")
    component = _Component(event, start, end, rid, assumed, inferred, duration_days)
    if "DURATION" in event:
        component.end = _duration_end(component, start)
    return component


def _nominal_duration_days(data):
    """Preserve P1D versus PT24H, which icalendar normalizes to one timedelta."""
    stack = []
    result = []
    for line in Contentlines.from_ical(data):
        if not line:
            continue
        name, _, value = line.parts()
        name, value = name.upper(), str(value).upper()
        if name == "BEGIN":
            stack.append(value)
            if value == "VEVENT":
                result.append(0)
        elif name == "END":
            if not stack or stack.pop() != value:
                raise SelectionError("Календарь содержит некорректную структуру компонентов.")
        elif name == "DURATION" and stack and stack[-1] == "VEVENT":
            match = re.match(r"^\+?P(\d+)([DW])", value)
            if match:
                result[-1] = int(match[1]) * (7 if match[2] == "W" else 1)
    return result


def _revision(component):
    event = component.event
    sequence = int(_single(event, "SEQUENCE") or 0)
    if sequence < 0:
        raise SelectionError("Событие содержит некорректную версию.")
    timestamp = _single(event, "LAST-MODIFIED") or _single(event, "DTSTAMP")
    if timestamp is None:
        return sequence, ""
    value, _ = _temporal(timestamp.dt, timestamp.params)
    if not isinstance(value, datetime):
        raise SelectionError("Событие содержит некорректное время обновления.")
    return sequence, _iso(value)


def _read_series(resources, *, only_key=None, masters_only=False):
    result = {}
    for resource in resources:
        calendar = Calendar.from_ical(resource.data)
        if calendar.name != "VCALENDAR" or calendar.errors:
            raise SelectionError("Не удалось разобрать календарь iCalendar.")
        events = calendar.walk("VEVENT")
        durations = _nominal_duration_days(resource.data)
        if len(events) != len(durations):
            raise SelectionError("Календарь содержит некорректную структуру событий.")
        for raw, duration_days in zip(events, durations, strict=True):
            if masters_only and raw.get("RECURRENCE-ID") is not None:
                # The legacy per-resource snapshot can mix detached overrides
                # from different observations. Only master intervals are used
                # for standalone move history; coherent series history and all
                # current resources still take the normal, strict path.
                continue
            component = _normalize(raw, duration_days=duration_days)
            uid = str(component.event["UID"])
            key = (resource.source_id, resource.calendar_id, uid)
            if only_key is not None and key != only_key:
                continue
            series = result.setdefault(key, _Series(
                resource.source_id, resource.calendar_id, resource.calendar_name,
                resource.account_email, uid,
            ))
            rid_key = _iso(component.rid) if component.rid is not None else None
            previous = series.components.get(rid_key)
            if previous is None or _revision(component) > _revision(previous):
                series.components[rid_key] = component
            elif _revision(component) == _revision(previous) and (
                component.event.to_ical() != previous.event.to_ical()
                or component.duration_days != previous.duration_days
            ):
                raise SelectionError("Обнаружены противоречивые версии одного экземпляра события.")
    return result


def _resource_uids(data, *, timezones=None):
    """Index raw resources without retaining parsed calendars for the archive."""
    stack = []
    uids = set()
    event_uids = []
    root_seen = False
    for line in Contentlines.from_ical(data):
        if not line:
            continue
        name, params, value = line.parts()
        name = name.upper()
        if timezones is not None and stack:
            if stack[-1] == "VTIMEZONE" and name == "TZID":
                timezones.add(str(vText.from_ical(value)))
            elif stack[-1] == "VEVENT" and name in {"DTSTART", "DTEND", "RECURRENCE-ID", "RDATE", "EXDATE"} and params.get("TZID"):
                timezones.add(str(params["TZID"]))
        if name == "BEGIN":
            kind = str(value).upper()
            if not stack:
                if root_seen or kind != "VCALENDAR":
                    raise SelectionError("Не удалось разобрать календарь iCalendar.")
                root_seen = True
            stack.append(kind)
            if kind == "VEVENT":
                event_uids.append([])
        elif name == "END":
            if not stack or stack.pop() != str(value).upper():
                raise SelectionError("Календарь содержит некорректную структуру событий.")
            if str(value).upper() == "VEVENT":
                values = event_uids.pop()
                if len(values) != 1 or not values[0].strip():
                    raise SelectionError("Событие содержит отсутствующее или неоднозначное обязательное поле.")
                uids.add(values[0])
        elif stack and stack[-1] == "VEVENT" and name == "UID":
            event_uids[-1].append(str(vText.from_ical(value)))
    if stack or not root_seen:
        raise SelectionError("Календарь содержит некорректную структуру событий.")
    return uids


def _group_resources(resources):
    groups = {}
    checked_timezones = set()
    for resource in resources:
        timezones = set()
        uids = _resource_uids(resource.data, timezones=timezones)
        custom_zone = False
        for identifier in timezones - checked_timezones:
            try:
                ZoneInfo(identifier)
            except (ZoneInfoNotFoundError, ValueError):
                custom_zone = True
        if not uids or custom_zone:
            # Retain the full parser's validation for resources with no VEVENT.
            # Unknown custom TZIDs must also be registered/validated in the
            # original current-resource order, before parsing old snapshots.
            # icalendar caches custom TZIDs by their first observed definition.
            _read_series([resource])
        checked_timezones.update(timezones)
        for uid in uids:
            key = (resource.source_id, resource.calendar_id, uid)
            groups.setdefault(key, []).append(resource)
    return groups


def _iter_grouped_series(groups):
    """Parse only one UID group at a time, including separately stored overrides."""
    for key, group in groups.items():
        parsed = _read_series(group, only_key=key)
        if key not in parsed:
            raise SelectionError("Не удалось однозначно определить идентификатор события.")
        yield key, parsed[key]


def _iter_series(resources):
    yield from _iter_grouped_series(_group_resources(resources))


def _previous_masters(resources):
    """History needs intervals, not a second complete iCalendar object graph."""
    result = {}
    for key, group in _group_resources(resources).items():
        series = _read_series(group, only_key=key, masters_only=True).get(key)
        if series is None:
            continue
        master = series.components.get(None)
        if master is not None:
            result[key] = _PreviousMaster(master.start, master.end, _is_recurring(master), master.assumed)
    return result


def _is_recurring(component):
    return component is not None and bool(component.event.get("RRULE") or component.event.get("RDATE"))


def _overlaps(start, end, day_start, day_end):
    begin = _instant(start)
    finish = _instant(end) if end is not None else begin
    if finish == begin:
        return day_start <= begin < day_end
    return begin < day_end and finish > day_start


def _participation(event, account_email):
    def address(value):
        value = str(value).strip().casefold()
        return value[7:] if value.startswith("mailto:") else value

    email = address(account_email)
    statuses = {
        str(attendee.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
        for attendee in _values(event, "ATTENDEE")
        if email and address(attendee) == email
    }
    return statuses.pop() if len(statuses) == 1 else "UNKNOWN"


def _duration_end(master, start):
    if not isinstance(start, datetime):
        return start + (master.end - master.start)
    if "DURATION" in master.event:
        # Calendar days/weeks are nominal; the hours/minutes/seconds part is
        # elapsed time. Apply these separately across daylight-saving changes.
        local = start.astimezone(master.start.tzinfo)
        nominal = timedelta(days=master.duration_days)
        remaining = master.event["DURATION"].dt - nominal
        return ((local + nominal).astimezone(timezone.utc) + remaining).astimezone(master.start.tzinfo)
    # DTEND specifies the exact elapsed duration, not a wall-clock difference.
    elapsed = master.end.astimezone(timezone.utc) - master.start.astimezone(timezone.utc)
    return (start.astimezone(timezone.utc) + elapsed).astimezone(master.start.tzinfo)


def _original_end(master, rid):
    if master is None:
        return None
    if not _same_type(master.start, rid):
        raise SelectionError("Серия и RECURRENCE-ID имеют разные типы дат.")
    for prop in _values(master.event, "RDATE"):
        for item in prop.dts:
            if isinstance(item.dt, tuple) and _iso(item.dt[0]) == _iso(rid):
                return item.dt[1]
    return _duration_end(master, rid)


def _digest(series, component, *, recurring, original_start=None, original_end=None, move_basis=None, assumed_extra=False):
    event = component.event
    master = series.components.get(None)
    if component.rid is not None and master is not None:
        event = deepcopy(event)
        for name in ("SUMMARY", "LOCATION", "STATUS", "ATTENDEE"):
            if name not in event and name in master.event:
                event[name] = deepcopy(master.event[name])
    all_day = not isinstance(component.start, datetime)
    begin, finish = _instant(component.start), _instant(component.end)
    last_day = (finish - timedelta(microseconds=1)).date() if finish > begin else begin.date()
    return DigestEvent(
        source_id=series.source_id,
        calendar_id=series.calendar_id,
        calendar_name=series.calendar_name,
        uid=series.uid,
        recurrence_id=_iso(component.rid) if component.rid is not None else None,
        title=str(event.get("SUMMARY", "Без названия")),
        start=_iso(component.start),
        end=_iso(component.end),
        all_day=all_day,
        event_status=str(event.get("STATUS", "UNSPECIFIED")).upper(),
        participation=_participation(event, series.account_email),
        recurring=recurring,
        multi_day=last_day > begin.date(),
        original_start=_iso(original_start) if original_start is not None else None,
        original_end=_iso(original_end) if original_end is not None else None,
        move_basis=move_basis,
        location=str(event["LOCATION"]) if "LOCATION" in event else None,
        timezone_assumed=component.assumed or assumed_extra,
    )


def _select_series_day(series, day_start, day_end, old_master=None):
    """Select real current instances; historical series anchors are not moves."""
    selected = []
    master = series.components.get(None)
    recurring = _is_recurring(master) or any(rid is not None for rid in series.components)
    if master is not None and _is_recurring(master):
        calendar = Calendar()
        calendar.add_component(deepcopy(master.event))
        query = recurring_ical_events.of(
            calendar, keep_recurrence_attributes=True, skip_bad_series=False,
            components=(recurring_ical_events.ComponentsWithName("VEVENT", series=_UncachedSeries),),
        )
        occurrences = query.between(day_start, day_end)
        # The library's lookback accounts for the master's duration,
        # but not longer RDATE PERIODs. Query their starts explicitly
        # rather than expanding an arbitrarily large earlier span.
        period_starts = set()
        for prop in _values(master.event, "RDATE"):
            for item in prop.dts:
                if not isinstance(item.dt, tuple):
                    continue
                begin, finish = item.dt
                begin = _instant(begin)
                if begin < day_start and begin not in period_starts and _overlaps(begin, finish, day_start, day_end):
                    period_starts.add(begin)
                    occurrences.extend(query.between(begin, begin + timedelta(microseconds=1)))
        for event in occurrences:
            occurrence = _normalize(event)
            occurrence.assumed |= master.assumed
            if occurrence.rid is not None and _iso(occurrence.rid) in series.components:
                continue
            occurrence.end = _original_end(master, occurrence.rid or occurrence.start)
            if _overlaps(occurrence.start, occurrence.end, day_start, day_end):
                selected.append(_digest(series, occurrence, recurring=True))
    elif master is not None:
        moved = old_master is not None and not old_master.recurring and _iso(master.start) != _iso(old_master.start)
        if _overlaps(master.start, master.end, day_start, day_end) or (
            moved and _overlaps(old_master.start, old_master.end, day_start, day_end)
        ):
            selected.append(_digest(
                series, master, recurring=recurring,
                original_start=old_master.start if moved else None,
                original_end=old_master.end if moved else None,
                move_basis="previous_snapshot" if moved else None,
                assumed_extra=old_master.assumed if moved else False,
            ))
    for rid_key, component in series.components.items():
        if rid_key is None:
            continue
        original_end = _original_end(master, component.rid)
        if component.inferred_start and "DTEND" not in component.event and "DURATION" not in component.event and original_end is not None:
            component.end = original_end
        moved = _iso(component.rid) != _iso(component.start)
        if _overlaps(component.start, component.end, day_start, day_end) or (
            moved and _overlaps(component.rid, original_end, day_start, day_end)
        ):
            selected.append(_digest(
                series, component, recurring=True,
                original_start=component.rid if moved else None,
                original_end=original_end if moved else None,
                move_basis="recurrence_id" if moved else None,
                assumed_extra=master.assumed if moved and master is not None else False,
            ))
    return selected


def _distinct_events(selected):
    # Duplicate resources and duplicate RRULE/RDATE results represent one
    # instance only within its source and calendar, never across sources.
    distinct = {}
    for event in selected:
        identity = (event.source_id, event.calendar_id, event.uid, event.recurrence_id)
        if identity in distinct and distinct[identity] != event:
            raise SelectionError("Обнаружены противоречивые данные одного экземпляра события.")
        distinct[identity] = event
    return sorted(distinct.values(), key=lambda event: (
        not event.all_day, event.start, event.source_id, event.calendar_id,
        event.uid, event.recurrence_id or "",
    ))


def _day_window(day):
    if type(day) is not date:
        raise SelectionError("Для дайджеста нужна календарная дата без времени.")
    start = datetime.combine(day, time.min, MOSCOW)
    return start, start + timedelta(days=1)


def _select_grouped_day(current_groups, previous, day_start, day_end):
    selected = []
    for key, series in _iter_grouped_series(current_groups):
        selected.extend(_select_series_day(series, day_start, day_end, previous.get(key)))
    return _distinct_events(selected)


def select_day(
    resources: Iterable[CalendarResourceLike], day: date,
    previous_resources: Iterable[CalendarResourceLike] | None = None,
) -> list[DigestEvent]:
    """Include current events intersecting the day and explicit moved-away slots.

    Detached RECURRENCE-ID overrides take precedence, including over EXDATE.
    Historical master DTSTART changes never become instance-level moves. Use
    select_day_with_changes with coherent history for separate series notices.
    Missing snapshot records never imply cancellation.
    """
    try:
        day_start, day_end = _day_window(day)
        current_groups = _group_resources(resources)
        previous = _previous_masters(previous_resources) if previous_resources is not None else {}
        return _select_grouped_day(current_groups, previous, day_start, day_end)
    except SelectionError:
        raise
    except Exception:
        raise SelectionError("Не удалось безопасно разобрать или развернуть события календаря.") from None


def _actual_day_intervals(series, day_start, day_end):
    intervals = set()
    for event in _distinct_events(_select_series_day(series, day_start, day_end)):
        # Explicit moved-away rows belong in the digest but are not an actual
        # slot on this day. Likewise, cancelled slots are not a current schedule.
        if event.event_status == "CANCELLED":
            continue
        parse = date.fromisoformat if event.all_day else datetime.fromisoformat
        if _overlaps(parse(event.start), parse(event.end), day_start, day_end):
            intervals.add(SeriesInterval(event.start, event.end, event.all_day))
    return sorted(intervals, key=lambda value: (value.start, value.end or "", value.all_day))


def _series_changes(current_groups, historical_resources, day_start, day_end):
    if historical_resources is None:
        return []
    # Store raw resource references, not a second parsed event graph. Only a
    # changed UID is expanded at a time, with its coherent old overrides intact.
    historical_groups = _group_resources(historical_resources)
    notices = []
    for key, group in historical_groups.items():
        if key not in current_groups:
            continue
        old_series = _read_series(group, only_key=key)[key]
        new_series = _read_series(current_groups[key], only_key=key)[key]
        old, new = old_series.components.get(None), new_series.components.get(None)
        if old is None or new is None or not (_is_recurring(old) or _is_recurring(new)):
            continue
        if _iso(old.start) == _iso(new.start):
            continue
        old_intervals = _actual_day_intervals(old_series, day_start, day_end)
        new_intervals = _actual_day_intervals(new_series, day_start, day_end)
        if old_intervals == new_intervals:
            continue
        notices.append(SeriesChangeNotice(
            source_id=new_series.source_id, calendar_id=new_series.calendar_id,
            calendar_name=new_series.calendar_name, uid=new_series.uid,
            title=str(new.event.get("SUMMARY", "Без названия")),
            old_anchor_start=_iso(old.start), old_anchor_end=_iso(old.end),
            new_anchor_start=_iso(new.start), new_anchor_end=_iso(new.end),
            old_recurring=_is_recurring(old), new_recurring=_is_recurring(new),
            old_intervals=old_intervals, new_intervals=new_intervals,
        ))
    return sorted(notices, key=lambda notice: (notice.source_id, notice.calendar_id, notice.uid))


def select_day_with_changes(
    resources: Iterable[CalendarResourceLike], day: date,
    previous_resources: Iterable[CalendarResourceLike] | None = None,
    series_previous_resources: Iterable[CalendarResourceLike] | None = None,
) -> SelectionResult:
    """Select events and independently compare complete historical series.

    Legacy per-resource move history cannot prove the full old recurrence set;
    only explicitly supplied coherent series history produces exact notices.
    """
    try:
        day_start, day_end = _day_window(day)
        # Register current custom time zones before reading any old resource,
        # matching select_day's established parsing precedence.
        current_groups = _group_resources(resources)
        previous = _previous_masters(previous_resources) if previous_resources is not None else {}
        events = _select_grouped_day(current_groups, previous, day_start, day_end)
        changes = _series_changes(current_groups, series_previous_resources, day_start, day_end)
        return SelectionResult(events, changes)
    except SelectionError:
        raise
    except Exception:
        raise SelectionError("Не удалось безопасно сравнить расписание повторяющихся событий.") from None
