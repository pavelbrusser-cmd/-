"""Private durable email outbox; importing this module never loads calendars.

SMTP acceptance is not inbox delivery. A lost acceptance receipt is deliberately
not retried: Message-ID alone does not prevent duplicate email.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

from .calendar_operation_lock import CalendarBusy
from .smtp_delivery import (
    MailSettings, SMTPDeliveryError, SMTPDeliveryUncertain, make_message, submit_message,
)


MAX_READY_AGE = 30 * 60
MAX_MESSAGE_BYTES = 20 * 1024 * 1024


def _utc_now():
    return datetime.now(timezone.utc)


def _check_daily_window(day, kind):
    if kind != "daily":
        return
    now = _utc_now().astimezone(ZoneInfo("Europe/Moscow"))
    if day != now.date() + timedelta(days=1):
        raise DeliveryError("Ежедневная отправка разрешена только для завтрашнего дня по Москве.")
    minute = now.hour * 60 + now.minute
    if not 17 * 60 <= minute < 17 * 60 + 30:
        raise DeliveryError("Ежедневная отправка разрешена с 17:00 до 17:30 по Москве.",
                            retryable=minute < 17 * 60)


class DeliveryError(Exception):
    """A fixed, private-data-free failure suitable for command-line output."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _identity(day, kind):
    if type(day) is not date or kind not in {"daily", "test"}:
        raise DeliveryError("Некорректная дата или тип отправки.")
    return day.isoformat(), kind


def _directory(directory):
    try:
        path = Path(directory).resolve()
        configured = os.environ.get("DIGEST_STATE_DIR")
        if configured and Path(configured).resolve() != path:
            raise DeliveryError("Каталог отправок должен совпадать с DIGEST_STATE_DIR.")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path
    except DeliveryError:
        raise
    except Exception:
        raise DeliveryError("Не удалось открыть каталог отправок.") from None


