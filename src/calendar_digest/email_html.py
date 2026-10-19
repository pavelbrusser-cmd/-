"""An email-safe layout and a separate, entirely local interaction prototype."""

from datetime import date, datetime, timedelta
from html import escape

from .presentation import CATEGORY_LABELS, actions_for, classify_event
from .selection import DigestEvent, MOSCOW, SeriesChangeNotice


INK = "#244f59"
MONTHS = ("", "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря")
WEEKDAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
SOURCE_NAMES = {"icloud": "iCloud", "mailru_1": "Mail.ru · 1", "mailru_2": "Mail.ru · 2", "mailru_3": "Mail.ru · 3", "google": "Google Календарь"}
ACTION_LABELS = {"confirm": "Подтвердить", "reschedule": "Перенести", "cancel": "Отменить"}
STATUS_LABELS = {"CONFIRMED": "Подтверждено", "TENTATIVE": "Предварительно", "CANCELLED": "Отменено"}
PARTICIPATION_LABELS = {"ACCEPTED": "принято", "TENTATIVE": "предварительно принято",
                        "DECLINED": "отклонено", "NEEDS-ACTION": "ожидает ответа", "DELEGATED": "делегировано"}
SECTIONS = {
    "birthdays": ("Дни рождения", "Важные даты", "#744064", "#fbf1f6", "#b86191"),
    "holidays": ("Праздники и памятные даты", "Поводы для поздравлений", "#795419", "#fff6df", "#c9942b"),
    "control": ("Контроль поручений", "Сроки и обязательства", "#914834", "#fff1e9", "#c66b43"),
    "timeline": ("Встречи и события", "Планы по времени", "#315e87", "#edf4fb", "#4d88b3"),
    "all_day": ("На весь день", "События без точного времени", "#316b66", "#eaf5f1", "#4a9689"),
    "changes": ("Изменения в расписании", "Переносы и отмены", "#76519a", "#f4f0fa", "#a483c3"),
}


def _text(value) -> str:
    return escape(str(value), quote=True)


def _date(value: date, *, include_year=False) -> str:
    return f"{value.day} {MONTHS[value.month]}" + (f" {value.year}" if include_year else "")


def _moment(value: str, *, include_year=False) -> str:
    if "T" in value:
        moment = datetime.fromisoformat(value)
        return f"{_date(moment.date(), include_year=include_year)}, {moment:%H:%M}"
    return _date(date.fromisoformat(value), include_year=include_year)


def _range(start: str, end: str | None, *, include_year=False) -> str:
    if not end or start == end:
        return _moment(start, include_year=include_year)
    if "T" not in start:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) - timedelta(days=1)
        return _date(first, include_year=include_year) if last <= first else f"{_date(first, include_year=include_year)} — {_date(last, include_year=include_year)}"
    begin, finish = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if begin.date() == finish.date():
        return f"{_date(begin.date(), include_year=include_year)}, {begin:%H:%M}–{finish:%H:%M}"
    return f"{_moment(start, include_year=include_year)} — {_moment(end, include_year=include_year)}"


def _badge(label: str, background="#eef4f3", color=INK) -> str:
    return (f'<span style="display:inline-block;margin:0 6px 6px 0;padding:4px 9px;'
            f'border-radius:5px;background:{background};color:{color};font-size:12px;'
            f'line-height:18px;font-weight:600;">{_text(label)}</span>')


def _section_for(event: DigestEvent) -> str:
    # A move/cancellation belongs to one change card, not a second copy of the
    # same event in its former category. The original action index stays intact.
    if event.event_status == "CANCELLED" or event.original_start:
        return "changes"
    category = classify_event(event)
    if category in {"birthdays", "holidays"}:
        return category
    if event.source_id == "mailru_2":
        return "control"
    return "all_day" if event.all_day else "timeline"


def _chronological(item):
    index, event = item
    moment = datetime.fromisoformat(event.start)
    if moment.tzinfo is not None:
        moment = moment.astimezone(MOSCOW)
    return moment.replace(tzinfo=None), index


