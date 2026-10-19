"""Private owner preview of executor reminders; never queue or send email."""

from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from enum import Enum
from html import escape
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar_operation_lock import calendar_operation_lock
from .calendar_sync import sync_calendars, to_calendar_resources
from .local_state import write_private
from .push_timing import reminder_was_deferred


MOSCOW = ZoneInfo("Europe/Moscow")
MAX_PREVIEW_DAYS = 31
EMAIL_SUBJECT = "ВНИМАНИЕ! Контроль и мониторинг поручений!"
RULE_LABELS = {
    "d3": "За 3 дня", "d1": "За 1 день", "h1": "За 1 час", "d0": "В день события",
    "w1": "За 1 неделю", "w2": "За 2 недели", "m1": "За 1 месяц",
    "m2": "За 2 месяца", "m3": "За 3 месяца", "m6": "За 6 месяцев",
    "y1": "За 1 год", "y2": "За 2 года",
}
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
SUMMARY_FIELDS = (
    "series_total", "series_without_external_recipients", "candidate_occurrences", "cancelled_occurrences", "cancelled_series",
    "without_external_recipients", "timezone_assumed_occurrences",
    "reminders_total", "recipients_total",
)


class ControlEmailPreviewError(Exception):
    """Fixed diagnostics without addresses, event contents or private paths."""


class ControlEmailPreviewStage(Enum):
    COLLECT = "Читаем календарь контрольных поручений. Это может занять несколько минут."
    PLAN = "Рассчитываем письма исполнителям и проверяем получателей."
    RENDER = "Формируем приватный предпросмотр писем исполнителям."
    SAVE = "Сохраняем предпросмотр."
    DONE = "Предпросмотр сохранён. Письма не отправлялись, расписание не включалось."


def _now():
    return datetime.now(MOSCOW)


def _report(progress, stage):
    if progress is not None:
        try:
            progress(stage)
        except Exception:
            pass


def _accounts():
    from .control_email_automation import control_email_accounts

    return control_email_accounts()


def _plan(resources, **kwargs):
    from .control_email_rules import plan_control_emails

    return plan_control_emails(resources, **kwargs)


def _text(value):
    return escape(" ".join(str(value).split()), quote=True)


