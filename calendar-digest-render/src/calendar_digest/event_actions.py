"""Prepare one CalDAV event change without transport, scheduling or notifications."""

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from icalendar import Calendar, vDDDLists, vDDDTypes
from icalendar.parser import Contentlines

from .selection import DigestEvent, MOSCOW, _nominal_duration_days, _normalize, select_day


class SafeActionError(Exception):
    """An actionable message containing no raw calendar or account data."""


class ActionConflict(SafeActionError):
    """The selected occurrence no longer matches the signed preview."""


@dataclass(frozen=True)
class EventTarget:
    source_id: str
    calendar_id: str
    uid: str
    expected_start: str
    expected_end: str
    recurrence_id: str | None = None
    expected_event_status: str = "UNSPECIFIED"
    expected_participation: str = "UNKNOWN"


@dataclass(frozen=True)
class Change:
    data: str
    summary: str
    before_start: str
    before_end: str
    after_start: str
    after_end: str
    operation: str
    title: str


class _SourceDuration(vDDDTypes):
    """Retain exact PT24H versus nominal P1D in untouched properties."""

    def __init__(self, original, source):
        super().__init__(original.dt, deepcopy(original.params))
        self.source = source

    def to_ical(self):
        return self.source.encode("utf-8")


class _SourceDateList(vDDDLists):
    def __init__(self, original, source):
        super().__init__([item.dt for item in original.dts], deepcopy(original.params))
        self.source = source

    def to_ical(self):
        return self.source.encode("utf-8")


def _preserve_duration_tokens(calendar, data):
    """The parser otherwise converts duration tokens, even in other events."""
    components = iter(calendar.walk())
    stack = []
    for line in Contentlines.from_ical(data):
        if not line:
            continue
        name, _, value = line.parts()
        name = name.upper()
        if name == "BEGIN":
            component = next(components)
            if component.name != str(value).upper():
                raise SafeActionError("Календарь содержит неоднозначную структуру компонентов.")
            stack.append((component, {}))
        elif name == "END":
            if not stack or stack.pop()[0].name != str(value).upper():
                raise SafeActionError("Календарь содержит некорректную структуру компонентов.")
        elif stack and name in {"DURATION", "TRIGGER", "RDATE"}:
            component, seen = stack[-1]
            index = seen.get(name, 0)
            seen[name] = index + 1
            properties = _properties(component, name)
            original = properties[index]
            if name in {"DURATION", "TRIGGER"} and isinstance(getattr(original, "dt", None), timedelta):
                properties[index] = _SourceDuration(original, str(value))
            elif name == "RDATE" and any(isinstance(item.dt, tuple) for item in original.dts):
                properties[index] = _SourceDateList(original, str(value))
            component[name] = properties if len(properties) > 1 else properties[0]


def _parse(value: str) -> date | datetime:
    if not isinstance(value, str):
        raise SafeActionError("Дата и время должны быть строкой ISO.")
    if len(value) == 10:
        return date.fromisoformat(value)
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise SafeActionError("Для времени события требуется явный часовой пояс.")
    return result.astimezone(MOSCOW)


def _iso(value):
    return value.astimezone(MOSCOW).isoformat() if isinstance(value, datetime) else value.isoformat()


def _address(value):
    result = str(value).strip().casefold()
    return result[7:] if result.startswith("mailto:") else result


def _properties(component, name):
    value = component.get(name)
    return value if isinstance(value, list) else ([] if value is None else [value])


def _rid(component):
    values = _properties(component, "RECURRENCE-ID")
    if len(values) > 1:
        raise SafeActionError("У события неоднозначный идентификатор повторения.")
    if not values:
        return None
    prop = values[0]
    if prop.params.get("RANGE"):
        raise SafeActionError("Изменение серии с RECURRENCE-ID RANGE пока не поддерживается.")
    value = prop.dt
    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        if prop.params.get("TZID"):
            raise SafeActionError("Не удалось определить часовой пояс повторения.")
        value = value.replace(tzinfo=MOSCOW)
    return _iso(value)


def _calendar(data, target):
    calendar = Calendar.from_ical(data)
    if calendar.name != "VCALENDAR" or calendar.errors:
        raise SafeActionError("Не удалось разобрать календарь для изменения.")
    _preserve_duration_tokens(calendar, data)
    components = []
    for event in calendar.walk("VEVENT"):
        if event.errors:
            raise SafeActionError("Календарь содержит некорректные поля события.")
        if str(event.get("UID", "")) == target.uid:
            _rid(event)  # Validate RANGE before considering an individual edit.
            components.append(event)
    if not components:
        raise ActionConflict("Событие больше не найдено. Обновите дайджест.")
    return calendar, components


