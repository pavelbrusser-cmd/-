"""TLS-only SMTP delivery with an explicit boundary before submitting a message.

Successful SMTP submission means the relay accepted the message, not that the
recipient inbox has received it. A lost reply after DATA must never trigger an
automatic retry: the relay may already have accepted the message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime
import os
import re
import smtplib
import ssl
from typing import TYPE_CHECKING, Callable, Iterable

if TYPE_CHECKING:
    from .selection import DigestEvent


class SMTPDeliveryError(Exception):
    """A safe diagnostic without server responses, addresses, or credentials."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class SMTPDeliveryUncertain(SMTPDeliveryError):
    """The relay might have accepted DATA; an automatic retry could duplicate it."""


_DOMAIN = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z")
_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")
_MESSAGE_ID = re.compile(r"<[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+>\Z")
_TIMEOUT = 30


def _address(value: str) -> bool:
    if not isinstance(value, str) or not value.isascii() or len(value) > 254:
        return False
    parts = value.split("@")
    return (len(parts) == 2 and len(parts[0]) <= 64
            and bool(_LOCAL.fullmatch(parts[0])) and bool(_DOMAIN.fullmatch(parts[1])))


@dataclass(frozen=True)
class MailSettings:
    username: str = field(repr=False)
    password: str = field(repr=False)
    recipient: str = field(repr=False)
    host: str = "smtp.yandex.ru"
    port: int = 465
    security: str = "ssl"
    sender_name: str = "Ваш календарь"

    def __post_init__(self):
        if not isinstance(self.host, str) or not _DOMAIN.fullmatch(self.host):
            raise SMTPDeliveryError("SMTP_HOST должен содержать имя почтового сервера без URL и порта.")
        if type(self.port) is not int or (self.security, self.port) not in {("ssl", 465), ("starttls", 587)}:
            raise SMTPDeliveryError("Используйте SMTP_SECURITY=ssl с SMTP_PORT=465 или starttls с SMTP_PORT=587.")
        if not _address(self.username) or not _address(self.recipient):
            raise SMTPDeliveryError("SMTP_USERNAME и DIGEST_RECIPIENT должны содержать по одному полному адресу без имени и разделителей.")
        if (not isinstance(self.password, str) or not self.password.strip() or not self.password.isascii()
                or any(character in self.password for character in "\r\n\x00")):
            raise SMTPDeliveryError("Укажите SMTP_PASSWORD в защищённых переменных среды.")
        if (not isinstance(self.sender_name, str) or not self.sender_name.strip()
                or len(self.sender_name) > 200
                or any(ord(character) < 32 or ord(character) == 127 for character in self.sender_name)):
            raise SMTPDeliveryError("DIGEST_SENDER_NAME должен содержать имя отправителя в одной строке.")

    @classmethod
    def from_environment(cls) -> "MailSettings":
        names = ("SMTP_USERNAME", "SMTP_PASSWORD", "DIGEST_RECIPIENT")
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise SMTPDeliveryError("Добавьте защищённые переменные среды: " + ", ".join(missing))
        try:
            port = int(os.environ.get("SMTP_PORT", "465"))
        except ValueError:
            raise SMTPDeliveryError("SMTP_PORT должен быть числом 465 или 587.") from None
        return cls(
            host=os.environ.get("SMTP_HOST", "smtp.yandex.ru").strip(),
            port=port,
            security=os.environ.get("SMTP_SECURITY", "ssl").strip().lower(),
            username=os.environ["SMTP_USERNAME"].strip(" \t"),
            password=os.environ["SMTP_PASSWORD"],
            recipient=os.environ["DIGEST_RECIPIENT"].strip(" \t"),
            sender_name=os.environ.get("DIGEST_SENDER_NAME", "Ваш календарь").strip(" \t"),
        )


def _response_error(message: str, code: int) -> SMTPDeliveryError:
    return SMTPDeliveryError(message, retryable=400 <= code < 500)


