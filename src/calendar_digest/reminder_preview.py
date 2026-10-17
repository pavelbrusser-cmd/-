"""Private, read-only preview of the proposed phone notification schedule.

This workflow collects a fresh inventory without reading or advancing the email
digest baseline. It neither creates action tokens nor sends notifications.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from enum import Enum
from html import escape
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar_collection import collect_all
from .calendar_operation_lock import calendar_operation_lock
from .contact_birthdays import contact_birthdays_enabled, plan_contact_birthdays
from .contact_birthdays_source import BirthdayCollection, collect_contact_birthdays
from .local_state import write_private
from .reminders import Reminder, combine_reminder_plans, plan_reminders
from .push_timing import reminder_was_deferred


MOSCOW = ZoneInfo("Europe/Moscow")
MAX_PREVIEW_DAYS = 31
CATEGORY_LABELS = {
    "birthdays": "Дни рождения",
    "control": "Поручения",
    "periodic_control": "Периодические поручения",
}
RULE_LABELS = {
    "d3": "За 3 дня", "d1": "За 1 день", "d0": "В день события",
    "h3": "За 3 часа", "h1": "За 1 час",
}
SOURCE_LABELS = {
    "icloud_contacts": "Контакты iCloud",
    "icloud": "iCloud", "mailru_1": "Mail.ru · аккаунт 1",
    "mailru_2": "Mail.ru · аккаунт 2", "mailru_3": "Mail.ru · аккаунт 3",
}
WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье",
)


class ReminderPreviewError(Exception):
    """Safe preview diagnostics without calendar contents or private paths."""


class ReminderPreviewStage(Enum):
    CONTACTS = "Читаем дни рождения из iCloud Contacts."
    COLLECT = "Читаем события календарей. Это может занять несколько минут."
    PLAN = "Рассчитываем время уведомлений по согласованным правилам."
    RENDER = "Формируем предпросмотр уведомлений."
    SAVE = "Сохраняем предпросмотр уведомлений."
    DONE = "Предпросмотр сохранён. Уведомления не отправлялись."


def _report(progress, stage):
    if progress is not None:
        try:
            progress(stage)
        except Exception:
            # Progress observers must not interrupt work or leak their errors.
            pass


def _text(value: str) -> str:
    return escape(" ".join(str(value).split()), quote=True)


def _event_moment(reminder: Reminder) -> str:
    if reminder.all_day:
        return date.fromisoformat(reminder.event_start).strftime("%d.%m.%Y") + " · весь день"
    instant = datetime.fromisoformat(reminder.event_start).astimezone(MOSCOW)
    return instant.strftime("%d.%m.%Y в %H:%M")


def render_reminder_preview(
    reminders: list[Reminder], start: date, *, days: int, generated_at: datetime,
) -> str:
    """Render plain calendar text as escaped, self-contained Russian HTML."""
    by_day = defaultdict(list)
    counts = dict.fromkeys(CATEGORY_LABELS, 0)
    for reminder in sorted(reminders, key=lambda item: (item.notify_at, item.title, item.stable_id)):
        moment = datetime.fromisoformat(reminder.notify_at).astimezone(MOSCOW)
        by_day[moment.date()].append((moment, reminder))
        counts[reminder.category] += 1

    sections = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        items = by_day[day]
        cards = []
        for moment, reminder in items:
            category = CATEGORY_LABELS[reminder.category]
            reason = RULE_LABELS[reminder.rule]
            if reminder_was_deferred(reminder.event_start, reminder.rule, reminder.notify_at):
                reason = "Перенесено на 07:00 МСК"
            source = SOURCE_LABELS.get(reminder.source_id, reminder.source_id)
            cards.append(
                '<article class="reminder">'
                f'<div class="time">{moment:%H:%M}<span>МСК</span></div>'
                '<div class="details">'
                f'<div class="category">{_text(category)} <span>· {_text(reason)}</span></div>'
                f'<h3>{_text(reminder.title or "Без названия")}</h3>'
                f'<p class="event-time">Событие: {_text(_event_moment(reminder))}</p>'
                f'<p class="calendar">{_text(source)} / {_text(reminder.calendar_name)}</p>'
                '</div></article>'
            )
        body = "".join(cards) if cards else '<p class="empty">На этот день уведомлений нет.</p>'
        sections.append(
            '<section class="day">'
            f'<h2>{day:%d.%m.%Y}<span>{WEEKDAYS[day.weekday()]}</span></h2>'
            + body + '</section>'
        )

    last = start + timedelta(days=days - 1)
    statistics = "".join(
        f'<div><strong>{counts[key]}</strong><span>{label}</span></div>'
        for key, label in CATEGORY_LABELS.items()
    )
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Предпросмотр уведомлений · {start:%d.%m.%Y}</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f2f5fa;color:#223047;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;font-size:16px;line-height:1.55}}
main{{max-width:860px;margin:36px auto;padding:0 20px}}
header{{padding:30px;border-radius:20px;background:#fff;border-top:5px solid #3265a8;box-shadow:0 4px 18px #213b6510}}
.eyebrow{{margin:0;color:#3265a8;font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}}
h1,h2,h3{{color:#24548f;line-height:1.3}}
h1{{font-size:30px;margin:10px 0}}
.lead{{font-size:19px;margin:10px 0}}
.note{{color:#55647a;margin:12px 0 0}}
.notice{{padding:14px 18px;background:#eaf2ff;border-radius:12px;margin:22px 0;color:#264f84}}
.statistics{{display:flex;gap:12px;margin:22px 0}}
.statistics div{{flex:1;background:#fff;border-radius:14px;padding:17px}}
.statistics strong{{display:block;color:#24548f;font-size:28px}}
.statistics span{{font-size:14px;color:#55647a}}
.day{{margin:25px 0}}
h2{{font-size:21px;margin:0 0 12px}}
h2 span{{font-weight:400;font-size:15px;margin-left:12px;color:#68758a}}
.reminder{{display:flex;gap:20px;padding:22px;background:#fff;border:1px solid #e0e7f1;border-radius:14px;margin:10px 0;break-inside:avoid}}
.time{{color:#24548f;min-width:62px;font-size:23px;font-weight:700;line-height:1.3}}
.time span{{display:block;font-size:11px;letter-spacing:.08em;color:#718097;margin-top:5px}}
.details{{min-width:0;overflow-wrap:anywhere}}
.category{{font-size:12px;color:#3265a8;font-weight:700}}
.category span{{color:#64728a;font-weight:400}}
h3{{font-size:19px;margin:6px 0 10px}}
.event-time{{margin:0;font-size:15px}}
.calendar{{margin:5px 0 0;font-size:13px;color:#718097}}
.empty{{color:#718097;padding:15px 20px;border:1px dashed #c9d5e6;border-radius:12px}}
footer{{padding:10px 0 30px;color:#718097;font-size:13px}}
@media(max-width:560px){{main{{margin:18px auto;padding:0 12px}}header{{padding:22px}}h1{{font-size:25px}}.statistics{{gap:6px}}.statistics div{{padding:12px 9px}}.statistics span{{font-size:12px}}.reminder{{padding:17px 14px;gap:12px}}h2 span{{display:block;margin:4px 0 0}}}}
@media print{{body{{background:#fff}}main{{max-width:none;margin:0}}header{{box-shadow:none}}}}
</style></head><body><main>
<header><p class="eyebrow">Уведомления на телефон · предпросмотр</p>
<h1>Ваше расписание напоминаний</h1>
<p class="lead">{start:%d.%m.%Y} — {last:%d.%m.%Y}</p>
<p class="note">Все даты и время — московские (Europe/Moscow). Ниже указано время уведомления; дата и время самого события приведены отдельно.</p>
<p class="note">Уведомления с 00:00 до 06:59 переносятся на 07:00 того же дня, даже если событие начинается раньше.</p></header>
<p class="notice"><strong>Это предпросмотр.</strong> Уведомления не отправлялись. Расписание рассчитано по свежим данным календарей; изменения событий могут изменить его.</p>
<div class="statistics">{statistics}</div>
{''.join(sections)}
<footer>Сформировано: {generated_at.astimezone(MOSCOW):%d.%m.%Y в %H:%M} МСК. Числа сверху показывают количество уведомлений, а не событий. Этот файл содержит личные данные календарей.</footer>
</main></body></html>'''


def _window(start, days, control_source):
    if type(start) is not date:
        raise ReminderPreviewError("Укажите начальную дату предпросмотра в формате ГГГГ-ММ-ДД.")
    if type(days) is not int or not 1 <= days <= MAX_PREVIEW_DAYS:
        raise ReminderPreviewError("Предпросмотр доступен на срок от 1 до 31 дня.")
    if control_source not in {"mailru_1", "mailru_2", "mailru_3"}:
        raise ReminderPreviewError("Для поручений выберите один из подключённых аккаунтов Mail.ru.")
    try:
        end = start + timedelta(days=days)
        # The planner must inspect future events with an advance notice of three
        # days; reject impossible date arithmetic before any account is read.
        end + timedelta(days=4)
    except OverflowError:
        raise ReminderPreviewError("Период предпросмотра выходит за допустимый диапазон дат.") from None
    return datetime.combine(start, time.min, MOSCOW), datetime.combine(end, time.min, MOSCOW)


def create_reminder_preview(
    start: date, directory: Path = Path(".local"), *, days: int = 7,
    control_source: str = "mailru_2",
    progress: Callable[[ReminderPreviewStage], None] | None = None,
) -> dict:
    """Collect once, calculate a notification window, save private preview files."""
    window_start, window_end = _window(start, days, control_source)
    use_contacts = contact_birthdays_enabled()
    directory = Path(directory)
    state_directory = Path(os.environ.get("DIGEST_STATE_DIR") or directory)
    with calendar_operation_lock(state_directory):
        try:
            if directory.is_symlink():
                raise OSError("symlink output directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise ReminderPreviewError("Не удалось открыть защищённый каталог предпросмотра.") from None
        birthdays = BirthdayCollection([], 0)
        if use_contacts:
            _report(progress, ReminderPreviewStage.CONTACTS)
            birthdays = collect_contact_birthdays()
        _report(progress, ReminderPreviewStage.COLLECT)
        collection = collect_all()
        _report(progress, ReminderPreviewStage.PLAN)
        plan = plan_reminders(
            collection.resources, window_start=window_start, window_end=window_end,
            control_source=control_source,
        )
        if use_contacts:
            plan = combine_reminder_plans(plan, plan_contact_birthdays(
                birthdays.contacts, window_start=window_start, window_end=window_end,
            ))
        generated_at = datetime.now(MOSCOW)
        _report(progress, ReminderPreviewStage.RENDER)
        html = render_reminder_preview(plan.reminders, start, days=days, generated_at=generated_at)
        metadata = {
            "start_date": start.isoformat(), "end_date_exclusive": window_end.date().isoformat(),
            "days": days, "timezone": "Europe/Moscow", "generated_at": generated_at.isoformat(),
            "calendars": collection.calendar_count, "resources": len(collection.resources),
            "contacts_enabled": use_contacts, "birthday_contacts": len(birthdays.contacts),
            "contact_addressbooks": birthdays.addressbooks,
            "sent": False,
        }
        payload = {
            "schema_version": 1, **metadata, "summary": plan.summary,
            "reminders": [dict(asdict(item), stable_id=item.stable_id) for item in plan.reminders],
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        json_path = directory / f"reminder-preview-{start.isoformat()}.json"
        html_path = directory / f"reminder-preview-{start.isoformat()}.html"
        _report(progress, ReminderPreviewStage.SAVE)
        try:
            write_private(json_path, encoded)
            write_private(html_path, html)
        except OSError:
            raise ReminderPreviewError("Не удалось сохранить защищённые файлы предпросмотра.") from None
        _report(progress, ReminderPreviewStage.DONE)
        return {
            **metadata, **plan.summary, "preview_json": str(json_path), "preview_html": str(html_path),
        }