def _resolve(data, target, account_email, reference_start=None) -> DigestEvent:
    start = _parse(reference_start or target.expected_start)
    day = start.date() if isinstance(start, datetime) else start
    resource = SimpleNamespace(
        source_id=target.source_id, calendar_id=target.calendar_id,
        calendar_name="", account_email=account_email, data=data,
    )
    wanted_rid = _iso(_parse(target.recurrence_id)) if target.recurrence_id is not None else None
    matches = [
        event for event in select_day([resource], day)
        if event.uid == target.uid and event.recurrence_id == wanted_rid
    ]
    if len(matches) != 1:
        raise ActionConflict("Экземпляр события изменился или больше не найден. Обновите дайджест.")
    return matches[0]


def _as_original_type(value, original):
    """Keep the source DTSTART's date/floating/zoned representation."""
    original_value = original.dt
    if isinstance(original_value, datetime):
        if not isinstance(value, datetime):
            raise SafeActionError("Тип даты нового времени не совпадает с исходным событием.")
        if original_value.tzinfo is None or original_value.utcoffset() is None:
            if original.params.get("TZID"):
                raise SafeActionError("Не удалось определить исходный часовой пояс события.")
            return value.astimezone(MOSCOW).replace(tzinfo=None)
        local = value.astimezone(original_value.tzinfo)
        if local.fold and local.replace(fold=0).utcoffset() != local.utcoffset():
            # A TZID wall time cannot encode the second fold explicitly. UTC
            # retains the requested instant instead of moving it an hour early.
            return value.astimezone(timezone.utc)
        return local
    if isinstance(value, datetime):
        raise SafeActionError("Событие на весь день требует даты без времени.")
    return value


def _set_interval(component, start, end, original_start):
    for name in ("DTSTART", "DTEND", "DURATION"):
        component.pop(name, None)
    source_start = _as_original_type(start, original_start)
    source_end = _as_original_type(end, original_start)
    if isinstance(source_start, datetime) and (
        source_start.tzinfo is timezone.utc or source_end.tzinfo is timezone.utc
    ):
        source_start, source_end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    component.add("DTSTART", source_start)
    # RFC 5545 point events have no end; explicit DTEND must follow DTSTART.
    if not isinstance(start, datetime) or end != start:
        component.add("DTEND", source_end)


def _editable(calendar, components, target, resolved):
    wanted = _iso(_parse(target.recurrence_id)) if target.recurrence_id is not None else None
    exact = [component for component in components if _rid(component) == wanted]
    if len(exact) > 1:
        raise SafeActionError("В календаре несколько версий выбранного экземпляра; изменение остановлено.")
    if exact:
        component = exact[0]
        if wanted is None and (component.get("RRULE") or component.get("RDATE")):
            raise SafeActionError("Для повторяющейся серии выберите конкретный экземпляр.")
        if wanted is not None:
            masters = [item for item in components if _rid(item) is None]
            if len(masters) > 1:
                raise SafeActionError("В календаре несколько основных версий серии; изменение остановлено.")
            if masters:
                # A sparse override inherits participation and organizer data.
                # Never mistake an omitted ATTENDEE for a personal event.
                for name in ("ATTENDEE", "ORGANIZER", "STATUS", "SUMMARY", "LOCATION"):
                    if name not in component and name in masters[0]:
                        component[name] = deepcopy(masters[0][name])
        return component
    masters = [component for component in components if _rid(component) is None]
    if wanted is None or len(masters) != 1:
        raise ActionConflict("Не удалось однозначно найти выбранный экземпляр.")
    master = masters[0]
    if not master.get("RRULE") and not master.get("RDATE"):
        raise ActionConflict("У события больше нет выбранного повторения.")
    component = deepcopy(master)
    original_start = deepcopy(master["DTSTART"])
    for name in ("RRULE", "RDATE", "EXDATE", "EXRULE", "RECURRENCE-ID"):
        component.pop(name, None)
    component.add("RECURRENCE-ID", _as_original_type(_parse(wanted), original_start))
    _set_interval(component, _parse(resolved.start), _parse(resolved.end), original_start)
    calendar.add_component(component)
    return component


def _check_snapshot(resolved, target):
    expected_status = (target.expected_event_status or "UNSPECIFIED").strip().upper()
    expected_participation = (target.expected_participation or "UNKNOWN").strip().upper()
    if (
        resolved.start != _iso(_parse(target.expected_start))
        or resolved.end != _iso(_parse(target.expected_end))
        or resolved.event_status != expected_status
        or resolved.participation != expected_participation
    ):
        raise ActionConflict("Время или статус события изменились. Обновите дайджест перед действием.")
    if resolved.event_status == "CANCELLED":
        raise SafeActionError("Событие уже отменено; изменение через эту кнопку недоступно.")


