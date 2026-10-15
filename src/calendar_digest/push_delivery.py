"""Durable, explicitly invoked Pushover tests; no calendar data or scheduler.

An accepted receipt means acceptance by Pushover, not display on the phone.
A lost reply is never retried automatically, including after a process restart.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import os
from pathlib import Path
import sqlite3
import stat
import time
from zoneinfo import ZoneInfo

from .push_transport import PushoverSettings, PushError, PushUncertain, send_notification


_TEST_TITLE = "Календарь: проверка уведомлений"
_TEST_MESSAGE = (
    "Защищённые уведомления подключены. Если вы читаете этот текст на iPhone, "
    "доставка и расшифровка работают. Ежедневные напоминания пока не включены."
)


class PushDeliveryError(Exception):
    """A safe diagnostic without filesystem paths, credentials or API replies."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _day(now):
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PushDeliveryError("Для проверочного уведомления требуется время с часовым поясом.")
    return now.astimezone(ZoneInfo("Europe/Moscow")).date().isoformat()


def _private_open(name, directory_fd):
    descriptor = os.open(
        name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600, dir_fd=directory_fd,
    )
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.geteuid()):
            raise OSError("not an application-owned regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _same_file(name, descriptor, directory_fd):
    opened = os.fstat(descriptor)
    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise OSError("file was replaced")


class _PushStore:
    def __init__(self, directory, directory_fd):
        self.connection = None
        descriptor = _private_open("push.sqlite3", directory_fd)
        try:
            # SQLite opens journal files itself. The containing directory is
            # owned by this service and 0700; reject unsafe pre-existing files.
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    info = os.stat("push.sqlite3" + suffix, dir_fd=directory_fd,
                                   follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid()):
                    raise OSError("unsafe database sidecar")
            os.fsync(descriptor)
            os.fsync(directory_fd)
            self.connection = sqlite3.connect(directory / "push.sqlite3", timeout=5)
            _same_file("push.sqlite3", descriptor, directory_fd)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise PushDeliveryError("Версия журнала push-уведомлений не поддерживается.")
            with self.connection:
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS deliveries (
                        notification_date TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK (kind = 'test'),
                        state TEXT NOT NULL CHECK (state IN
                            ('pending', 'sending', 'accepted', 'failed', 'uncertain')),
                        retryable INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY (kind, notification_date)
                    )
                """)
                self.connection.execute("PRAGMA user_version=1")
            os.fsync(directory_fd)
        except BaseException:
            self.close()
            raise
        finally:
            os.close(descriptor)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def get(self, day):
        return self.connection.execute(
            "SELECT * FROM deliveries WHERE kind='test' AND notification_date=?", (day,),
        ).fetchone()

    def recover(self):
        # The process owns flock: a previous sender cannot still be active.
        with self.connection:
            self.connection.execute(
                "UPDATE deliveries SET state='uncertain', retryable=0, updated_at=? "
                "WHERE state='sending'", (time.time(),),
            )
            self.connection.execute(
                "UPDATE deliveries SET state='failed', retryable=1, updated_at=? "
                "WHERE state='pending'", (time.time(),),
            )

    def begin(self, day):
        now = time.time()
        with self.connection:
            self.connection.execute(
                "INSERT INTO deliveries "
                "(notification_date,kind,state,retryable,created_at,updated_at) "
                "VALUES (?,'test','pending',1,?,?) "
                "ON CONFLICT(kind,notification_date) DO UPDATE SET "
                "state='pending',retryable=1,updated_at=excluded.updated_at "
                "WHERE deliveries.state='failed'", (day, now, now),
            )
        if self.get(day)["state"] != "pending":
            raise PushDeliveryError("Повторная отправка проверочного уведомления остановлена.")

    def transition(self, day, state, *, retryable=False, expected=None):
        query = "UPDATE deliveries SET state=?,retryable=?,updated_at=? WHERE kind='test' AND notification_date=?"
        arguments = [state, int(retryable), time.time(), day]
        if expected is not None:
            query += " AND state=?"
            arguments.append(expected)
        with self.connection:
            if self.connection.execute(query, arguments).rowcount != 1:
                raise PushDeliveryError("Состояние уведомления изменилось; повторная отправка остановлена.")


@contextmanager
def _locked(directory):
    root_fd = lock_fd = private_fd = None
    store = None
    try:
        directory = Path(directory)
        configured = os.environ.get("DIGEST_STATE_DIR")
        if configured and Path(configured).resolve() != directory.resolve():
            raise PushDeliveryError("Каталог уведомлений должен совпадать с DIGEST_STATE_DIR.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        # Never chmod the managed mount root: on Render it can belong to root.
        lock_fd = _private_open(".push-delivery.lock", root_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise PushDeliveryError("Операция с push-уведомлениями уже выполняется.", retryable=True) from None
            raise
        _same_file(".push-delivery.lock", lock_fd, root_fd)
        try:
            os.mkdir("push-delivery", mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        private_fd = os.open("push-delivery", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=root_fd)
        if os.fstat(private_fd).st_uid != os.geteuid():
            raise OSError("private directory belongs to another user")
        os.fchmod(private_fd, 0o700)
        _same_file("push-delivery", private_fd, root_fd)
        store = _PushStore(directory / "push-delivery", private_fd)
        store.recover()
        yield store
    except PushDeliveryError:
        raise
    except Exception:
        raise PushDeliveryError("Не удалось выполнить операцию с защищённым журналом push-уведомлений.") from None
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        for descriptor in (private_fd, lock_fd, root_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


def _summary(row):
    return {"date": row["notification_date"], "kind": row["kind"],
            "state": row["state"], "retryable": bool(row["retryable"])}


def send_push_test(directory: Path, *, now: datetime | None = None) -> dict:
    """Send a harmless encrypted test at most once per Moscow calendar date.

    Re-running this explicit command may retry a known rejection after settings
    have been corrected. Accepted and uncertain attempts are never repeated.
    """
    day = _day(now)
    with _locked(directory) as store:
        row = store.get(day)
        if row is not None and row["state"] in {"accepted", "uncertain"}:
            return _summary(row)
        try:
            settings = PushoverSettings.from_environment()
        except Exception:
            raise PushDeliveryError("Проверьте защищённые настройки Pushover; проверочное уведомление не отправлено.") from None
        store.begin(day)
        sending = False

        def before_send():
            nonlocal sending
            store.transition(day, "sending", expected="pending")
            sending = True

        try:
            send_notification(settings, title=_TEST_TITLE, message=_TEST_MESSAGE, before_send=before_send)
        except PushUncertain:
            store.transition(day, "uncertain")
            raise PushDeliveryError("Результат отправки уведомления неизвестен; повтор запрещён.") from None
        except PushError as error:
            store.transition(day, "failed", retryable=bool(error.retryable))
            raise PushDeliveryError("Pushover не принял проверочное уведомление. Проверьте подключение и настройки.",
                                    retryable=bool(error.retryable)) from None
        except Exception as error:
            # A checkpoint commit can succeed even if its acknowledgement is
            # lost. Inspect the durable row before deciding whether retry is safe.
            actual = store.get(day)
            uncertain = sending or actual["state"] == "sending"
            retryable = not uncertain and bool(getattr(error, "retryable", True))
            store.transition(day, "uncertain" if uncertain else "failed", retryable=retryable)
            raise PushDeliveryError("Отправка остановлена; проверьте состояние push-уведомлений.",
                                    retryable=retryable) from None
        if not sending:
            store.transition(day, "uncertain")
            raise PushDeliveryError("Не подтверждён безопасный порядок отправки уведомления; повтор запрещён.")
        try:
            store.transition(day, "accepted", expected="sending")
        except Exception:
            raise PushDeliveryError("Pushover принял уведомление, но квитанция не сохранена; повтор запрещён.") from None
        return _summary(store.get(day))


def push_status(directory: Path) -> dict:
    """Return private-data-free receipts and recover interrupted attempts."""
    with _locked(directory) as store:
        rows = store.connection.execute(
            "SELECT * FROM deliveries ORDER BY notification_date DESC LIMIT 100",
        )
        return {"deliveries": [_summary(row) for row in rows]}