def digest_sections(events):
    """Ordered semantic groups of (original index, event), each event once.

    Keeping original indices allows HTML and plain-text parts to share this
    ordering without changing which signed action link belongs to an event.
    """
    groups = {key: [] for key in SECTIONS}
    for index, event in enumerate(events):
        groups[_section_for(event)].append((index, event))
    return [(key, sorted(group, key=_chronological)) for key, group in groups.items() if group]


def _account_email(record) -> str:
    value = getattr(record, "account_email", "")
    return value.strip() if isinstance(value, str) else ""


def _emails(records):
    emails = {}
    for record in records:
        value = _account_email(record)
        if value:
            emails.setdefault(value.casefold(), value)
    return [emails[key] for key in sorted(emails)]


def _source(record, *, show_email=False):
    source = f"{SOURCE_NAMES.get(record.source_id, record.source_id)} · {record.calendar_name}"
    html = f'<div style="font-size:12px;line-height:19px;color:#62747a;overflow-wrap:anywhere;word-break:break-word;">{_text(source)}</div>'
    if show_email and _account_email(record):
        html += (f'<div class="card-source-email" style="margin-top:2px;font-size:12px;line-height:19px;'
                 f'color:#62747a;overflow-wrap:anywhere;word-break:break-word;">{_text(_account_email(record))}</div>')
    return html


def _section_header(key, count, records, number):
    heading, subtitle, color, background, accent = SECTIONS[key]
    addresses = _emails(records)
    sources = ""
    if addresses:
        label = "Адрес источника" if len(addresses) == 1 else "Адреса источников"
        sources = (f'<div class="section-source-emails" style="margin-top:7px;font-size:12px;line-height:19px;'
                   f'color:{color};overflow-wrap:anywhere;word-break:break-word;">'
                   f'{label}: {_text(" · ".join(addresses))}</div>')
    return f'''<table role="presentation" class="section-banner" width="100%" cellpadding="0" cellspacing="0"
style="width:100%;margin:28px 0 13px;background:{background};border-left:4px solid {accent};border-radius:0 9px 9px 0;">
<tr><td style="padding:17px 18px;">
<div style="font-size:11px;line-height:17px;letter-spacing:1px;color:{color};">{number:02d} &nbsp;·&nbsp; {_text(subtitle)}</div>
<h2 class="section-heading" style="margin:4px 0 0;font-size:21px;line-height:28px;font-weight:700;color:{color};">{heading}</h2>{sources}
</td><td width="48" valign="top" align="right" style="width:48px;padding:20px 17px 16px 0;font-size:22px;line-height:28px;font-weight:700;color:{color};">{count}</td></tr></table>'''


def _actions(event: DigestEvent, index: int, action_links=None) -> str:
    buttons = []
    for action in actions_for(event):
        if action_links is not None and (index, action) not in action_links:
            continue
        if action_links is None:
            attributes = (f'href="#actions-demo" data-action="{action}" '
                          f'data-event="{index}" data-title="{_text(event.title)}" '
                          f'data-start="{_text(event.start)}" data-end="{_text(event.end)}" '
                          f'data-recurring="{str(event.recurring).lower()}" ')
        else:
            attributes = f'href="{_text(action_links[index, action])}" rel="noreferrer" '
        color, background, border = ("#ffffff", INK, INK) if action != "cancel" else ("#95473e", "#ffffff", "#d9bcb6")
        buttons.append(
            f'<a class="action" {attributes}'
            f'aria-label="{ACTION_LABELS[action]}: {_text(event.title)}" '
            f'style="display:inline-block;margin:0 8px 6px 0;padding:10px 14px;'
            f'border:1px solid {border};border-radius:6px;background:{background};'
            f'color:{color};font-size:13px;line-height:20px;font-weight:600;'
            f'text-decoration:none;">{ACTION_LABELS[action]}</a>'
        )
    if not buttons:
        return ""
    return '<div class="actions" style="padding-top:14px;">' + "".join(buttons) + "</div>"