def _rescheduled_interval(resolved, new_start, new_end):
    if new_start is None:
        raise SafeActionError("Укажите новое начало события.")
    start = _parse(new_start)
    previous_start, previous_end = _parse(resolved.start), _parse(resolved.end)
    if isinstance(start, datetime) != isinstance(previous_start, datetime):
        raise SafeActionError("При переносе сохраните тип события: время или весь день.")
    if new_end is not None:
        end = _parse(new_end)
    elif isinstance(start, datetime):
        duration = previous_end.astimezone(timezone.utc) - previous_start.astimezone(timezone.utc)
        end = (start.astimezone(timezone.utc) + duration).astimezone(MOSCOW)
    else:
        end = start + (previous_end - previous_start)
    if isinstance(end, datetime) != isinstance(start, datetime) or end < start:
        raise SafeActionError("Конец события должен соответствовать типу даты и быть не раньше начала.")
    if not isinstance(start, datetime) and end == start:
        raise SafeActionError("Конечная дата события на весь день должна быть позже начальной.")
    return start, end


def prepare_change(
    data: str, target: EventTarget, action: str, account_email: str,
    new_start: str | None = None, new_end: str | None = None,
    *, allow_organizer_updates: bool = False,
) -> Change:
    """Return modified ICS after the caller has established write ACL and ETag.

    Organizer changes with attendees require both separately established
    organizer authority and an exact ORGANIZER/account match. A writable copy
    of an invitation alone is not evidence of that authority.
    """
    try:
        if action not in {"confirm", "cancel", "reschedule"}:
            raise SafeActionError("Неизвестное действие с календарём.")
        if not account_email or not target.uid:
            raise SafeActionError("Не удалось определить аккаунт или событие для действия.")
        if action != "reschedule" and (new_start is not None or new_end is not None):
            raise SafeActionError("Новое время допускается только при переносе события.")
        calendar, components = _calendar(data, target)
        if target.recurrence_id is None and any(c.get("RRULE") or c.get("RDATE") for c in components):
            raise SafeActionError("Для повторяющейся серии выберите конкретный экземпляр.")
        resolved = _resolve(data, target, account_email)
        _check_snapshot(resolved, target)
        component = _editable(calendar, components, target, resolved)
        attendees = _properties(component, "ATTENDEE")
        own_attendees = [attendee for attendee in attendees if _address(attendee) == _address(account_email)]
        if len(own_attendees) > 1:
            raise SafeActionError("В событии несколько записей вашего участия; действие остановлено.")
        organizers = _properties(component, "ORGANIZER")
        organizer_owned = (
            allow_organizer_updates and len(organizers) == 1
            and _address(organizers[0]) == _address(account_email)
        )
        after_start, after_end = resolved.start, resolved.end
        own_response = False
        if action == "confirm":
            if attendees:
                if not own_attendees:
                    raise SafeActionError("В приглашении не найден ваш адрес участника; подтвердить участие нельзя.")
                own_attendees[0].params["PARTSTAT"] = "ACCEPTED"
                operation, summary = "accept_invitation", "Участие подтверждено."
                own_response = True
            else:
                component["STATUS"] = "CONFIRMED"
                operation, summary = "confirm_event", "Событие подтверждено."
        elif action == "cancel":
            if attendees and not organizer_owned:
                if not own_attendees:
                    raise SafeActionError("Отмена встречи доступна организатору; права организатора не подтверждены.")
                own_attendees[0].params["PARTSTAT"] = "DECLINED"
                operation, summary = "decline_participation", "Участие отклонено."
                own_response = True
            else:
                component["STATUS"] = "CANCELLED"
                operation, summary = "cancel_event", "Событие отменено."
        else:
            if attendees and not organizer_owned:
                raise SafeActionError("Перенос встречи доступен организатору; права организатора не подтверждены.")
            start, end = _rescheduled_interval(resolved, new_start, new_end)
            original_start = deepcopy(component["DTSTART"])
            _set_interval(component, start, end, original_start)
            after_start, after_end = _iso(start), _iso(end)
            operation, summary = "reschedule", "Время события изменено."
        # A participant's reply must not increment the organizer's sequence.
        if not own_response:
            sequence = component.decoded("SEQUENCE", 0)
            if not isinstance(sequence, int) or sequence < 0:
                raise SafeActionError("У события некорректный номер версии.")
            component["SEQUENCE"] = sequence + 1
        now = datetime.now(timezone.utc)
        for name in ("DTSTAMP", "LAST-MODIFIED"):
            component.pop(name, None)
            component.add(name, now)
        prepared_data = calendar.to_ical().decode("utf-8")
        serialized = _resolve(prepared_data, target, account_email, after_start)
        if serialized.start != after_start or serialized.end != after_end:
            raise SafeActionError("Новое время не удалось однозначно сохранить в часовом поясе календаря.")
        return Change(
            data=prepared_data, summary=summary,
            before_start=resolved.start, before_end=resolved.end,
            after_start=after_start, after_end=after_end,
            operation=operation, title=resolved.title,
        )
    except SafeActionError:
        raise
    except Exception:
        raise SafeActionError("Не удалось безопасно подготовить изменение события.") from None


