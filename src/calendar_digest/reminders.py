"""Pure reminder preview from current calendar resources; no delivery or state.

Each UID is parsed once for the requested interval. Only current occurrence
starts participate: digest history, moved-from slots and cancellation archives
are not inputs to this planner. Recurrence expansion is bounded and streamed.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import re
from typing import Iterable

from icalendar import Calendar
import recurring_ical_events

from . import selection
from .presentation import _BIRTHDAY_TITLE, _normalized


MAX_WINDOW_DAYS = 31
MAX_OCCURRENCES = 25_000
MAX_REMINDERS = 50_000
MAX_SERIES_RECURRENCE_STEPS = 100_000
MAX_RECURRENCE_STEPS = 2_000_000

_BIRTHDAY_LABELS = frozenset({
    "birthday", "birthdays", "день рождения", "дни рождения", "др", "д.р.",
})
_BIRTHDAY_ABBREVIATION = re.compile(
    r"^(?:др\b|д\.\s*р\.(?=\s|$))|\(\s*др\.?\s*\)\s*$"
)
_PERIODIC = re.compile(r"\b(?:weekly|monthly|quarterly|yearly|daily|biweekly)\b", re.IGNORECASE)


class ReminderError(selection.SelectionError):
    """A safe planning failure, never containing calendar content."""


@dataclass(frozen=True)
class Reminder:
    source_id: str
    calendar_id: str
    calendar_name: str
    uid: str
    recurrence_id: str | None
    title: str
    event_start: str
    all_day: bool
    category: str
    rule: str
    notify_at: str
    classification_evidence: tuple[str, ...] = ()

    @property
    def stable_id(self) -> str:
        """Keep identity across moves, text edits and repeated previews."""
        identity = [self.source_id, self.calendar_id, self.uid, self.recurrence_id, self.rule]
        encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReminderPlan:
    reminders: list[Reminder]
    summary: dict


class _Budget:
    def __init__(self):
        self.total = 0

    def step(self):
        self.total += 1
        if self.total > MAX_RECURRENCE_STEPS:
            raise ReminderError("Раскрытие повторений превышает допустимый объём; план не сформирован.")


class _BoundedRule:
    """Stream dateutil rules rather than allocating its entire between() list.

