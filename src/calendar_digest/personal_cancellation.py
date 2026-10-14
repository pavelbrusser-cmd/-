"""Prepare verified Mail.ru personal-event deletion or one-instance exclusion.

Mail.ru does not persist STATUS:CANCELLED reliably. The transport must first
persist ``event`` as a pending archive entry, and activate it only after the
conditional mutation has been read back successfully. This module never
performs network requests, writes an archive, or authorizes a resource ACL.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from types import SimpleNamespace

from .event_actions import (
    EventTarget, SafeActionError, _address, _as_original_type,
    _calendar, _check_snapshot, _iso, _parse, _properties, _resolve, _rid,
)
from .selection import DigestEvent, select_day


@dataclass(frozen=True)
class PersonalCancellation:
    method: str
    data: str
    event: DigestEvent
    summary: str
    before_start: str
    before_end: str
    after_start: str
    after_end: str
    operation: str
    title: str


def _check_personal(components, account_email, allow_organizer_updates):
    # A sparse override inherits participation from the master. Conversely, an
    # attendee on a different override makes this a mixed personal/invitation
    # series, for which deleting an occurrence is deliberately unsupported.
    for component in components:
        if _properties(component, "ATTENDEE"):
            raise SafeActionError(
                "Удаление доступно только личным событиям без участников."
            )
        organizers = _properties(component, "ORGANIZER")
        if organizers and (
            len(organizers) != 1 or not allow_organizer_updates
            or _address(organizers[0]) != _address(account_email)
        ):
            raise SafeActionError(
                "Удаление события недоступно: права организатора не подтверждены."
            )


def _assert_single_resource_event(calendar, component):
    """DELETE is safe only when the entire resource belongs to this event."""
    if any(
        child is not component and child.name != "VTIMEZONE"
        for child in calendar.subcomponents
    ):
        raise SafeActionError(
            "Ресурс содержит другие записи; удаление целого ресурса недоступно."
        )
    # VALARMs belong to the selected event. Unrecognized nested components
    # could hold independent records and must not be silently deleted.
    if any(child.name != "VALARM" or child.subcomponents for child in component.subcomponents):
        raise SafeActionError(
            "Событие содержит дополнительные записи; удаление недоступно."
        )
    for zone in (child for child in calendar.subcomponents if child.name == "VTIMEZONE"):
        if any(child.name not in {"STANDARD", "DAYLIGHT"} or child.subcomponents
               for child in zone.subcomponents):
            raise SafeActionError(
                "Ресурс содержит дополнительные записи; удаление недоступно."
            )


def _target_absent(data, target, account_email):
    resource = SimpleNamespace(
        source_id=target.source_id, calendar_id=target.calendar_id,
        calendar_name="", account_email=account_email, data=data,
    )
    wanted = _iso(_parse(target.recurrence_id))
    # Check both the visible (possibly moved) date and the original recurrence
    # slot. Removing only a moved override would otherwise resurrect the slot.
    for reference in {target.expected_start, target.recurrence_id}:
        value = _parse(reference)
        day = value.date() if isinstance(value, datetime) else value
        if any(
            item.uid == target.uid and item.recurrence_id == wanted
            for item in select_day([resource], day)
        ):
            return False
    return True


def _structural_snapshot(data, target):
    """Retain every property and component except observed provider metadata."""
    calendar, _ = _calendar(data, target)

    def snapshot(component):
        fields = []
        for name in component:
            if name.upper() in {"DTSTAMP", "X-MOZ-LASTACK"}:
                continue
            values = []
            for prop in _properties(component, name):
                encoded = prop.to_ical()
                params = getattr(prop, "params", None)
                values.append((encoded, params.to_ical() if params else b""))
            fields.append((name.upper(), tuple(sorted(values))))
        return (
            component.name, tuple(sorted(fields)),
            tuple(sorted(snapshot(child) for child in component.subcomponents)),
        )

    return snapshot(calendar)


def prepare_personal_cancellation(
    data: str, target: EventTarget, account_email: str,
    *, allow_organizer_updates: bool = False,
) -> PersonalCancellation:
    """Prepare a Mail.ru personal cancellation after caller verifies write ACL.

    Standalone events use DELETE; series always use PUT with EXDATE of the
    original slot and removal of only its exact override. No whole-series
    deletion and no STATUS:CANCELLED write are produced.
    """
    try:
        if target.source_id not in {"mailru_1", "mailru_2", "mailru_3"}:
            raise SafeActionError("Удаление личных событий поддерживается только для Mail.ru.")
        if not account_email or not target.uid:
            raise SafeActionError("Не удалось определить аккаунт или событие для действия.")
        calendar, components = _calendar(data, target)
        if any(not any(item is child for child in calendar.subcomponents) for item in components):
            raise SafeActionError("Календарь содержит неоднозначную структуру событий.")
        _check_personal(components, account_email, allow_organizer_updates)
        resolved = _resolve(data, target, account_email)
        _check_snapshot(resolved, target)
        masters = [item for item in components if _rid(item) is None]
        if len(masters) != 1:
            raise SafeActionError(
                "Для безопасной отмены требуется единственная основная запись события."
            )
        master = masters[0]
        recurrence_fields = {"RRULE", "RDATE", "EXRULE", "EXDATE"}
        if target.recurrence_id is None:
            if len(components) != 1 or any(name in master for name in recurrence_fields):
                raise SafeActionError("Для повторяющейся серии выберите конкретный экземпляр.")
            _assert_single_resource_event(calendar, master)
            method, prepared_data = "DELETE", ""
            operation = "delete_personal_event"
            summary = "Личное событие удалено из календаря; его отмена сохранена для дайджеста."
        else:
            if not master.get("RRULE") and not master.get("RDATE"):
                raise SafeActionError("У события не найдена основная повторяющаяся серия.")
            wanted = _iso(_parse(target.recurrence_id))
            exact = [item for item in components if _rid(item) == wanted]
            if len(exact) > 1:
                raise SafeActionError("В календаре несколько версий выбранного экземпляра.")
            original_start = deepcopy(master["DTSTART"])
            exclusion = _as_original_type(_parse(wanted), original_start)
            master.add("EXDATE", exclusion)
            if exact:
                calendar.subcomponents = [
                    child for child in calendar.subcomponents if child is not exact[0]
                ]
            sequence = master.decoded("SEQUENCE", 0)
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
                raise SafeActionError("У события некорректный номер версии.")
            master["SEQUENCE"] = sequence + 1
            now = datetime.now(timezone.utc)
            for name in ("DTSTAMP", "LAST-MODIFIED"):
                master.pop(name, None)
                master.add(name, now)
            prepared_data = calendar.to_ical().decode("utf-8")
            if not _target_absent(prepared_data, target, account_email):
                raise SafeActionError("Не удалось исключить только выбранное повторение события.")
            method, operation = "PUT", "exclude_personal_occurrence"
            summary = "Выбранное повторение исключено из календаря; его отмена сохранена для дайджеста."
        return PersonalCancellation(
            method=method, data=prepared_data,
            event=replace(resolved, event_status="CANCELLED"), summary=summary,
            before_start=resolved.start, before_end=resolved.end,
            after_start=resolved.start, after_end=resolved.end,
            operation=operation, title=resolved.title,
        )
    except SafeActionError:
        raise
    except Exception:
        raise SafeActionError("Не удалось безопасно подготовить отмену личного события.") from None


def verify_personal_cancellation(
    actual_data: str, target: EventTarget, plan: PersonalCancellation, account_email: str,
) -> bool:
    """Verify one-instance PUT; DELETE requires a transport-level fresh 404."""
    try:
        return (
            plan.method == "PUT" and target.recurrence_id is not None
            and _structural_snapshot(actual_data, target) == _structural_snapshot(plan.data, target)
            and _target_absent(actual_data, target, account_email)
        )
    except Exception:
        return False