def _schedule_snapshot(data, target):
    """Compare scheduling semantics throughout the resource, without revisions.

    A successful single-instance readback must not conceal a changed master or
    sibling. Recurrence anchors retain their time zone: converting a wall-time
    RRULE to UTC can change future occurrences even if today's instant matches.
    """
    calendar, _ = _calendar(data, target)
    events = calendar.walk("VEVENT")
    duration_days = _nominal_duration_days(data)
    if len(events) != len(duration_days):
        raise SafeActionError("Календарь содержит некорректную структуру событий.")
    result = {}
    zone_definitions = {}
    for original_zone in calendar.walk("VTIMEZONE"):
        zone = deepcopy(original_zone)
        for component in zone.walk():
            for name in ("LAST-MODIFIED", "TZURL", "TZNAME"):
                component.pop(name, None)
        identifier = str(zone.get("TZID", ""))
        if identifier in zone_definitions:
            raise SafeActionError("Календарь содержит неоднозначное определение часового пояса.")
        zone_definitions[identifier] = zone.to_ical()
    for raw, nominal_days in zip(events, duration_days, strict=True):
        component = _normalize(raw, duration_days=nominal_days)
        event = component.event
        identity = (str(event["UID"]), _rid(event))
        if identity in result:
            raise SafeActionError("Календарь содержит несколько версий одного экземпляра.")
        rules = tuple(
            (name, tuple(sorted(
                tuple(sorted((key, tuple(sorted(str(item) for item in values)))
                             for key, values in prop.items()))
                for prop in _properties(event, name)
            )))
            for name in ("RRULE", "EXRULE")
        )
        dates = []
        for name in ("RDATE", "EXDATE"):
            values = []
            for prop in _properties(event, name):
                for item in prop.dts:
                    value = item.dt
                    values.append(tuple(_iso(part) for part in value)
                                  if isinstance(value, tuple) else (_iso(value),))
            dates.append((name, tuple(sorted(values))))
        recurring = bool(event.get("RRULE") or event.get("RDATE") or event.get("EXRULE"))
        if isinstance(component.start, datetime):
            elapsed = (component.end.astimezone(timezone.utc)
                       - component.start.astimezone(timezone.utc)).total_seconds()
            duration = event.get("DURATION")
            duration_semantics = (
                nominal_days,
                duration.dt.total_seconds() - nominal_days * 86400,
            ) if duration is not None else (0, elapsed)
            if event.get("RRULE") or event.get("EXRULE"):
                zone_id = str(raw["DTSTART"].params.get("TZID", ""))
                anchor_zone = (str(raw["DTSTART"].dt.tzinfo), zone_definitions.get(zone_id))
            else:
                anchor_zone = None
        else:
            duration_semantics = ((component.end - component.start).days, 0)
            anchor_zone = None
        result[identity] = (
            _iso(component.start), _iso(component.end),
            str(event.get("STATUS", "UNSPECIFIED")).strip().upper(),
            tuple(sorted((_address(attendee), str(attendee.params.get("PARTSTAT", "NEEDS-ACTION")).upper())
                         for attendee in _properties(event, "ATTENDEE"))),
            rules, tuple(dates), duration_semantics if recurring else None, anchor_zone,
        )
    return result


def verify_change(actual_data: str, target: EventTarget, change: Change, account_email: str) -> bool:
    """Check the occurrence and guard against unintended whole-series changes."""
    try:
        expected = _resolve(change.data, target, account_email, change.after_start)
        actual = _resolve(actual_data, target, account_email, change.after_start)
        if expected.start != change.after_start or expected.end != change.after_end:
            return False
        if _schedule_snapshot(actual_data, target) != _schedule_snapshot(change.data, target):
            return False
        return (
            actual.start, actual.end, actual.event_status, actual.participation,
        ) == (
            expected.start, expected.end, expected.event_status, expected.participation,
        )
    except Exception:
        return False