def _card(event: DigestEvent, index: int, day: date, action_links=None, *, section="timeline", show_email=False) -> str:
    category = classify_event(event)
    _, _, section_color, section_background, accent = SECTIONS[section]
    cross_year = event.start[:4] != str(day.year) or event.end[:4] != str(day.year)
    if event.all_day:
        time_label, time_detail = "Весь день", _range(event.start, event.end, include_year=cross_year)
    else:
        begin, finish = datetime.fromisoformat(event.start), datetime.fromisoformat(event.end)
        time_label = begin.strftime("%H:%M")
        time_detail = "до " + finish.strftime("%H:%M") if begin.date() == finish.date() else _range(event.start, event.end, include_year=cross_year)
        if begin.date() != day:
            time_detail = _range(event.start, event.end, include_year=cross_year)
    category_label = "Контроль поручений" if event.source_id == "mailru_2" and category not in {"birthdays", "holidays"} else CATEGORY_LABELS[category]
    metadata = _badge(category_label, section_background, section_color)
    if event.recurring:
        metadata += _badge("Повтор", "#f3f4f5", "#64727a")
    if event.multi_day:
        metadata += _badge("Несколько дней", "#f3f4f5", "#64727a")
    status = ""
    if event.event_status in STATUS_LABELS:
        background, color = ("#fbeeea", "#95473e") if event.event_status == "CANCELLED" else (
            ("#fff4dd", "#865e1b") if event.event_status == "TENTATIVE" else ("#eaf3ef", "#3b6858")
        )
        status += _badge(STATUS_LABELS[event.event_status], background, color)
    if event.participation in PARTICIPATION_LABELS:
        status += (f'<span style="display:inline-block;font-size:12px;color:#64727a;line-height:20px;'
                   f'margin-bottom:6px;">Ваш ответ: {_text(PARTICIPATION_LABELS[event.participation])}</span>')
    moved = ""
    if event.original_start:
        moved = (
            '<div style="margin-top:13px;padding:11px 13px;background:#fff7e6;border-left:3px solid #c39b4d;'
            'border-radius:0 5px 5px 0;font-size:13px;line-height:21px;color:#6c572c;">'
            '<strong>Перенесено</strong><br>'
            f'Было: {_text(_range(event.original_start, event.original_end, include_year=True))}<br>'
            f'Стало: <strong>{_text(_range(event.start, event.end, include_year=True))}</strong></div>'
        )
    location = ""
    if event.location:
        location = (f'<div style="margin-top:8px;font-size:13px;line-height:21px;color:#586a72;'
                    f'overflow-wrap:anywhere;word-break:break-word;"><span style="color:{INK};">Место:</span> '
                    f'{_text(event.location)}</div>')
    assumption = ('<div style="margin-top:8px;font-size:11px;color:#865e1b;line-height:18px;">'
                  'Часовой пояс не указан; время показано по Москве.</div>') if event.timezone_assumed else ""
    border = "#e6d3ce" if event.event_status == "CANCELLED" else "#dfe8e5"
    return f'''<table role="presentation" class="event-card" width="100%" cellpadding="0" cellspacing="0"
style="width:100%;margin:0 0 12px;border:1px solid {border};border-radius:10px;background:#ffffff;page-break-inside:avoid;break-inside:avoid;">
<tr><td class="time-column" width="100" valign="top" style="width:100px;padding:21px 12px 21px 17px;border-left:3px solid {accent};border-right:1px solid #edf1ef;">
<div style="font-size:{'14' if event.all_day else '22'}px;line-height:28px;font-weight:700;color:{section_color};white-space:nowrap;">{_text(time_label)}</div>
<div style="margin-top:5px;font-size:12px;line-height:18px;color:#62747a;">{_text(time_detail)}</div>
</td><td class="event-content" valign="top" style="padding:18px 21px;">
<div>{metadata}</div>
<h3 style="margin:3px 0 9px;font-size:18px;line-height:26px;font-weight:600;color:{INK};overflow-wrap:anywhere;word-break:break-word;">{_text(event.title or 'Без названия')}</h3>
{_source(event, show_email=show_email)}
{location}
<div style="margin-top:11px;">{status}</div>
{moved}{assumption}{_actions(event, index, action_links)}
</td></tr></table>'''


