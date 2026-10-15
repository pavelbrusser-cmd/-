"""Private, durable calendar cache and automatic push receipts.

This module deliberately imports only the standard library. A successful full
inventory and its reminder plan are committed together. Provider acceptance or
an interrupted submission permanently consumes that reminder's stable identity.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat


HORIZON_DAYS = 7
OVERDUE_GRACE_SECONDS = 15 * 60
SYNC_FRESH_SECONDS = 120
SYNC_HEALTH_SECONDS = 10 * 60
RETRY_DELAY_SECONDS = 60
_STATES = ("pending", "sending", "accepted", "failed", "uncertain", "cancelled", "expired")
_TERMINAL = {"accepted", "uncertain"}
_CACHE_FIELDS = ("source_id", "calendar_id", "calendar_name", "href", "etag", "data")
_CONTACT_FIELDS = ("book_id", "uid", "name", "month", "day")


class QueueError(Exception):
    """A diagnostic safe for logs, without resource content or paths."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise QueueError("Для расписания push требуется время с часовым поясом.")
    return value.timestamp()


def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)


def _private_open(name, directory_fd):
    descriptor = os.open(name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
                         | os.O_NONBLOCK, 0o600, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
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


def _cache_row(resource):
    if not isinstance(resource, dict) or any(field not in resource for field in _CACHE_FIELDS):
        raise QueueError("Неполный результат чтения календарей; расписание не сохранено.")
    if any(not isinstance(resource[field], str) for field in _CACHE_FIELDS if field != "etag"):
        raise QueueError("Некорректный результат чтения календарей; расписание не сохранено.")
    if resource["etag"] is not None and not isinstance(resource["etag"], str):
        raise QueueError("Некорректный результат чтения календарей; расписание не сохранено.")
    if any(not resource[field] for field in ("source_id", "calendar_id", "href", "data")):
        raise QueueError("Неполный результат чтения календарей; расписание не сохранено.")
    return tuple(resource[field] for field in _CACHE_FIELDS)


def _reminder_row(reminder):
    if not isinstance(reminder, dict):
        raise QueueError("Некорректное напоминание; расписание не сохранено.")
    for field in ("source_id", "calendar_id", "calendar_name", "uid", "title", "event_start", "notify_at"):
        if not isinstance(reminder.get(field), str):
            raise QueueError("Неполное напоминание; расписание не сохранено.")
    if reminder.get("recurrence_id") is not None and not isinstance(reminder["recurrence_id"], str):
        raise QueueError("Некорректное повторение; расписание не сохранено.")
    if (reminder.get("category") not in {"birthdays", "control", "periodic_control"}
            or reminder.get("rule") not in {"d3", "d1", "d0", "h3", "h1"}
            or not isinstance(reminder.get("all_day"), bool)):
        raise QueueError("Некорректное правило; расписание не сохранено.")
    identity = [reminder["source_id"], reminder["calendar_id"], reminder["uid"],
                reminder.get("recurrence_id"), reminder["rule"]]
    expected = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    if reminder.get("stable_id") != expected:
        raise QueueError("Не подтверждена идентичность напоминания; расписание не сохранено.")
    try:
        due = _timestamp(datetime.fromisoformat(reminder["notify_at"]))
        payload = _json(reminder)
    except (TypeError, ValueError):
        raise QueueError("Некорректное время напоминания; расписание не сохранено.") from None
    return expected, due, payload


def _contact_row(contact):
    # Full vCards and additional contact properties must never enter this table.
    if not isinstance(contact, dict) or set(contact) != set(_CONTACT_FIELDS):
        raise QueueError("Некорректный минимальный набор данных дня рождения.")
    if any(not isinstance(contact[field], str) or not contact[field].strip()
           for field in ("book_id", "uid", "name")):
        raise QueueError("Неполная запись дня рождения; расписание не сохранено.")
    if type(contact["month"]) is not int or type(contact["day"]) is not int:
        raise QueueError("Некорректная дата дня рождения; расписание не сохранено.")
    try:
        date(2000, contact["month"], contact["day"])
    except ValueError:
        raise QueueError("Некорректная дата дня рождения; расписание не сохранено.") from None
    return tuple(contact[field] for field in _CONTACT_FIELDS)


class QueueStore:
    """Use only while holding ``schedule_store``'s exclusive process lock."""

    def __init__(self, directory, directory_fd):
        self.connection = None
        descriptor = _private_open("schedule.sqlite3", directory_fd)
        try:
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    info = os.stat("schedule.sqlite3" + suffix, dir_fd=directory_fd,
                                   follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid()):
                    raise OSError("unsafe database sidecar")
            os.fsync(descriptor)
            os.fsync(directory_fd)
            self.connection = sqlite3.connect(directory / "schedule.sqlite3", timeout=5)
            _same_file("schedule.sqlite3", descriptor, directory_fd)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("PRAGMA journal_mode=DELETE")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise QueueError("Версия защищённого расписания push не поддерживается.")
            with self.connection:
                self.connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS resources (
                        source_id TEXT NOT NULL, calendar_id TEXT NOT NULL,
                        calendar_name TEXT NOT NULL, href TEXT NOT NULL,
                        etag TEXT, data TEXT NOT NULL,
                        PRIMARY KEY (source_id,calendar_id,href)
                    )
                """)
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS birthday_contacts (
                        book_id TEXT NOT NULL, uid TEXT NOT NULL, name TEXT NOT NULL,
                        month INTEGER NOT NULL, day INTEGER NOT NULL,
                        PRIMARY KEY (book_id,uid)
                    )
                """)
                self.connection.execute("""
                    CREATE TABLE IF NOT EXISTS reminders (
                        stable_id TEXT PRIMARY KEY,
                        notify_at REAL NOT NULL,
                        payload TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN
                          ('pending','sending','accepted','failed','uncertain','cancelled','expired')),
                        retryable INTEGER NOT NULL DEFAULT 0,
                        retry_at REAL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                """)
                self.connection.execute("CREATE INDEX IF NOT EXISTS reminders_due ON reminders(state,notify_at)")
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

    def _get(self, key, default=None):
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def _set(self, key, value):
        self.connection.execute("INSERT INTO metadata(key,value) VALUES (?,?) "
                                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _json(value)))

    def _expire(self, now):
        stamp = _timestamp(now)
        self.connection.execute("UPDATE reminders SET state='expired',retryable=0,retry_at=NULL,updated_at=? "
                                "WHERE state IN ('pending','failed') AND notify_at<?",
                                (stamp, stamp - OVERDUE_GRACE_SECONDS))
        activation = self._get("activated_at")
        if activation is not None:
            self.connection.execute("UPDATE reminders SET state='expired',retryable=0,retry_at=NULL,updated_at=? "
                                    "WHERE state IN ('pending','failed') AND notify_at<?", (stamp, activation))

    def recover(self, now):
        # The exclusive flock proves that no previous sender is still working.
        with self.connection:
            self.connection.execute("UPDATE reminders SET state='uncertain',retryable=0,retry_at=NULL,updated_at=? "
                                    "WHERE state='sending'", (_timestamp(now),))
            self._expire(now)

    def load_cache(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT source_id,calendar_id,calendar_name,href,etag,data FROM resources "
            "ORDER BY source_id,calendar_id,href")]

    def load_contacts(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT book_id,uid,name,month,day FROM birthday_contacts ORDER BY book_id,uid")]

    def _contacts_metadata(self, enabled, addressbook_count):
        if (type(enabled) is not bool or type(addressbook_count) is not int or addressbook_count < 0
                or (not enabled and addressbook_count != 0)
                or self.connection.execute("SELECT COUNT(DISTINCT book_id) FROM birthday_contacts").fetchone()[0] > addressbook_count):
            raise QueueError("Некорректные настройки источника дней рождения.")
        self._set("contacts_enabled", enabled)
        self._set("addressbooks", addressbook_count)

    def _sync_metadata(self, now, calendar_count, sync_summary, *, observed_at=None):
        if isinstance(calendar_count, bool) or not isinstance(calendar_count, int) or calendar_count < 0:
            raise QueueError("Некорректное число календарей; расписание не сохранено.")
        if not isinstance(sync_summary, dict):
            raise QueueError("Некорректный результат сверки календарей.")
        completed = _timestamp(now)
        observed = completed if observed_at is None else _timestamp(observed_at)
        if observed > completed:
            raise QueueError("Некорректное время наблюдения календарей; расписание не сохранено.")
        self._set("last_sync_at", completed)
        self._set("last_observation_at", observed)
        self._set("calendars", calendar_count)
        self._set("sync_summary", sync_summary)

    def commit_plan(self, cache, reminders, *, now, horizon, calendar_count, sync_summary,
                    policy_version="1", observed_at=None, contacts=None,
                    addressbook_count=0, contacts_enabled=False):
        """Atomically replace the complete cache and reconcile authoritative work.

        A failed/incomplete provider read must never call this method. Accepted
        and uncertain identities survive every refresh, move and deletion.
        """
        stamp, end = _timestamp(now), _timestamp(horizon)
        if not stamp < end <= stamp + HORIZON_DAYS * 86400:
            raise QueueError("Некорректный горизонт расписания push.")
        if not isinstance(policy_version, str) or not policy_version or len(policy_version) > 64:
            raise QueueError("Некорректная версия правил push.")
        rows = [_cache_row(resource) for resource in cache]
        if len({(row[0], row[1], row[3]) for row in rows}) != len(rows):
            raise QueueError("Повторные ресурсы календаря; расписание не сохранено.")
        if contacts is None and contacts_enabled:
            raise QueueError("Не получен полный снимок дней рождения; расписание не сохранено.")
        contact_rows = [_contact_row(contact) for contact in (contacts if contacts is not None else [])]
        if (len({(row[0], row[1]) for row in contact_rows}) != len(contact_rows)
                or (contact_rows and not contacts_enabled)
                or type(addressbook_count) is not int
                or len({row[0] for row in contact_rows}) > addressbook_count):
            raise QueueError("Неполный или противоречивый снимок дней рождения.")
        plan = [_reminder_row(reminder) for reminder in reminders]
        identities = {row[0] for row in plan}
        if len(identities) != len(plan) or any(due >= end for _, due, _ in plan):
            raise QueueError("Повторное напоминание или время вне горизонта; расписание не сохранено.")
        with self.connection:
            self.connection.execute("DELETE FROM resources")
            self.connection.executemany("INSERT INTO resources VALUES (?,?,?,?,?,?)", rows)
            self.connection.execute("DELETE FROM birthday_contacts")
            self.connection.executemany("INSERT INTO birthday_contacts VALUES (?,?,?,?,?)", contact_rows)
            self._contacts_metadata(contacts_enabled, addressbook_count)
            existing = {row["stable_id"]: row for row in self.connection.execute("SELECT * FROM reminders")}
            for stable_id, due, payload in plan:
                prior = existing.get(stable_id)
                if prior is not None and prior["state"] in _TERMINAL | {"sending"}:
                    continue
                state, retryable, retry_at = "pending", 1, None
                if prior is not None:
                    if prior["state"] == "failed" and prior["payload"] == payload:
                        state, retryable, retry_at = "failed", prior["retryable"], prior["retry_at"]
                    elif prior["state"] in {"expired", "cancelled"} and due < stamp:
                        state, retryable = prior["state"], 0
                self.connection.execute("""
                    INSERT INTO reminders(stable_id,notify_at,payload,state,retryable,retry_at,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(stable_id) DO UPDATE SET
                    notify_at=excluded.notify_at,payload=excluded.payload,state=excluded.state,
                    retryable=excluded.retryable,retry_at=excluded.retry_at,updated_at=excluded.updated_at
                """, (stable_id, due, payload, state, retryable, retry_at, stamp, stamp))
            for stable_id, prior in existing.items():
                if stable_id not in identities and prior["state"] in {"pending", "failed"}:
                    self.connection.execute("UPDATE reminders SET state='cancelled',retryable=0,retry_at=NULL,updated_at=? "
                                            "WHERE stable_id=?", (stamp, stable_id))
            self._sync_metadata(now, calendar_count, sync_summary, observed_at=observed_at)
            self._set("planned_until", end)
            self._set("policy_version", policy_version)
            self._expire(now)

    def mark_sync(self, now, calendar_count, sync_summary, *, observed_at=None,
                  contacts_enabled=None, addressbook_count=None):
        """Mark a successful unchanged complete inventory, without replanning."""
        with self.connection:
            current_enabled = self._get("contacts_enabled", False)
            if contacts_enabled is not None and contacts_enabled != current_enabled:
                raise QueueError("Изменение источника дней рождения требует нового плана.")
            self._contacts_metadata(current_enabled if contacts_enabled is None else contacts_enabled,
                                    self._get("addressbooks", 0) if addressbook_count is None else addressbook_count)
            self._sync_metadata(now, calendar_count, sync_summary, observed_at=observed_at)
            self._expire(now)

    def activate(self, now):
        """First activation suppresses all older pending reminders permanently."""
        _timestamp(now)
        with self.connection:
            if self._get("activated_at") is None:
                self._set("activated_at", _timestamp(now))
            self._expire(now)

    def due(self, now, limit=50):
        stamp = _timestamp(now)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise QueueError("Некорректный размер очереди push.")
        activation = self._get("activated_at")
        if activation is None:
            return []
        rows = self.connection.execute("""
            SELECT payload FROM reminders WHERE (state='pending' OR (state='failed' AND retryable=1))
            AND notify_at>=? AND notify_at>=? AND notify_at<=? AND (retry_at IS NULL OR retry_at<=?)
            ORDER BY notify_at,stable_id LIMIT ?
        """, (activation, stamp - OVERDUE_GRACE_SECONDS, stamp, stamp, limit))
        return [json.loads(row["payload"]) for row in rows]

    def claim(self, stable_id, *, now):
        """Durably checkpoint immediately before a provider request; never retry an unknown outcome."""
        stamp = _timestamp(now)
        activation = self._get("activated_at")
        observed = self._get("last_observation_at", self._get("last_sync_at"))
        if activation is None or observed is None or not 0 <= stamp - observed <= SYNC_FRESH_SECONDS:
            return False
        with self.connection:
            updated = self.connection.execute("""
                UPDATE reminders SET state='sending',retryable=0,retry_at=NULL,updated_at=?
                WHERE stable_id=? AND (state='pending' OR (state='failed' AND retryable=1))
                AND notify_at>=? AND notify_at>=? AND notify_at<=? AND (retry_at IS NULL OR retry_at<=?)
            """, (stamp, stable_id, activation, stamp - OVERDUE_GRACE_SECONDS, stamp, stamp))
        return updated.rowcount == 1

    def complete(self, stable_id, state, *, now, retryable=False):
        stamp = _timestamp(now)
        if state not in {"accepted", "uncertain", "failed"}:
            raise QueueError("Некорректный результат отправки push.")
        retryable = state == "failed" and bool(retryable)
        # An uncertain provider result consumes identity even if a broken
        # transport forgot its callback. It must never become retryable again.
        expected = ("sending",) if state == "accepted" else ("sending", "pending", "failed")
        placeholders = ",".join("?" for _ in expected)
        with self.connection:
            updated = self.connection.execute(
                "UPDATE reminders SET state=?,retryable=?,retry_at=?,updated_at=? "
                f"WHERE stable_id=? AND state IN ({placeholders})",
                (state, int(retryable), stamp + RETRY_DELAY_SECONDS if retryable else None,
                 stamp, stable_id, *expected))
            if updated.rowcount != 1:
                raise QueueError("Состояние push изменилось; повторная отправка остановлена.")

    def summary(self, now):
        stamp = _timestamp(now)
        with self.connection:
            self._expire(now)
        activated, synced = self._get("activated_at"), self._get("last_sync_at")
        observed = self._get("last_observation_at", synced)
        counts = dict.fromkeys(_STATES, 0)
        counts.update({row["state"]: row["total"] for row in self.connection.execute(
            "SELECT state,COUNT(*) AS total FROM reminders GROUP BY state")})
        next_due = None
        if activated is not None:
            next_due = self.connection.execute("""
                SELECT MIN(MAX(notify_at,COALESCE(retry_at,notify_at))) FROM reminders
                WHERE (state='pending' OR (state='failed' AND retryable=1)) AND notify_at>=?
            """, (activated,)).fetchone()[0]
        return {"activated_at": _iso(activated), "last_sync_at": _iso(synced),
                "last_observation_at": _iso(observed),
                "sync_recent": synced is not None and 0 <= stamp - synced <= SYNC_HEALTH_SECONDS,
                "sync_fresh_for_delivery": observed is not None and 0 <= stamp - observed <= SYNC_FRESH_SECONDS,
                "next_due_at": _iso(next_due), "planned_until": _iso(self._get("planned_until")),
                "policy_version": self._get("policy_version"),
                "calendars": self._get("calendars", 0),
                "contacts_enabled": self._get("contacts_enabled", False),
                "contacts": self.connection.execute("SELECT COUNT(*) FROM birthday_contacts").fetchone()[0],
                "addressbooks": self._get("addressbooks", 0),
                "resources": self.connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
                "counts": counts}


@contextmanager
def schedule_store(directory: Path, *, now=None):
    """Lock a private schedule without changing Render's managed mount mode."""
    now = datetime.now(timezone.utc) if now is None else now
    _timestamp(now)
    root_fd = lock_fd = private_fd = None
    store = None
    try:
        directory = Path(directory)
        configured = os.environ.get("DIGEST_STATE_DIR")
        if configured and Path(configured).resolve() != directory.resolve():
            raise QueueError("Каталог расписания должен совпадать с DIGEST_STATE_DIR.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        lock_fd = _private_open(".push-schedule.lock", root_fd)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                raise QueueError("Операция с расписанием push уже выполняется.", retryable=True) from None
            raise
        _same_file(".push-schedule.lock", lock_fd, root_fd)
        try:
            os.mkdir("push-schedule", mode=0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
        private_fd = os.open("push-schedule", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=root_fd)
        if os.fstat(private_fd).st_uid != os.geteuid():
            raise OSError("private directory belongs to another user")
        os.fchmod(private_fd, 0o700)
        _same_file("push-schedule", private_fd, root_fd)
        store = QueueStore(directory / "push-schedule", private_fd)
        _same_file("push-schedule", private_fd, root_fd)
        _same_file(".push-schedule.lock", lock_fd, root_fd)
        store.recover(now)
        yield store
    except QueueError:
        raise
    except Exception:
        raise QueueError("Не удалось выполнить операцию с защищённым расписанием push.") from None
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
