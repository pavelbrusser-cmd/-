"""Pure, bounded planning of individual email reminders for control events.

The personal push policy is independent. Only the verified control mailbox is
eligible here; each planned item contains one external calendar attendee.
"""

from calendar import monthrange
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re
from typing import Iterable

from icalendar import Calendar
import recurring_ical_events

from . import reminders, selection
from .push_timing import normalize_notification_time


CONTROL_SOURCE = "mailru_2"
CONTROL_ACCOUNT = "gpb.control@mail.ru"
MAX_WINDOW_DAYS = 31
MAX_OCCURRENCES = 25_000
MAX_REMINDERS = 50_000
MAX_SERIES_RECURRENCE_STEPS = 100_000
MAX_RECURRENCE_STEPS = 2_000_000

RULE_PRECEDENCE = ("h1", "d0", "d1", "d3", "w1", "w2", "m1", "m2", "m3", "m6", "y1", "y2")
_EXTRA_RULES = {
    "weekly": "w1", "biweekly": "w2", "monthly": "m1", "bimonthly": "m2",
    "quarterly": "m3", "semiannually": "m6", "yearly": "y1", "annually": "y1",
    "biennially": "y2",
}
_KEYWORDS = re.compile(r"\b(?:" + "|".join(_EXTRA_RULES) + r")\b", re.IGNORECASE)
_DAY_LEADS = {"d3": 3, "d1": 1, "w1": 7, "w2": 14}
_MONTH_LEADS = {"m1": 1, "m2": 2, "m3": 3, "m6": 6, "y1": 12, "y2": 24}
_LOCAL_PART = re.compile(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]+", re.ASCII)
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


class ControlEmailError(selection.SelectionError):
    """A safe planning error without event or participant data."""


@dataclass(frozen=True)
class ControlEmailReminder:
    source_id: str
    calendar_id: str = field(repr=False)
    calendar_name: str = field(repr=False)
    account_email: str = field(repr=False)
    uid: str = field(repr=False)
    recurrence_id: str | None = field(repr=False)
    recipient: str = field(repr=False)
    title: str = field(repr=False)
    event_start: str
    event_end: str
    all_day: bool
    location: str | None = field(repr=False)
    rule: str
    notify_at: str
    category: str = "control_email"
    recurring: bool = False

    @property
    def stable_id(self):
        # A pending item can move, while an accepted occurrence/rule is never
        # silently resent just because the start time or title changed.
        identity = [self.source_id, self.calendar_id, self.uid, self.recurrence_id, self.rule, self.recipient]
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ControlEmailPlan:
    reminders: list[ControlEmailReminder]
    summary: dict