def _series_side(notice: SeriesChangeNotice, side: str, heading: str) -> str:
    intervals = getattr(notice, f"{side}_intervals")
    values = [
        _range(interval.start, interval.end, include_year=True) + (" · весь день" if interval.all_day else "")
        for interval in intervals
    ]
    rendered = "<br>".join(_text(value) for value in values) if values else "На этот день событий нет."
    recurring = getattr(notice, f"{side}_recurring")
    anchor = getattr(notice, f"{side}_anchor_start")
    anchor_label = "Начало серии" if recurring else "Начало события"
    anchor_value = _moment(anchor, include_year=True) + (" · весь день" if "T" not in anchor else "")
    return (
        '<div style="margin-top:13px;padding:12px 14px;background:#f3f6f3;border-radius:6px;'
        'font-size:13px;line-height:21px;">'
        f'<strong style="color:{INK};">{heading}</strong><br>{rendered}'
        f'<div style="margin-top:7px;font-size:12px;line-height:19px;color:#62747a;">'
        f'{anchor_label}: {_text(anchor_value)}</div></div>'
    )


def _series_card(notice: SeriesChangeNotice, *, show_email=False) -> str:
    return (
        '<div class="series-change-card" style="margin:0 0 12px;padding:18px 21px;'
        'border:1px solid #e0d8e9;border-left:3px solid #a483c3;border-radius:10px;background:#ffffff;'
        'page-break-inside:avoid;break-inside:avoid;">'
        f'{_badge("Изменение расписания", "#fff4dd", "#865e1b")}'
        f'<h3 style="margin:3px 0 9px;font-size:18px;line-height:26px;font-weight:600;'
        f'color:{INK};overflow-wrap:anywhere;word-break:break-word;">{_text(notice.title or "Без названия")}</h3>'
        + _source(notice, show_email=show_email)
        + _series_side(notice, "old", "В сохранённом расписании")
        + _series_side(notice, "new", "Сейчас")
        + '</div>'
    )