def _private_open(path, flags):
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not regular")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class DeliveryStore:
    """Used only while the process owns the shared delivery lock."""

    def __init__(self, directory):
        self.directory = directory
        path = directory / "deliveries.sqlite3"
        descriptor = _private_open(path, os.O_CREAT | os.O_RDWR)
        os.close(descriptor)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA journal_mode=DELETE")
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            self.connection.close()
            raise DeliveryError("Версия журнала отправок не поддерживается.")
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    digest_date TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('daily', 'test')),
                    state TEXT NOT NULL CHECK (state IN
                        ('preparing','ready','submitting','accepted','failed','uncertain')),
                    retryable INTEGER NOT NULL DEFAULT 0,
                    message_id TEXT NOT NULL,
                    sender TEXT,
                    recipient TEXT,
                    eml_name TEXT,
                    sha256 TEXT,
                    generated_at TEXT,
                    prepared_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    events INTEGER NOT NULL DEFAULT 0,
                    action_links INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (kind, digest_date)
                )
            """)
            self.connection.execute("PRAGMA user_version=1")

    def close(self):
        self.connection.close()

    def get(self, day, kind):
        return self.connection.execute(
            "SELECT * FROM deliveries WHERE digest_date=? AND kind=?", (day, kind),
        ).fetchone()

    def recover(self):
        # A live sender would own the same flock. No sender can still be active.
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state='uncertain', retryable=0, updated_at=? "
                "WHERE state='submitting'", (time.time(),),
            )
            self.connection.execute(
                "UPDATE deliveries SET state='failed', retryable=1, updated_at=? "
                "WHERE state='ready' AND (prepared_at < ? OR prepared_at > ?)",
                (time.time(), time.time() - MAX_READY_AGE, time.time() + 60),
            )

    def transition(self, day, kind, state, *, retryable=False, expected=None):
        with self.connection:
            statement = "UPDATE deliveries SET state=?, retryable=?, updated_at=? WHERE digest_date=? AND kind=?"
            parameters = [state, int(retryable), time.time(), day, kind]
            if expected is not None:
                statement += " AND state=?"
                parameters.append(expected)
            changed = self.connection.execute(statement, parameters).rowcount
            if changed != 1:
                raise DeliveryError("Состояние отправки изменилось; повторная отправка остановлена.")

    def begin(self, day, kind):
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries "
                "(digest_date,kind,state,retryable,message_id,created_at,updated_at) "
                "VALUES (?,?,'preparing',1,?,?,?)",
                (day, kind, f"<{uuid.uuid4().hex}@calendar-digest.local>", now, now),
            )
            self.connection.execute(
                "UPDATE deliveries SET state='preparing', retryable=1, sender=NULL, recipient=NULL, "
                "eml_name=NULL, sha256=NULL, generated_at=NULL, prepared_at=NULL, "
                "events=0, action_links=0, updated_at=? WHERE digest_date=? AND kind=?",
                (now, day, kind),
            )
        return self.get(day, kind)

    def ready(self, day, kind, *, settings, name, digest, generated_at, events, action_links):
        now = time.time()
        with self.connection:
            changed = self.connection.execute(
                "UPDATE deliveries SET state='ready', retryable=0, sender=?, recipient=?, "
                "eml_name=?, sha256=?, generated_at=?, prepared_at=?, updated_at=?, "
                "events=?, action_links=? WHERE digest_date=? AND kind=? AND state='preparing'",
                (settings.username, settings.recipient, name, digest, generated_at.isoformat(),
                 now, now, events, action_links, day, kind),
            ).rowcount
            if changed != 1:
                raise DeliveryError("Не удалось сохранить подготовленное письмо.")


def _summary(row):
    return {"date": row["digest_date"], "kind": row["kind"], "state": row["state"],
            "events": row["events"], "action_links": row["action_links"],
            "retryable": bool(row["retryable"])}


@contextmanager
def _locked(directory):
    descriptor = None
    store = None
    try:
        directory = _directory(directory)
        descriptor = _private_open(directory / ".delivery.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeliveryError("Подготовка или отправка уже выполняется.", retryable=True) from None
        store = DeliveryStore(directory)
        store.recover()
        yield store
    except (DeliveryError, CalendarBusy):
        raise
    except Exception:
        raise DeliveryError("Не удалось выполнить операцию с журналом отправок.") from None
    finally:
        if store is not None:
            store.close()
        if descriptor is not None:
            os.close(descriptor)


def _save_message(directory, message):
    if not isinstance(message, bytes) or not message or len(message) > MAX_MESSAGE_BYTES:
        raise DeliveryError("Подготовленное письмо имеет недопустимый размер.")
    target = directory / "deliveries"
    if target.is_symlink():
        raise DeliveryError("Каталог писем недоступен.")
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    name = uuid.uuid4().hex + ".eml"
    descriptor, temporary = tempfile.mkstemp(prefix=".message-", dir=target)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(message)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target / name)
        directory_fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return name, hashlib.sha256(message).hexdigest()


def prepare_delivery(day: date, directory: Path, *, kind="daily") -> dict:
    """Collect every source, then persist one complete immutable MIME message."""
    day_text, kind = _identity(day, kind)
    with _locked(directory) as store:
        row = store.get(day_text, kind)
        if row is not None and row["state"] in {"ready", "accepted", "uncertain"}:
            return _summary(row)
        row = store.begin(day_text, kind)
        try:
            settings = MailSettings.from_environment()
            # Keep scheduler/status/send resident memory independent of calendar parsing.
            from .action_links import compose_action_email
            from .preview import create_preview
            from .selection import DigestEvent

            create_preview(day, store.directory,
                           progress=lambda stage: print(stage.value, file=sys.stderr, flush=True))
            # One atomic JSON contains the whole selection; never mix its events
            # with an HTML file that a manual preview may replace concurrently.
            preview = store.directory / f"preview-{day_text}.json"
            if preview.stat().st_size > MAX_MESSAGE_BYTES:
                raise DeliveryError("Подготовленный дайджест слишком большой.")
            payload = json.loads(preview.read_text(encoding="utf-8"))
            if payload["date"] != day_text or payload["timezone"] != "Europe/Moscow":
                raise DeliveryError("Дата или часовой пояс дайджеста не совпадают.")
            events = [DigestEvent(**item) for item in payload["events"]]
            html, summary = compose_action_email(
                day, events, first_snapshot=bool(payload.get("first_snapshot", False)),
                state_dir=store.directory,
            )
            generated_at = _utc_now()
            message = make_message(day, events, html, settings, message_id=row["message_id"],
                                   generated_at=generated_at, test=kind == "test")
            name, digest = _save_message(store.directory, message)
            store.ready(day_text, kind, settings=settings, name=name, digest=digest,
                        generated_at=generated_at, events=len(events), action_links=summary["action_links"])
        except CalendarBusy:
            # Keep the durable row retryable and preserve the typed signal all
            # the way through _locked to the CLI's temporary-busy exit code.
            # The scheduler can then retry after 30 seconds instead of missing
            # short gaps between push synchronizations with five-minute waits.
            store.transition(day_text, kind, "failed", retryable=True)
            raise
        except Exception as error:
            retryable = getattr(error, "retryable", not isinstance(error, (ValueError, KeyError, TypeError)))
            store.transition(day_text, kind, "failed", retryable=retryable)
            raise DeliveryError("Не удалось подготовить новое письмо; отправка не выполнялась.",
                                retryable=retryable) from None
        return _summary(store.get(day_text, kind))


def _read_message(store, row, settings):
    if row["sender"] != settings.username or row["recipient"] != settings.recipient:
        raise DeliveryError("Адреса отправки изменились; подготовьте новое письмо.")
    name = row["eml_name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-f0-9]{32}\.eml", name):
        raise DeliveryError("Подготовленное письмо недоступно.")
    target = store.directory / "deliveries"
    if target.is_symlink():
        raise DeliveryError("Подготовленное письмо недоступно.")
    descriptor = _private_open(target / name, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as stream:
        message = stream.read(MAX_MESSAGE_BYTES + 1)
    if not message or len(message) > MAX_MESSAGE_BYTES or hashlib.sha256(message).hexdigest() != row["sha256"]:
        raise DeliveryError("Подготовленное письмо изменено или повреждено; отправка остановлена.")
    parsed = BytesParser(policy=policy.default).parsebytes(message, headersonly=True)
    if (parsed.defects or parsed.get_all("Message-ID") != [row["message_id"]]
            or len(parsed.get_all("From", [])) != 1 or len(parsed.get_all("To", [])) != 1
            or [address for _, address in getaddresses(parsed.get_all("From", []))] != [settings.username]
            or [address for _, address in getaddresses(parsed.get_all("To", []))] != [settings.recipient]
            or any(parsed.get_all(header) for header in ("Cc", "Bcc", "Resent-To", "Resent-Cc", "Resent-Bcc"))):
        raise DeliveryError("Заголовки подготовленного письма не совпадают с настройками.")
    return message


def submit_delivery(day: date, directory: Path, *, kind="daily") -> dict:
    """Send an eligible prepared message at most once after possible acceptance."""
    day_text, kind = _identity(day, kind)
    with _locked(directory) as store:
        row = store.get(day_text, kind)
        if row is None:
            raise DeliveryError("Сначала подготовьте письмо за выбранную дату.", retryable=True)
        if row["state"] in {"accepted", "uncertain"}:
            return _summary(row)
        _check_daily_window(day, kind)
        if row["state"] != "ready":
            raise DeliveryError("Нет свежего готового письма; сначала выполните подготовку.",
                                retryable=bool(row["retryable"]))
        try:
            settings = MailSettings.from_environment()
            message = _read_message(store, row, settings)
        except Exception as error:
            retryable = bool(getattr(error, "retryable", False))
            store.transition(day_text, kind, "failed", retryable=retryable)
            raise DeliveryError("Проверка подготовленного письма не пройдена; отправка не выполнялась.",
                                retryable=retryable) from None

        data_started = False

        def before_data():
            nonlocal data_started
            _check_daily_window(day, kind)
            if not 0 <= time.time() - row["prepared_at"] <= MAX_READY_AGE:
                raise DeliveryError("Подготовленное письмо устарело; требуется новый сбор.", retryable=True)
            store.transition(day_text, kind, "submitting", expected="ready")
            data_started = True

        try:
            submit_message(settings, message, before_data=before_data)
        except SMTPDeliveryUncertain:
            store.transition(day_text, kind, "uncertain")
            raise DeliveryError("Результат передачи письма неизвестен; автоматический повтор запрещён.") from None
        except SMTPDeliveryError as error:
            # Explicit SMTP rejection, even following DATA, proves non-acceptance.
            store.transition(day_text, kind, "failed", retryable=error.retryable)
            raise DeliveryError("Почтовый сервер не принял письмо.", retryable=error.retryable) from None
        except Exception as error:
            # Includes inability to commit the pre-DATA checkpoint. No payload
            # may be sent if its callback fails; conservatively block retries if
            # the checkpoint was durable but its commit acknowledgement was lost.
            actual = store.get(day_text, kind)
            uncertain = data_started or actual["state"] == "submitting"
            retryable = not uncertain and bool(getattr(error, "retryable", True))
            store.transition(day_text, kind, "uncertain" if uncertain else "failed", retryable=retryable)
            raise DeliveryError("Передача письма остановлена; проверьте состояние отправки.",
                                retryable=retryable) from None
        if not data_started:
            store.transition(day_text, kind, "uncertain")
            raise DeliveryError("Не подтверждён безопасный порядок передачи письма; повтор запрещён.")
        try:
            store.transition(day_text, kind, "accepted", expected="submitting")
        except Exception:
            # The server already accepted DATA. A failed receipt must NEVER
            # become a retryable job, even if the SQLite commit itself succeeded.
            raise DeliveryError("Сервер принял письмо, но квитанция не сохранена; повтор запрещён.") from None
        return _summary(store.get(day_text, kind))


def delivery_status(directory: Path, *, day: date | None = None) -> dict:
    """Safe counters only; interrupted SMTP attempts become non-retryable."""
    if day is not None:
        day_text, _ = _identity(day, "daily")
    with _locked(directory) as store:
        query = "SELECT * FROM deliveries"
        parameters = ()
        if day is not None:
            query += " WHERE digest_date=?"
            parameters = (day_text,)
        query += " ORDER BY digest_date DESC, kind ASC LIMIT 100"
        return {"deliveries": [_summary(row) for row in store.connection.execute(query, parameters)]}