def _connection_error(error: Exception) -> SMTPDeliveryError:
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return _response_error("Почтовый сервер не подтвердил вход. Проверьте настройки SMTP и пароль приложения.", error.smtp_code)
    if isinstance(error, ssl.SSLError):
        return SMTPDeliveryError("Не удалось подтвердить защищённое соединение с почтовым сервером.")
    if isinstance(error, smtplib.SMTPResponseException):
        return _response_error("Почтовый сервер отклонил операцию до отправки письма.", error.smtp_code)
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return SMTPDeliveryError("Почтовый сервер не поддерживает требуемое защищённое подключение или вход.")
    return SMTPDeliveryError("Соединение с почтовым сервером прервалось до отправки письма.", retryable=True)


def _close(client, *, polite: bool = True):
    if client is None:
        return
    try:
        if polite:
            client.quit()
    except Exception:
        # In particular, failed QUIT cannot undo a successful DATA reply.
        pass
    finally:
        try:
            client.close()
        except Exception:
            pass


def _connect(settings: MailSettings):
    client = None
    try:
        context = ssl.create_default_context()
        if settings.security == "ssl":
            client = smtplib.SMTP_SSL(settings.host, settings.port, timeout=_TIMEOUT, context=context)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=_TIMEOUT)
        code, _ = client.ehlo()
        if code != 250:
            raise _response_error("Почтовый сервер не подтвердил возможности SMTP.", code)
        if settings.security == "starttls":
            if not client.has_extn("starttls"):
                raise SMTPDeliveryError("Почтовый сервер не поддерживает обязательное шифрование STARTTLS.")
            client.starttls(context=context)
            code, _ = client.ehlo()
            if code != 250:
                raise _response_error("Почтовый сервер не подтвердил SMTP после установки шифрования.", code)
        client.login(settings.username, settings.password)
        return client
    except SMTPDeliveryError:
        _close(client, polite=False)
        raise
    except (OSError, smtplib.SMTPException) as error:
        _close(client, polite=False)
        raise _connection_error(error) from None


def probe_smtp(settings: MailSettings) -> dict:
    """Authenticate over verified TLS, without MAIL, RCPT, or DATA commands."""
    client = _connect(settings)
    _close(client)
    return {"smtp": "ok", "security": settings.security, "authenticated": True, "sent": False}


def _date_label(day: date) -> str:
    from .email_html import MONTHS

    return f"{day.day} {MONTHS[day.month]} {day.year}"


def _moment(value: str) -> str:
    if "T" in value:
        moment = datetime.fromisoformat(value)
        return f"{_date_label(moment.date())}, {moment:%H:%M}"
    return _date_label(date.fromisoformat(value))


def _range(start: str, end: str | None) -> str:
    if not end or start == end:
        return _moment(start)
    if "T" not in start:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) - timedelta(days=1)
        return _date_label(first) if last <= first else f"{_date_label(first)} — {_date_label(last)}"
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    if first.date() == last.date():
        return f"{_date_label(first.date())}, {first:%H:%M}–{last:%H:%M}"
    return f"{_moment(start)} — {_moment(end)}"


def _plain_text(day: date, events: Iterable[DigestEvent], *, test: bool) -> str:
    from .email_html import PARTICIPATION_LABELS, SOURCE_NAMES, STATUS_LABELS, WEEKDAYS
    from .presentation import CATEGORY_LABELS, classify_event

    events = list(events)
    lines = [f"Ваш календарь на {_date_label(day)}", f"{WEEKDAYS[day.weekday()]}. Время московское.", ""]
    if test:
        lines.extend(["Проверочное письмо.", ""])
    if not events:
        lines.append("На этот день событий нет.")
    for all_day, label in ((True, "На весь день"), (False, "По времени")):
        group = [event for event in events if event.all_day == all_day]
        if not group:
            continue
        lines.extend([label, ""])
        for event in group:
            lines.extend([event.title or "Без названия", _range(event.start, event.end)])
            source = SOURCE_NAMES.get(event.source_id, event.source_id)
            lines.append(f"{source} / {event.calendar_name}")
            metadata = [CATEGORY_LABELS[classify_event(event)]]
            if event.recurring:
                metadata.append("Повторяющееся событие")
            if event.multi_day:
                metadata.append("Несколько дней")
            lines.append(" · ".join(metadata))
            if event.event_status in STATUS_LABELS:
                lines.append(STATUS_LABELS[event.event_status])
            if event.participation in PARTICIPATION_LABELS:
                lines.append("Ваш ответ: " + PARTICIPATION_LABELS[event.participation])
            if event.original_start:
                lines.extend(["Перенесено", "Было: " + _range(event.original_start, event.original_end),
                              "Стало: " + _range(event.start, event.end)])
            if event.location:
                lines.append("Место: " + event.location)
            if event.timezone_assumed:
                lines.append("Часовой пояс не указан; время показано по Москве.")
            lines.append("")
    lines.append("Кнопки действий доступны в HTML-версии письма.")
    return "\n".join(lines) + "\n"