DEMO = r'''
<dialog id="action-dialog" aria-labelledby="dialog-heading">
  <form id="action-form">
    <div class="dialog-kicker">ПРОВЕРКА ОБРАЗЦА</div>
    <h2 id="dialog-heading"></h2>
    <p id="dialog-title"></p>
    <p id="dialog-description"></p>
    <div id="reschedule-fields" hidden>
      <label>Новая дата <input type="date" id="new-date"></label>
      <div id="time-fields" class="time-fields">
        <label>Начало <input type="time" id="new-time"></label>
        <label>Окончание <input type="time" id="new-end-time"></label>
      </div>
      <label>Дата окончания <input type="date" id="new-end-date"></label>
      <p class="dialog-small" id="time-note">Время московское.</p>
    </div>
    <p id="recurrence-note" class="dialog-small" hidden>Действие относится только к этому повторению.</p>
    <p class="demo-note">Это демонстрация. Ваши календари останутся без изменений.</p>
    <p id="dialog-result" role="status" hidden></p>
    <div class="dialog-buttons"><button type="button" id="close-dialog">Закрыть</button><button type="submit" id="submit-action">Показать результат</button></div>
  </form>
</dialog>
<script>
(() => {
  const dialog = document.getElementById('action-dialog');
  const form = document.getElementById('action-form');
  const field = id => document.getElementById(id);
  const names = { confirm: 'Подтвердить участие', reschedule: 'Перенести событие', cancel: 'Отменить событие' };
  let action = '', allDay = false;
  document.querySelectorAll('[data-action]').forEach(button => {
    button.addEventListener('click', event => {
      event.preventDefault();
      action = button.dataset.action;
      allDay = !button.dataset.start.includes('T');
      field('dialog-heading').textContent = names[action];
      field('dialog-title').textContent = button.dataset.title;
      field('dialog-description').textContent = action === 'reschedule' ? 'Выберите новые дату и время.' : 'Посмотрите, как будет выглядеть подтверждение выбранного действия.';
      field('reschedule-fields').hidden = action !== 'reschedule';
      field('time-fields').hidden = allDay;
      field('time-note').textContent = allDay ? 'Дата окончания — последний день события включительно.' : 'Время московское.';
      field('new-date').value = button.dataset.start.slice(0, 10);
      field('new-time').value = button.dataset.start.slice(11, 16);
      let end = button.dataset.end;
      if (allDay) { const last = new Date(end + 'T12:00:00Z'); last.setUTCDate(last.getUTCDate() - 1); end = last.toISOString().slice(0, 10); }
      field('new-end-date').value = end.slice(0, 10);
      field('new-end-time').value = end.slice(11, 16);
      ['new-date', 'new-end-date'].forEach(id => { field(id).required = action === 'reschedule'; field(id).disabled = action !== 'reschedule'; });
      ['new-time', 'new-end-time'].forEach(id => { field(id).required = action === 'reschedule' && !allDay; field(id).disabled = action !== 'reschedule' || allDay; });
      field('recurrence-note').hidden = button.dataset.recurring !== 'true';
      field('dialog-result').hidden = true;
      field('submit-action').hidden = false;
      dialog.showModal();
    });
  });
  field('close-dialog').addEventListener('click', () => dialog.close());
  form.addEventListener('submit', event => {
    event.preventDefault();
    let message;
    if (action === 'reschedule') {
      const begin = field('new-date').value + (allDay ? '' : 'T' + field('new-time').value);
      const end = field('new-end-date').value + (allDay ? '' : 'T' + field('new-end-time').value);
      if (end < begin || (!allDay && end === begin)) {
        field('dialog-result').textContent = 'Укажите окончание после начала события.';
        field('dialog-result').hidden = false;
        return;
      }
      const pretty = value => { const [d,t] = value.split('T'); return d.split('-').reverse().join('.') + (t ? ' в ' + t : ''); };
      message = 'Образец результата: перенос на ' + pretty(begin) + ' — ' + pretty(end) + (allDay ? ' (весь день).' : ' (МСК).');
    } else {
      message = action === 'confirm' ? 'Образец результата: участие подтверждено.' : 'Образец результата: отмена подтверждена.';
    }
    field('dialog-result').textContent = message + ' Изменения не сохранены в календаре.';
    field('dialog-result').hidden = false;
    field('submit-action').hidden = true;
  });
})();
</script>'''