def normalize_email(value):
    """Return one conservative ASCII addr-spec, never a display-name/address list."""
    if not isinstance(value, str) or not value.isascii() or len(value) > 254:
        return None
    value = value.casefold()
    if value != value.strip() or value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if (not local or len(local) > 64 or not _LOCAL_PART.fullmatch(local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        return None
    return value


def _attendee_email(value):
    raw = str(value)
    if not raw.casefold().startswith("mailto:"):
        return None
    # URI queries, lists and percent-encoded addresses are not explicit,
    # directly usable mailboxes. Do not infer recipients from their contents.
    raw = raw[7:]
    if any(marker in raw for marker in ("?", "#", "%")):
        return None
    return normalize_email(raw)


def _recipient_list(series, component, own_accounts):
    event = component.event
    master = series.components.get(None)
    if "ATTENDEE" not in event and component.rid is not None and master is not None:
        event = master.event
    permitted = set()
    excluded = set()
    for attendee in selection._values(event, "ATTENDEE"):
        email = _attendee_email(attendee)
        if email is None:
            continue
        status = str(attendee.params.get("PARTSTAT", "NEEDS-ACTION")).upper()
        kind = str(attendee.params.get("CUTYPE", "INDIVIDUAL")).upper()
        role = str(attendee.params.get("ROLE", "REQ-PARTICIPANT")).upper()
        if (email in own_accounts or status not in {"ACCEPTED", "TENTATIVE", "NEEDS-ACTION", "DELEGATED"}
                or kind not in {"INDIVIDUAL", "GROUP"} or role == "NON-PARTICIPANT"):
            excluded.add(email)
        else:
            permitted.add(email)
    # Conflicting duplicate attendee records fail closed for that address.
    return sorted(permitted - excluded)


def _shift_months(value: date, months: int):
    total = value.year * 12 + value.month - 1 + months
    year, zero_month = divmod(total, 12)
    month = zero_month + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def _extra_rules(title):
    return {_EXTRA_RULES[match.group().casefold()] for match in _KEYWORDS.finditer(title)}


def _single_line(value):
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())


def _merge_windows(windows):
    merged = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return merged


def _candidate_windows(series, window_start, window_end, eligible_components):
    first = window_start.date()
    last = window_end.date()
    day_start = lambda value: datetime.combine(value, time.min, selection.MOSCOW)
    # Covers D3/D1, all-day D0 and the hourly floor/cross-midnight cases.
    windows = [(day_start(first), day_start(last + timedelta(days=4)))]
    rules = set()
    for component in eligible_components:
        event = selection._digest(series, component, recurring=component.rid is not None)
        rules.update(_extra_rules(event.title))
    for rule in rules:
        if rule in _DAY_LEADS:
            delta = timedelta(days=_DAY_LEADS[rule])
            windows.append((day_start(first + delta), day_start(last + delta + timedelta(days=1))))
        else:
            # Calendar subtraction clamps at month end and is not invertible:
            # March 28..31 all map to February 28. Whole future months are
            # narrow, safe candidate windows; exact subtraction filters later.
            cursor = first.replace(day=1)
            while cursor <= last:
                future = _shift_months(cursor, _MONTH_LEADS[rule])
                windows.append((day_start(future), day_start(_shift_months(future, 1))))
                cursor = _shift_months(cursor, 1)
    return _merge_windows(windows)


def _inside_windows(value, windows):
    if isinstance(value, datetime) and value.tzinfo is None:
        moment = value.replace(tzinfo=selection.MOSCOW)
    else:
        moment = selection._instant(value)
    return any(start <= moment < end for start, end in windows)


class _Budget:
    total = 0

    def step(self):
        self.total += 1
        if self.total > MAX_RECURRENCE_STEPS:
            raise ControlEmailError("Раскрытие повторений превышает лимит; план писем не сформирован.")


class _WindowedRule:
    """Traverse a rule once, yielding only relevant disjoint date windows.

    dateutil still visits bounded history to reach a future occurrence. Events
    between a one-week and a two-year lead are never materialized as ICS or
    reminder records, and historical work is not repeated for each lead.
    """

    def __init__(self, rule, windows, budget, series_steps):
        self.rule = rule
        self.windows = windows
        self.budget = budget
        self.series_steps = series_steps
        self.until = rule.until

    def between(self, start, end, inc=False):
        for value in self.rule:
            self.budget.step()
            self.series_steps[0] += 1
            if self.series_steps[0] > MAX_SERIES_RECURRENCE_STEPS:
                raise ControlEmailError("Одна серия содержит слишком много повторений; план писем не сформирован.")
            if value > end or (value == end and not inc):
                return
            if (value > start or (value == start and inc)) and _inside_windows(value, self.windows):
                yield value


def _occurrences(series, windows, budget, *, expand_master):
    master = series.components.get(None)
    if master is not None and selection._is_recurring(master) and expand_master:
        calendar = Calendar()
        calendar.add_component(deepcopy(master.event))
        query = recurring_ical_events.of(
            calendar, keep_recurrence_attributes=True, skip_bad_series=False,
            components=(recurring_ical_events.ComponentsWithName("VEVENT", series=reminders._StartOnlySeries),),
        )
        steps = [0]
        for expanded in query.series:
            expanded.recurrence.rrules = [
                _WindowedRule(rule, windows, budget, steps) for rule in expanded.recurrence.rrules
            ]
            for occurrence in expanded.between(windows[0][0], windows[-1][1]):
                component = selection._normalize(occurrence.as_component(True))
                component.assumed |= master.assumed
                if component.rid is not None and selection._iso(component.rid) in series.components:
                    continue
                if _inside_windows(component.start, windows):
                    component.end = selection._original_end(master, component.rid or component.start)
                    yield component
    elif master is not None and expand_master and _inside_windows(master.start, windows):
        yield master
    for rid, component in series.components.items():
        if rid is not None and _inside_windows(component.start, windows):
            yield component


def _schedule(event):
    start = datetime.fromisoformat(event.start)
    if event.all_day:
        start = datetime.combine(start.date(), time.min, selection.MOSCOW)
    else:
        start = start.astimezone(selection.MOSCOW)
    morning = datetime.combine(start.date(), time(9), selection.MOSCOW)
    result = [("d3", morning - timedelta(days=3)), ("d1", morning - timedelta(days=1))]
    result.append(("d0", morning) if event.all_day else ("h1", start - timedelta(hours=1)))
    for rule in _extra_rules(event.title):
        if rule in _DAY_LEADS:
            moment = morning - timedelta(days=_DAY_LEADS[rule])
        else:
            moment = datetime.combine(_shift_months(start.date(), -_MONTH_LEADS[rule]), time(9), selection.MOSCOW)
        result.append((rule, moment))
    return [(rule, normalize_notification_time(moment)) for rule, moment in result]


def _validate_window(window_start, window_end):
    for value in (window_start, window_end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ControlEmailError("Период писем должен содержать часовой пояс.")
    duration = window_end.astimezone(timezone.utc) - window_start.astimezone(timezone.utc)
    if duration <= timedelta(0) or duration > timedelta(days=MAX_WINDOW_DAYS):
        raise ControlEmailError("Период писем должен быть положительным и не длиннее 31 суток.")
    return window_start.astimezone(selection.MOSCOW), window_end.astimezone(selection.MOSCOW)


def plan_control_emails(
    resources: Iterable[selection.CalendarResourceLike], *,
    window_start: datetime, window_end: datetime, own_accounts: Iterable[str],
) -> ControlEmailPlan:
    """Plan one recipient per item; no network, state mutation or delivery.

    All eligible occurrences receive D3/D1/T1h (all-day D0). English title
    keywords add calendar/week leads. Every candidate is filtered by the actual
    notification instant, so distant events may participate in a short window.
    """
    try:
        window_start, window_end = _validate_window(window_start, window_end)
        if isinstance(own_accounts, (str, bytes)):
            raise ControlEmailError("Собственные адреса должны быть заданы списком.")
        owned = {CONTROL_ACCOUNT}
        for raw in own_accounts:
            address = normalize_email(raw)
            if address is None:
                raise ControlEmailError("Некорректный собственный адрес; план писем не сформирован.")
            owned.add(address)
        scoped = []
        for resource in resources:
            if resource.source_id != CONTROL_SOURCE:
                continue
            if normalize_email(resource.account_email) != CONTROL_ACCOUNT:
                # A detached cancellation/move must never disappear from a
                # partial UID group just because its metadata is inconsistent.
                raise ControlEmailError("Не подтверждён аккаунт источника контрольных событий; план писем не сформирован.")
            scoped.append(resource)
        summary = {
            "series_total": 0, "candidate_occurrences": 0, "cancelled_occurrences": 0,
            "cancelled_series": 0, "without_external_recipients": 0,
            "series_without_external_recipients": 0,
            "timezone_assumed_occurrences": 0, "reminders_total": 0, "recipients_total": 0,
            "by_rule": {rule: 0 for rule in RULE_PRECEDENCE},
        }
        budget = _Budget()
        distinct = {}
        slots = {}
        priorities = {rule: index for index, rule in enumerate(RULE_PRECEDENCE)}
        for _, series in selection._iter_series(scoped):
            summary["series_total"] += 1
            master = series.components.get(None)
            if master is not None and str(master.event.get("STATUS", "")).upper() == "CANCELLED":
                summary["cancelled_series"] += 1
                continue
            eligible_components = [component for component in series.components.values()
                                   if _recipient_list(series, component, owned)
                                   and str(component.event.get("STATUS", "")).upper() != "CANCELLED"]
            if not eligible_components:
                summary["series_without_external_recipients"] += 1
                continue
            expand_master = master is not None and any(component is master for component in eligible_components)
            windows = _candidate_windows(series, window_start, window_end, eligible_components)
            for component in _occurrences(series, windows, budget, expand_master=expand_master):
                summary["candidate_occurrences"] += 1
                if summary["candidate_occurrences"] > MAX_OCCURRENCES:
                    raise ControlEmailError("Слишком много экземпляров; план писем не сформирован.")
                event = selection._digest(series, component, recurring=selection._is_recurring(master) or component.rid is not None)
                if event.timezone_assumed:
                    summary["timezone_assumed_occurrences"] += 1
                if event.event_status.strip().upper() == "CANCELLED":
                    summary["cancelled_occurrences"] += 1
                    continue
                recipients = _recipient_list(series, component, owned)
                if not recipients:
                    summary["without_external_recipients"] += 1
                    continue
                for rule, notify_at in _schedule(event):
                    if not window_start <= notify_at < window_end:
                        continue
                    for recipient in recipients:
                        item = ControlEmailReminder(
                            event.source_id, event.calendar_id, _single_line(event.calendar_name) or "Без названия", CONTROL_ACCOUNT,
                            event.uid, event.recurrence_id, recipient, _single_line(event.title) or "Без названия",
                            event.start, event.end, event.all_day, _single_line(event.location) if event.location is not None else None,
                            rule, notify_at.isoformat(), recurring=event.recurring,
                        )
                        prior = distinct.get(item.stable_id)
                        if prior is not None and prior != item:
                            raise ControlEmailError("Противоречивые экземпляры; план писем не сформирован.")
                        slot = (item.source_id, item.calendar_id, item.uid, item.recurrence_id, recipient, item.notify_at)
                        previous_slot = slots.get(slot)
                        if previous_slot is not None:
                            if priorities[previous_slot.rule] <= priorities[item.rule]:
                                continue
                            distinct.pop(previous_slot.stable_id, None)
                        slots[slot] = item
                        distinct[item.stable_id] = item
                        if len(distinct) > MAX_REMINDERS:
                            raise ControlEmailError("Слишком много писем; план не сформирован.")
        planned = sorted(distinct.values(), key=lambda item: (item.notify_at, item.stable_id))
        for item in planned:
            summary["by_rule"][item.rule] += 1
        summary["reminders_total"] = len(planned)
        summary["recipients_total"] = len({item.recipient for item in planned})
        return ControlEmailPlan(planned, summary)
    except ControlEmailError:
        raise
    except selection.SelectionError as error:
        raise ControlEmailError(str(error)) from None
    except Exception:
        raise ControlEmailError("Не удалось безопасно сформировать план писем.") from None