def make_message(day: date, events: Iterable[DigestEvent], html: str, settings: MailSettings,
                 *, message_id: str, generated_at: datetime, test: bool = False) -> bytes:
    """Build a readable plain alternative and preserve the approved HTML layout."""
    if type(day) is not date or not isinstance(html, str):
        raise SMTPDeliveryError("Не удалось сформировать письмо: некорректные данные дайджеста.")
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
        raise SMTPDeliveryError("Не удалось сформировать письмо: некорректный Message-ID.")
    if (not isinstance(generated_at, datetime) or generated_at.tzinfo is None
            or generated_at.utcoffset() is None):
        raise SMTPDeliveryError("Время формирования письма должно содержать часовой пояс.")
    message = EmailMessage(policy=SMTP_POLICY)
    message["From"] = Address(display_name=settings.sender_name, addr_spec=settings.username)
    message["To"] = Address(addr_spec=settings.recipient)
    message["Subject"] = ("[Проверка] " if test else "") + f"Ваш календарь на {_date_label(day)}"
    message["Date"] = format_datetime(generated_at)
    message["Message-ID"] = message_id
    message.set_content(_plain_text(day, events, test=test), charset="utf-8", cte="quoted-printable")
    message.add_alternative(html, subtype="html", charset="utf-8", cte="quoted-printable")
    return message.as_bytes()


def submit_message(settings: MailSettings, message_bytes: bytes, *, before_data: Callable[[], None]) -> None:
    """Submit once, recording a durable caller-owned intent before SMTP DATA.

    Callback exceptions propagate unchanged and no DATA command is issued.
    Explicit rejection can be retried if transient; lost/ambiguous DATA replies
    are uncertain and must be reconciled by the owner instead of being retried.
    """
    if not isinstance(message_bytes, bytes) or not message_bytes:
        raise SMTPDeliveryError("Для отправки требуется готовое MIME-письмо.")
    client = _connect(settings)
    polite_close = True
    try:
        try:
            code, _ = client.mail(settings.username)
            if code != 250:
                raise _response_error("Почтовый сервер не разрешил отправку от выбранного адреса.", code)
            code, _ = client.rcpt(settings.recipient)
            if code not in (250, 251):
                raise _response_error("Почтовый сервер не принял получателя письма.", code)
        except SMTPDeliveryError:
            raise
        except (OSError, smtplib.SMTPException) as error:
            raise _connection_error(error) from None

        before_data()
        # After an interrupted DATA stream the server may still be reading the
        # message body. Close the socket, without appending a QUIT command to it.
        polite_close = False
        try:
            code, _ = client.data(message_bytes)
        except smtplib.SMTPDataError as error:
            if 400 <= error.smtp_code < 600:
                raise _response_error("Почтовый сервер отклонил письмо.", error.smtp_code) from None
            raise SMTPDeliveryUncertain("Ответ почтового сервера неоднозначен. Проверьте доставку перед повторной отправкой.") from None
        except (OSError, smtplib.SMTPException):
            raise SMTPDeliveryUncertain("Связь прервалась во время отправки. Письмо могло быть принято; проверьте доставку перед повторной отправкой.") from None
        if code != 250:
            if 400 <= code < 600:
                raise _response_error("Почтовый сервер отклонил письмо.", code)
            raise SMTPDeliveryUncertain("Ответ почтового сервера неоднозначен. Проверьте доставку перед повторной отправкой.")
        polite_close = True
    finally:
        _close(client, polite=polite_close)