def render_email_html(events: list[DigestEvent], day: date, *, interactive=False, first_snapshot=False,
                      action_links=None, series_changes=()) -> str:
    events = list(events)
    series_changes = list(series_changes)
    if action_links is not None:
        interactive = False
    groups = dict(digest_sections(events))
    categories = " ".join(
        _badge(f"{SECTIONS[key][0]} · {count}", SECTIONS[key][3], SECTIONS[key][2])
        for key in SECTIONS
        if (count := len(groups.get(key, [])) + (len(series_changes) if key == "changes" else 0))
    )
    tiles = []
    for number, label, color in ((len(events), "всего событий", INK),
                                  (sum(e.all_day for e in events), "на весь день", "#316b66"),
                                  (sum(e.event_status == 'CANCELLED' for e in events), "отменено", "#914834"),
                                  (sum(bool(e.original_start) for e in events), "перенесено", "#76519a")):
        tiles.append(f'<td width="25%" valign="top" style="padding:17px 6px;border-right:1px solid #e4e8e3;">'
                     f'<div style="font-size:27px;line-height:32px;font-weight:700;color:{color};">{number}</div>'
                     f'<div style="font-size:12px;line-height:18px;color:#62747a;margin-top:3px;">{label}</div></td>')
    sections = []
    for key in SECTIONS:
        group = groups.get(key, [])
        notices = series_changes if key == "changes" else []
        if not group and not notices:
            continue
        records = [event for _, event in group] + notices
        show_email = len({_account_email(record).casefold() for record in records}) > 1
        content = _section_header(key, len(group) + len(notices), records, len(sections) + 1)
        content += "".join(_card(event, index, day, action_links, section=key, show_email=show_email)
                           for index, event in group)
        if notices:
            content += (
                '<div class="series-changes"><h3 style="margin:22px 0 9px;'
                'font-size:17px;line-height:25px;font-weight:600;color:#76519a;">'
                'Изменения повторяющихся событий</h3>'
                '<p style="margin:0 0 14px;font-size:13px;line-height:21px;color:#62747a;">'
                'Интервалы на этот день показаны отдельно для сохранённого и текущего расписания.</p>'
                + "".join(_series_card(notice, show_email=show_email) for notice in notices) + '</div>'
            )
        sections.append(f'<div class="digest-section" data-section="{key}">{content}</div>')
    if not events:
        sections.insert(0, f'<div style="margin-top:25px;padding:24px;border:1px solid #dce7e2;background:#ffffff;border-radius:10px;'
                        f'color:{INK};font-size:16px;line-height:26px;">На этот день событий нет.</div>')
    providers = []
    for record in [*events, *series_changes]:
        provider = SOURCE_NAMES.get(record.source_id, record.source_id)
        if provider and provider not in providers:
            providers.append(provider)
    source_note = f'Источники этого выпуска: {_text(" · ".join(providers))}.<br>' if providers else ""
    first_note = ('<p style="margin:12px 0 0;">Прежнее время одиночных событий отслеживается с первого сохранённого снимка.</p>'
                  if first_snapshot else "")
    instructions = ('Нажмите на кнопку у события, чтобы посмотреть подтверждение или выбрать новое время. '
                    'Это образец: действия не изменяют календари.') if interactive else (
                    'Кнопки показаны для согласования. В рабочем письме они откроют страницу подтверждения действия.')
    if action_links is not None:
        instructions = ('Кнопки открывают защищённую страницу управления событием. '
                        'После входа подтвердите действие или выберите новую дату и время. '
                        'Ссылки действуют 48 часов. Данные в письме отражают момент его подготовки.')
    edition = 'ДАЙДЖЕСТ НА ЗАВТРА' if action_links is not None else 'ОБРАЗЕЦ ДАЙДЖЕСТА'
    closing = 'Изменения сохраняются в исходном календаре.' if action_links is not None else 'Подготовлено для согласования оформления.'
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><meta name="supported-color-schemes" content="light">
<title>Дайджест · {_date(day)} {day.year}</title>
<style>
body, table, td, a, button, input {{ font-family: Arial, Helvetica, sans-serif; }}
body {{ margin:0; padding:0; background:#f2f1ed; color:#344c54; -webkit-text-size-adjust:100%; }}
table {{ border-spacing:0; }} a:focus-visible, button:focus-visible, input:focus-visible {{ outline:3px solid #c39b4d; outline-offset:3px; }}
.action:hover {{ opacity:.85; }} [hidden] {{ display:none !important; }}
dialog {{ border:1px solid #dfe8e5;border-radius:14px;max-width:430px;width:calc(100% - 54px);padding:26px;color:#344c54;box-shadow:0 16px 60px #183e4533; }}
dialog::backdrop {{ background:#183e4577; }} dialog h2 {{ color:{INK};font-size:22px;line-height:30px;margin:10px 0; }}
dialog p {{ font-size:14px;line-height:22px;overflow-wrap:anywhere; }} .dialog-kicker {{ font-size:10px;letter-spacing:1.8px;color:#718188; }}
dialog label {{ display:block;font-size:12px;margin:13px 0;color:{INK}; }} dialog input {{ display:block;box-sizing:border-box;width:100%;margin-top:6px;padding:11px;border:1px solid #b8cbc4;border-radius:6px;font-size:15px; }}
.time-fields {{ display:flex;gap:16px; }} .time-fields label {{ flex:1; }} .dialog-small {{ color:#718188;font-size:12px; }}
.demo-note {{ padding:12px;background:#fff7e6;border-radius:6px;color:#715c32; }}
.dialog-buttons {{ display:flex;gap:10px;justify-content:flex-end;margin-top:22px; }}
dialog button {{ background:#fff;border:1px solid #b8cbc4;border-radius:6px;padding:10px 14px;color:{INK};font-size:13px;cursor:pointer; }}
#submit-action {{ background:{INK};color:#fff;border-color:{INK}; }} #dialog-result {{ padding:13px;background:#eaf3ef;border-radius:6px;color:#315d4c; }}
@media (max-width:560px) {{ .outer-pad {{ padding:8px !important; }} .main-pad {{ padding:24px 14px !important; }} .hero-pad {{ padding:28px 22px !important; }} .hero-title {{ font-size:36px !important;line-height:42px !important; }} .time-column {{ width:74px !important;padding:16px 8px 16px 10px !important; }} .event-content {{ padding:15px 12px !important; }} .action {{ margin-top:7px !important; }} .section-heading {{ font-size:19px !important;line-height:26px !important; }} }}
@media print {{ body {{ background:#fff; }} .outer-pad {{ padding:0 !important; }} .main-pad {{ padding:20px 12px !important; }} .event-card {{ box-shadow:none; }} .section-heading {{ break-after:avoid;page-break-after:avoid; }} dialog {{ display:none !important; }} }}
</style></head>
<body style="margin:0;padding:0;background:#f2f1ed;color:#344c54;font-family:Arial,Helvetica,sans-serif;"><div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{len(events)} событий на {_date(day)}: встречи, важные даты, поручения и изменения в расписании.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td class="outer-pad" align="center" style="padding:28px 14px;">
<table role="presentation" width="720" cellpadding="0" cellspacing="0" style="width:100%;max-width:720px;background:#fafbf8;border:1px solid #dce4df;border-radius:16px;overflow:hidden;">
<tr><td class="hero-pad" style="padding:32px 34px 30px;background:#244f59;border-bottom:4px solid #c39b4d;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="font-size:11px;line-height:18px;letter-spacing:2px;font-weight:600;color:#e5efeb;">ВАШ КАЛЕНДАРЬ</td>
<td align="right" style="font-size:10px;line-height:18px;letter-spacing:1px;color:#d0e0db;">{edition}</td></tr></table>
<p style="margin:25px 0 5px;font-size:16px;line-height:24px;color:#d7e7e1;">{WEEKDAYS[day.weekday()]}</p>
<h1 class="hero-title" style="margin:0;font-family:Georgia,'Times New Roman',serif;font-size:44px;line-height:50px;letter-spacing:-.7px;font-weight:400;color:#ffffff;">{_date(day)}</h1>
<p style="margin:13px 0 0;font-size:13px;line-height:21px;color:#d7e7e1;">{day.year} год &nbsp;·&nbsp; Всё время — московское</p>
</td></tr>
<tr><td class="main-pad" style="padding:26px 34px 30px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background:#ffffff;border:1px solid #e1e7df;border-radius:10px;text-align:center;"><tr>{''.join(tiles)}</tr></table>
<div style="margin-top:18px;line-height:24px;">{categories}</div>
{''.join(sections)}
<div id="actions-demo" style="margin-top:29px;padding:19px 20px;background:#edf2ed;border:1px solid #dce7df;border-radius:9px;color:#49665f;font-size:13px;line-height:22px;">
<strong style="color:{INK};">Действия с событиями</strong><br>{instructions}</div>
<div style="margin-top:26px;padding-top:18px;border-top:1px solid #dfe7df;color:#62747a;font-size:12px;line-height:20px;">
{source_note}Все приглашения включены.<br>Отметка «Отменено» отражает статус записи в календаре. Ответ на приглашение показан отдельно.
{first_note}<p style="margin:12px 0 0;letter-spacing:.3px;">{closing}</p></div>
</td></tr></table></td></tr></table>
{DEMO if interactive else ''}</body></html>'''
