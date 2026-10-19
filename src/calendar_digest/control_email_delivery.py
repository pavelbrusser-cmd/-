"""Single-event participant reminders through the configured, real SMTP sender.

These are ordinary email messages, never invitations or organizer replies.
Only the selected event is rendered; recipient lists and owner action links
are deliberately absent from both MIME alternatives.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime
from html import escape
import re

from .push_timing import MOSCOW, reminder_was_deferred
from .smtp_delivery import MailSettings, SMTPDeliveryError, _address, submit_message


CONTROL_ACCOUNT = "gpb.control@mail.ru"
LEAD_LABELS = {
    "d3": "За 3 дня", "d1": "За 1 день", "h1": "За 1 час", "d0": "В день события",
    "w1": "За 1 неделю", "w2": "За 2 недели", "m1": "За 1 месяц", "m2": "За 2 месяца",
    "m3": "За 3 месяца", "m6": "За 6 месяцев", "y1": "За 1 год", "y2": "За 2 года",
}
_IDENTIFIER = re.compile(r"[a-f0-9]{64}\Z")


def _plain(value, limit):
    if not isinstance(value, str):
        raise ValueError("invalid text")
    text = " ".join("".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _moment(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("missing timezone")
    return moment.astimezone(MOSCOW)


def _event_time(reminder):
    start, end = reminder["event_start"], reminder["event_end"]
    if reminder["all_day"] is True:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) - timedelta(days=1) if end else first
        if last < first:
            raise ValueError("invalid date range")
        label = first.strftime("%d.%m.%Y")
        if last != first:
            label += " — " + last.strftime("%d.%m.%Y")
        return label + " · весь день"
    if reminder["all_day"] is not False:
        raise ValueError("invalid event type")
    first = _moment(start)
    last = _moment(end) if end else None
    if last is not None and last < first:
        raise ValueError("invalid time range")
    label = first.strftime("%d.%m.%Y, %H:%M")
    if last is not None and last != first:
        label += "–" + last.strftime("%H:%M") if last.date() == first.date() else " — " + last.strftime("%d.%m.%Y, %H:%M")
    return label + " МСК"


def make_control_email_message(reminder: dict, settings: MailSettings, *, generated_at: datetime) -> bytes:
    """Render one safe recipient and only the approved event fields."""
    try:
        identifier = reminder["stable_id"]
        recipient = reminder["recipient"]
        if (not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier)
                or not _address(recipient) or settings.recipient.casefold() != recipient.casefold()
                or reminder["source_id"] != "mailru_2"
                or reminder["account_email"].casefold() != CONTROL_ACCOUNT):
            raise ValueError("invalid message scope")
        if not isinstance(generated_at, datetime) or generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("missing timezone")
        title = _plain(reminder["title"], 1000) or "Без названия"
        location = _plain(reminder["location"] or "", 600)
        calendar = _plain(reminder["calendar_name"], 200) or "Контроль поручений"
        event_time = _event_time(reminder)
        lead = LEAD_LABELS[reminder["rule"]]
        if reminder["rule"] == "h1" and reminder_was_deferred(
                reminder["event_start"], "h1", reminder["notify_at"]):
            lead = "Перенесено на 07:00 МСК"
        source = calendar + " · " + CONTROL_ACCOUNT
        plain = ["Напоминание о событии", lead, "", title, event_time]
        if location:
            plain.append("Место: " + location)
        plain.extend(["", "Источник: " + source])
        location_html = ('<p style="margin:16px 0 0;color:#334155;">Место: ' + escape(location) + "</p>") if location else ""
        html = (
            '<!doctype html><html lang="ru"><meta charset="utf-8"><body style="margin:0;background:#f1f5f9;'
            'font-family:Arial,Helvetica,sans-serif;color:#172033;line-height:1.6;">'
            '<div style="max-width:600px;margin:24px auto;padding:28px;background:#ffffff;border-radius:16px;">'
            '<p style="margin:0;color:#2563eb;font-size:14px;font-weight:bold;">'
            '<span style="display:inline-block;margin-right:8px;padding:0 9px;border-radius:50%;'
            'background:#fee2e2;color:#b91c1c;">!</span>КОНТРОЛЬ И МОНИТОРИНГ ПОРУЧЕНИЙ</p>'
            '<p style="margin:10px 0;color:#526078;">' + escape(lead) + '</p>'
            '<h1 style="margin:12px 0 18px;color:#2563eb;font-size:24px;line-height:1.3;">' + escape(title) + '</h1>'
            '<p style="margin:0;font-size:18px;font-weight:bold;">' + escape(event_time) + '</p>'
            + location_html + '<p style="margin:24px 0 0;padding-top:18px;border-top:1px solid #e2e8f0;'
            'color:#64748b;font-size:13px;">Источник: ' + escape(source) + '</p></div></body></html>'
        )
        message = EmailMessage(policy=SMTP_POLICY)
        message["From"] = Address(display_name=settings.sender_name, addr_spec=settings.username)
        message["To"] = Address(addr_spec=recipient)
        message["Subject"] = "ВНИМАНИЕ! Контроль и мониторинг поручений!"
        message["Importance"] = "high"
        message["X-Priority"] = "1 (Highest)"
        message["X-MSMail-Priority"] = "High"
        message["Date"] = format_datetime(generated_at)
        message["Message-ID"] = f"<control-email.{identifier}@{settings.username.rsplit('@', 1)[1].lower()}>"
        message.set_content("\n".join(plain) + "\n", charset="utf-8", cte="quoted-printable")
        message.add_alternative(html, subtype="html", charset="utf-8", cte="quoted-printable")
        return message.as_bytes()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise SMTPDeliveryError("Не удалось оформить письмо участнику; отправка не выполнялась.") from None


def submit_control_email(reminder: dict, settings: MailSettings, *, generated_at: datetime, before_data) -> None:
    """Use a separate SMTP envelope for this participant; the sender stays real."""
    try:
        recipient_settings = replace(settings, recipient=reminder["recipient"])
    except (KeyError, TypeError):
        raise SMTPDeliveryError("Не подтверждён получатель письма участнику.") from None
    message = make_control_email_message(reminder, recipient_settings, generated_at=generated_at)
    submit_message(recipient_settings, message, before_data=before_data)