Counting historical candidates also bounds dense rules whose start is far in
the past. The underlying rules use selection's disabled recurrence cache.
"""

    def __init__(self, rule, budget, series_steps):
        self.rule = rule
        self.budget = budget
        self.series_steps = series_steps
        self.until = rule.until

    def between(self, start, end, inc=False):
        for value in self.rule:
            self.budget.step()
            self.series_steps[0] += 1
            if self.series_steps[0] > MAX_SERIES_RECURRENCE_STEPS:
                raise ReminderError("Одна серия содержит слишком много повторений; план не сформирован.")
            if value > end or (value == end and not inc):
                return
            if value > start or (value == start and inc):
                yield value


class _StartOnlySeries(selection._UncachedSeries):
    def rrule_between(self, span_start, span_stop):
        # The normal library query looks back by the event's duration. Reminder
        # rules depend on its start, so that expansion is unnecessary here.
        yield from self.recurrence.rrule_between(span_start, span_stop)


def _birthday_evidence(series, component, title):
    master = series.components.get(None)
    event = component.event
    if "CATEGORIES" not in event and master is not None:
        event = master.event
    categories = {
        _normalized(str(category))
        for prop in selection._values(event, "CATEGORIES")
        for category in getattr(prop, "cats", ())
    }
    evidence = []
    if categories & _BIRTHDAY_LABELS:
        evidence.append("categories")
    if _normalized(series.calendar_name) in _BIRTHDAY_LABELS:
        evidence.append("calendar_name")
    normalized_title = _normalized(title)
    if _BIRTHDAY_TITLE.search(normalized_title) or _BIRTHDAY_ABBREVIATION.search(normalized_title):
        evidence.append("title")
    return tuple(evidence)


def _classification(series, component, control_source):
    event = selection._digest(series, component, recurring=component.rid is not None)
    evidence = _birthday_evidence(series, component, event.title)
    if evidence:
        return event, "birthdays", evidence
    if series.source_id == control_source:
        periodic = _PERIODIC.search(event.title) is not None
        return event, "periodic_control" if periodic else "control", ()
    return event, None, ()


def _inside(component, start, end):
    return start <= selection._instant(component.start) < end


def _occurrences(series, start, end, *, expand_master, budget):
    master = series.components.get(None)
    if master is not None and selection._is_recurring(master) and expand_master:
        calendar = Calendar()
        calendar.add_component(deepcopy(master.event))
        query = recurring_ical_events.of(
            calendar, keep_recurrence_attributes=True, skip_bad_series=False,
            components=(recurring_ical_events.ComponentsWithName("VEVENT", series=_StartOnlySeries),),
        )
        steps = [0]
        for expanded in query.series:
            expanded.recurrence.rrules = [
                _BoundedRule(rule, budget, steps) for rule in expanded.recurrence.rrules
            ]
            for occurrence in expanded.between(start, end):
                component = selection._normalize(occurrence.as_component(True))
                component.assumed |= master.assumed
                if component.rid is not None and selection._iso(component.rid) in series.components:
                    # An explicit cancellation or move suppresses the generated
                    # slot even if that override's actual start is elsewhere.
                    continue
                if _inside(component, start, end):
                    component.end = selection._original_end(master, component.rid or component.start)
                    yield component
    elif master is not None and not selection._is_recurring(master) and _inside(master, start, end):
        yield master
    for rid, component in series.components.items():
        if rid is not None and _inside(component, start, end):
            yield component


def _schedule(event, category, morning_time):
    start = selection._instant(
        datetime.fromisoformat(event.start) if not event.all_day else datetime.fromisoformat(event.start).date()
    )
    morning = datetime.combine(start.date(), morning_time, selection.MOSCOW)
    if category == "birthdays":
        return [("d3", morning - timedelta(days=3)), ("d1", morning - timedelta(days=1)), ("d0", morning)]
    result = [("d1", morning - timedelta(days=1))]
    if category == "control":
        result.insert(0, ("d3", morning - timedelta(days=3)))
    if event.all_day:
        result.append(("d0", morning))
    else:
        hours = 1 if category == "periodic_control" else 3
        result.append((f"h{hours}", start - timedelta(hours=hours)))
    return result


def _validate_window(window_start, window_end, morning_time, control_source):
    for value in (window_start, window_end):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ReminderError("Начало и конец периода должны содержать часовой пояс.")
    duration = window_end.astimezone(timezone.utc) - window_start.astimezone(timezone.utc)
    if duration <= timedelta(0) or duration > timedelta(days=MAX_WINDOW_DAYS):
        raise ReminderError("Период напоминаний должен быть положительным и не длиннее 31 суток.")
    if not isinstance(morning_time, time) or morning_time.tzinfo is not None:
        raise ReminderError("Утреннее время задаётся без часового пояса и применяется по Москве.")
    if not isinstance(control_source, str) or not control_source.strip():
        raise ReminderError("Не задан источник контрольных событий.")
    return window_start.astimezone(selection.MOSCOW), window_end.astimezone(selection.MOSCOW)


def plan_reminders(
    resources: Iterable[selection.CalendarResourceLike], *,
    window_start: datetime, window_end: datetime,
    control_source: str = "mailru_2", morning_time: time = time(9),
) -> ReminderPlan:
    """Plan notification times in the half-open interval, without sending.

    D−3/D−1 use Moscow calendar dates, not 72/24 elapsed hours. The timed
    offsets remain exact even across midnight. Birthday policy takes priority;
    periodic English title markers override only the selected control source.
    All participation states are retained, while STATUS:CANCELLED is excluded.

    Counts describe considered occurrence starts, not the complete inventory:
    unrelated recurring masters are not expanded. No partial result is returned
    if parsing, recurrence or output limits fail.
    """
    try:
        window_start, window_end = _validate_window(window_start, window_end, morning_time, control_source)
        # A D0 morning can follow a midnight/all-day start. D−3 can refer to any
        # time three calendar dates later. Filter notification timestamps last.
        starts_from = datetime.combine(window_start.date(), time.min, selection.MOSCOW)
        starts_until = datetime.combine(window_end.date() + timedelta(days=4), time.min, selection.MOSCOW)
        summary = {
            "series_total": 0, "series_without_policy": 0, "candidate_occurrences": 0,
            "cancelled_occurrences": 0, "unclassified_occurrences": 0,
            "timezone_assumed_occurrences": 0, "reminders_total": 0,
            "by_category": {"birthdays": 0, "control": 0, "periodic_control": 0},
            "by_rule": {"d3": 0, "d1": 0, "d0": 0, "h3": 0, "h1": 0},
            "classification_evidence": {"categories": 0, "calendar_name": 0, "title": 0},
        }
        budget = _Budget()
        distinct = {}
        for _, series in selection._iter_series(resources):
            summary["series_total"] += 1
            master = series.components.get(None)
            expand_master = master is not None and _classification(series, master, control_source)[1] is not None
            if not expand_master and not any(
                _classification(series, component, control_source)[1] is not None
                for rid, component in series.components.items() if rid is not None
            ):
                summary["series_without_policy"] += 1
            for component in _occurrences(series, starts_from, starts_until, expand_master=expand_master, budget=budget):
                summary["candidate_occurrences"] += 1
                if summary["candidate_occurrences"] > MAX_OCCURRENCES:
                    raise ReminderError("Слишком много экземпляров событий; план не сформирован.")
                event, category, evidence = _classification(series, component, control_source)
                if event.timezone_assumed:
                    summary["timezone_assumed_occurrences"] += 1
                if event.event_status.strip().upper() == "CANCELLED":
                    summary["cancelled_occurrences"] += 1
                    continue
                if category is None:
                    summary["unclassified_occurrences"] += 1
                    continue
                for name in evidence:
                    summary["classification_evidence"][name] += 1
                for rule, notify_at in _schedule(event, category, morning_time):
                    if not window_start <= notify_at < window_end:
                        continue
                    reminder = Reminder(
                        event.source_id, event.calendar_id, event.calendar_name, event.uid,
                        event.recurrence_id, event.title, event.start, event.all_day,
                        category, rule, notify_at.isoformat(), evidence,
                    )
                    previous = distinct.get(reminder.stable_id)
                    if previous is not None and previous != reminder:
                        raise ReminderError("Обнаружены противоречивые напоминания одного экземпляра.")
                    distinct[reminder.stable_id] = reminder
                    if len(distinct) > MAX_REMINDERS:
                        raise ReminderError("Слишком много напоминаний; план не сформирован.")
        reminders = sorted(distinct.values(), key=lambda item: (item.notify_at, item.stable_id))
        for reminder in reminders:
            summary["by_category"][reminder.category] += 1
            summary["by_rule"][reminder.rule] += 1
        summary["reminders_total"] = len(reminders)
        return ReminderPlan(reminders, summary)
    except ReminderError:
        raise
    except selection.SelectionError as exc:
        raise ReminderError(str(exc)) from None
    except Exception:
        raise ReminderError("Не удалось безопасно сформировать план напоминаний.") from None
