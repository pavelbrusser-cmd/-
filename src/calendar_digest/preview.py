"""Local digest preview. No email transport or scheduler is invoked here."""

from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta
from enum import Enum
import fcntl
from html import escape
import json
import os
from pathlib import Path

from .calendar_collection import collect_all
from .cancellation_archive import merge_cancellations
from .local_state import advance_snapshot, read_snapshot, write_private, write_snapshot
from .selection import DigestEvent, select_day
from .email_html import render_email_html


class PreviewError(Exception):
    pass


class PreviewStage(Enum):
    """Fixed progress messages, containing no calendar or account data."""

    READ_STATE = "Читаем сохранённое состояние."
    COLLECT = "Читаем события календарей. Это может занять несколько минут."
    COMPARE = "Сравниваем события с предыдущим состоянием."
    SELECT = "Отбираем события и разворачиваем повторения."
    RENDER = "Формируем дайджест."
    SAVE = "Сохраняем дайджест и состояние."
    DONE = "Дайджест сохранён."


def _report_progress(progress: Callable[[PreviewStage], None] | None, stage: PreviewStage) -> None:
    if progress is None:
        return
    try:
        progress(stage)
    except Exception:
        # An observer must not interrupt collection or expose its own exception.
        pass


def _text(value: str) -> str:
    """Calendar content is text, never Markdown/HTML instructions or links."""
    value = escape(" ".join(value.split()), quote=False)
    for symbol in ("\\", "`", "*", "_", "[", "]", "#", "|", "!"):
        value = value.replace(symbol, "\\" + symbol)
    return value


def _moment(value: str) -> str:
    if "T" in value:
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    return date.fromisoformat(value).strftime("%d.%m.%Y")


def _interval(start: str, end: str | None, *, all_day: bool) -> str:
    if not end or end == start:
        return _moment(start) + (" · весь день" if all_day else "")
    if all_day:
        last = date.fromisoformat(end) - timedelta(days=1)
        if last <= date.fromisoformat(start):
            return _moment(start) + " · весь день"
        return f"{_moment(start)} — {last:%d.%m.%Y} · весь день"
    return f"{_moment(start)} — {_moment(end)}"


def render_markdown(events: list[DigestEvent], day: date, *, first_snapshot: bool) -> str:
    lines = [f"# События на {day:%d.%m.%Y}", "", "Время — московское (Europe/Moscow).", ""]
    if not events:
        lines += ["На этот день событий нет.", ""]
    statuses = {"CONFIRMED": "подтверждено", "TENTATIVE": "предварительное", "CANCELLED": "отменено"}
    responses = {
        "ACCEPTED": "принято", "TENTATIVE": "предварительно принято",
        "DECLINED": "отклонено", "NEEDS-ACTION": "ожидает ответа",
        "DELEGATED": "делегировано",
    }
    for all_day, heading in ((True, "На весь день"), (False, "По времени")):
        group = [event for event in events if event.all_day == all_day]
        if not group:
            continue
        lines += [f"## {heading}", ""]
        for event in group:
            lines += [f"### {_text(event.title or 'Без названия')}", ""]
            lines += [_interval(event.start, event.end, all_day=event.all_day), ""]
            lines += [f"Календарь: {_text(event.source_id)} / {_text(event.calendar_name)}.", ""]
            if event.original_start:
                original = _interval(event.original_start, event.original_end,
                                     all_day="T" not in event.original_start)
                actual = _interval(event.start, event.end, all_day=event.all_day)
                lines += [f"**Перенесено: {original} → {actual}.**", ""]
            labels = []
            if event.event_status and event.event_status != "UNSPECIFIED":
                labels.append("Статус в календаре: " + statuses.get(event.event_status, _text(event.event_status)))
            if event.participation != "UNKNOWN":
                labels.append("Ваш ответ: " + responses.get(event.participation, _text(event.participation)))
            if event.recurring:
                labels.append("повторяющееся")
            if event.multi_day:
                labels.append("многодневное")
            if labels:
                lines += ["; ".join(labels) + ".", ""]
            if event.location:
                lines += ["Место: " + _text(event.location), ""]
            if event.timezone_assumed:
                lines += ["В исходном времени не указан часовой пояс; использовано московское время.", ""]
    if first_snapshot:
        lines += ["Первый снимок: прежнее время одиночных событий до начала наблюдения неизвестно.", ""]
    return "\n".join(lines)