def _moment(value, *, all_day=False):
    if all_day:
        return date.fromisoformat(value).strftime("%d.%m.%Y") + " · весь день"
    return datetime.fromisoformat(value).astimezone(MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")


def _window(start, days, now):
    if type(start) is not date or type(days) is not int or not 1 <= days <= MAX_PREVIEW_DAYS:
        raise ControlEmailPreviewError("Укажите дату начала и период предпросмотра от 1 до 31 дня.")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ControlEmailPreviewError("Для предпросмотра требуется текущее время с часовым поясом.")
    try:
        beginning = datetime.combine(start, time.min, MOSCOW)
        end = datetime.combine(start + timedelta(days=days), time.min, MOSCOW)
    except (ValueError, OverflowError):
        raise ControlEmailPreviewError("Период предпросмотра выходит за допустимый диапазон дат.") from None
    beginning = max(beginning, now.astimezone(MOSCOW))
    if beginning >= end:
        raise ControlEmailPreviewError("Период предпросмотра уже закончился; укажите текущие или будущие даты.")
    return beginning, end


def _summary(summary):
    """Only known numeric planner fields can reach the console."""
    result = {}
    for field in SUMMARY_FIELDS:
        value = summary.get(field, 0)
        if type(value) is not int or value < 0:
            raise ControlEmailPreviewError("Не удалось проверить счётчики предпросмотра.")
        result[field] = value
    rules = summary.get("by_rule", {})
    if not isinstance(rules, dict) or any(
        key not in RULE_LABELS or type(value) is not int or value < 0 for key, value in rules.items()
    ):
        raise ControlEmailPreviewError("Не удалось проверить правила предпросмотра.")
    result["by_rule"] = rules
    return result


def render_control_email_preview(reminders, start: date, *, days: int, generated_at: datetime) -> str:
    by_day = defaultdict(list)
    for reminder in sorted(reminders, key=lambda item: (item.notify_at, item.title, item.recipient, item.stable_id)):
        moment = datetime.fromisoformat(reminder.notify_at).astimezone(MOSCOW)
        by_day[moment.date()].append((moment, reminder))
    sections = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        cards = []
        for moment, reminder in by_day[day]:
            reason = RULE_LABELS[reminder.rule]
            if reminder_was_deferred(reminder.event_start, reminder.rule, reminder.notify_at):
                reason = "Перенесено на 07:00 МСК"
            end = ""
            if reminder.event_end and not reminder.all_day:
                end = f'<span class="end">Окончание: {_text(_moment(reminder.event_end))}</span>'
            location = f'<p>Место: {_text(reminder.location)}</p>' if reminder.location else ""
            cards.append(
                '<article class="card">'
                f'<div class="time">{moment:%H:%M}<span>МСК</span></div>'
                '<div class="details">'
                f'<div class="rule">{_text(reason)}</div>'
                f'<h3>{_text(reminder.title or "Без названия")}</h3>'
                f'<p class="recipient"><span>Получатель</span>{_text(reminder.recipient)}</p>'
                f'<p class="event">Событие: {_text(_moment(reminder.event_start, all_day=reminder.all_day))}{end}</p>'
                f'<p class="muted">Календарь: {_text(reminder.calendar_name)}</p>'
                f'{location}</div></article>'
            )
        content = "".join(cards) or '<p class="empty">На этот день писем нет.</p>'
        sections.append(f'<section><h2>{day:%d.%m.%Y}<span>{WEEKDAYS[day.weekday()]}</span></h2>{content}</section>')
    recipients = len({item.recipient for item in reminders})
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Предпросмотр писем исполнителям</title><style>
*{{box-sizing:border-box}} body{{margin:0;background:#f3f6fb;color:#203047;font:16px/1.6 Arial,sans-serif}}
main{{max-width:880px;margin:32px auto;padding:0 20px 40px}} header{{padding:32px;border-radius:20px;background:#e8f0ff}}
.label,.rule{{font-weight:700;color:#315db3;font-size:13px}} h1{{margin:10px 0;color:#244b91;font-size:30px;line-height:1.25}}
h2{{font-size:21px;color:#244b91;margin:30px 0 12px}} h2 span{{font-size:14px;font-weight:400;margin-left:12px;color:#607089}}
h3{{margin:4px 0 12px;font-size:19px;color:#244b91}} p{{margin:8px 0}} .notice{{margin-top:18px;padding:14px 18px;background:white;border-radius:12px}}
.stats{{display:flex;gap:24px;flex-wrap:wrap;margin-top:20px}} .stats strong{{font-size:24px;color:#244b91}}
.card{{display:flex;gap:20px;margin:12px 0;padding:22px;background:white;border:1px solid #e1e8f2;border-radius:16px}}
.time{{min-width:64px;font-size:24px;font-weight:700;color:#315db3;line-height:1.3}} .time span{{display:block;font-size:11px;font-weight:400;color:#607089;margin-top:6px}}
.details{{min-width:0;overflow-wrap:anywhere;flex:1}} .recipient{{padding:10px 14px;background:#edf4fd;border-radius:10px;font-weight:700}}
.recipient span{{display:block;font-size:11px;font-weight:400;color:#607089;text-transform:uppercase;letter-spacing:.08em}}
.end{{display:block;font-size:13px;color:#607089}} .muted,.empty,footer{{color:#607089;font-size:14px}} .empty{{padding:18px;background:white;border-radius:12px}}
footer{{margin-top:30px;border-top:1px solid #dbe4ef;padding-top:18px}} @media(max-width:540px){{header{{padding:22px}}h1{{font-size:25px}}.card{{padding:16px;gap:12px}}.time{{font-size:21px;min-width:55px}}}}
</style></head><body><main><header><div class="label">КОНТРОЛЬ ПОРУЧЕНИЙ</div><h1>Письма исполнителям</h1>
<p>Предстоящие отправки с {start:%d.%m.%Y} по {start + timedelta(days=days - 1):%d.%m.%Y}. Всё время — московское.</p>
<p><span style="color:#c52c37;font-weight:700">!</span> Высокая важность · Тема письма: <strong>{EMAIL_SUBJECT}</strong></p>
<div class="stats"><div><strong>{len(reminders)}</strong> писем</div><div><strong>{recipients}</strong> получателей</div></div>
<div class="notice">Это предпросмотр для владельца. Письма не отправлялись, расписание не включалось.
Каждая карточка показывает отдельное письмо одному получателю.</div></header>{''.join(sections)}
<footer>Сформировано {generated_at.astimezone(MOSCOW):%d.%m.%Y в %H:%M} МСК. Прошедшие отправки не воспроизводятся.
Адреса и содержание предназначены только для вашей локальной проверки.</footer></main></body></html>'''


def create_control_email_preview(start: date, directory: Path = Path(".local"), *, days=7,
                                 progress=None, clock=_now) -> dict:
    """Collect only the control account; save review files without queue changes."""
    generated_at = clock()
    window_start, window_end = _window(start, days, generated_at)
    account, own_accounts = _accounts()
    directory = Path(directory)
    gate = Path(os.environ.get("DIGEST_STATE_DIR") or directory)
    with calendar_operation_lock(gate):
        try:
            if directory.is_symlink():
                raise OSError("symlink output directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise ControlEmailPreviewError("Не удалось открыть защищённый каталог предпросмотра.") from None
        _report(progress, ControlEmailPreviewStage.COLLECT)
        collection = sync_calendars([], source_ids=("mailru_2",))
        generated_at = clock()
        window_start, window_end = _window(start, days, generated_at)
        _report(progress, ControlEmailPreviewStage.PLAN)
        resources = to_calendar_resources(collection.resources, account_emails={"mailru_2": account})
        plan = _plan(resources, window_start=window_start, window_end=window_end, own_accounts=own_accounts)
        counts = _summary(plan.summary)
        _report(progress, ControlEmailPreviewStage.RENDER)
        html = render_control_email_preview(plan.reminders, start, days=days, generated_at=generated_at)
        metadata = {
            "start_date": start.isoformat(), "end_date_exclusive": window_end.date().isoformat(),
            "window_start": window_start.isoformat(), "days": days, "timezone": "Europe/Moscow",
            "generated_at": generated_at.astimezone(MOSCOW).isoformat(), "calendars": collection.calendar_count,
            "resources": len(collection.resources), "sent": False, "activated": False,
        }
        payload = {"schema_version": 1, **metadata, "subject": EMAIL_SUBJECT, "importance": "high", "summary": counts,
                   "reminders": [dict(asdict(item), stable_id=item.stable_id) for item in plan.reminders]}
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        json_path = directory / f"control-email-preview-{start.isoformat()}.json"
        html_path = directory / f"control-email-preview-{start.isoformat()}.html"
        _report(progress, ControlEmailPreviewStage.SAVE)
        try:
            write_private(json_path, encoded)
            write_private(html_path, html)
        except OSError:
            raise ControlEmailPreviewError("Не удалось сохранить защищённые файлы предпросмотра.") from None
        _report(progress, ControlEmailPreviewStage.DONE)
        return {**metadata, **counts, "preview_json": str(json_path), "preview_html": str(html_path)}