def create_preview(
    day: date, directory: Path = Path(".local"), *,
    progress: Callable[[PreviewStage], None] | None = None,
) -> dict:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(directory / ".preview.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PreviewError("Сбор календарей уже выполняется; дождитесь его завершения.") from None
        _report_progress(progress, PreviewStage.READ_STATE)
        old = read_snapshot(directory / "calendar-state.json")
        _report_progress(progress, PreviewStage.COLLECT)
        collection = collect_all()
        _report_progress(progress, PreviewStage.COMPARE)
        snapshot = advance_snapshot(collection.resources, old)
        first_snapshot = not old.current
        _report_progress(progress, PreviewStage.SELECT)
        events = select_day(collection.resources, day, previous_resources=snapshot.previous)
        events = merge_cancellations(
            events, collection.resources, day, Path(os.environ.get("DIGEST_STATE_DIR", directory)),
        )
        _report_progress(progress, PreviewStage.RENDER)
        markdown = render_markdown(events, day, first_snapshot=first_snapshot)
        email_html = render_email_html(events, day, first_snapshot=first_snapshot)
        interactive_html = render_email_html(events, day, interactive=True, first_snapshot=first_snapshot)
        payload = {
            "date": day.isoformat(), "timezone": "Europe/Moscow", "first_snapshot": first_snapshot,
            "events": [asdict(event) for event in events],
        }
        _report_progress(progress, PreviewStage.SAVE)
        markdown_path = directory / f"preview-{day.isoformat()}.md"
        json_path = directory / f"preview-{day.isoformat()}.json"
        payload_text = json.dumps(payload, ensure_ascii=False, indent=2)
        # Fetch/selection must succeed for every source before any output changes.
        write_private(markdown_path, markdown)
        write_private(json_path, payload_text)
        write_private(directory / f"email-{day.isoformat()}.html", email_html)
        write_private(directory / f"preview-{day.isoformat()}.html", interactive_html)
        # Commit the baseline last, so failed collection/rendering cannot consume a change.
        write_snapshot(directory / "calendar-state.json", snapshot)
        _report_progress(progress, PreviewStage.DONE)
        return {
            "date": day.isoformat(), "timezone": "Europe/Moscow",
            "calendars": collection.calendar_count, "resources": len(collection.resources),
            "events": len(events), "all_day": sum(event.all_day for event in events),
            "moved": sum(bool(event.original_start) for event in events),
            "event_statuses": dict(Counter(event.event_status or "UNSPECIFIED" for event in events)),
            "participation": dict(Counter(event.participation for event in events)),
            "first_snapshot": first_snapshot,
            "preview_markdown": str(markdown_path), "preview_json": str(json_path),
            "email_html": str(directory / f"email-{day.isoformat()}.html"),
            "preview_html": str(directory / f"preview-{day.isoformat()}.html"),
        }


def render_saved_preview(day: date, directory: Path = Path(".local")) -> dict:
    """Restyle an existing selection without fetching or changing its baseline."""
    try:
        payload = json.loads((directory / f"preview-{day.isoformat()}.json").read_text(encoding="utf-8"))
        if payload["date"] != day.isoformat() or payload["timezone"] != "Europe/Moscow":
            raise ValueError("date or timezone mismatch")
        events = [DigestEvent(**item) for item in payload["events"]]
        first_snapshot = bool(payload.get("first_snapshot", False))
        static = render_email_html(events, day, first_snapshot=first_snapshot)
        interactive = render_email_html(events, day, interactive=True, first_snapshot=first_snapshot)
    except Exception:
        raise PreviewError("Не удалось прочитать сохранённый образец за выбранную дату.") from None
    email_path = directory / f"email-{day.isoformat()}.html"
    preview_path = directory / f"preview-{day.isoformat()}.html"
    write_private(email_path, static)
    write_private(preview_path, interactive)
    return {"date": day.isoformat(), "events": len(events), "email_html": str(email_path),
            "preview_html": str(preview_path), "calendar_changes": False}
