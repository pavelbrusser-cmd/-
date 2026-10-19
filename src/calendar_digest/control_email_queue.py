"""Private cache and at-most-once receipts for individual control-calendar email.

The queue is independent of the daily digest and phone notifications. Its only
source is the selected mailru_2 calendar inventory. A successful full inventory
and the complete per-recipient plan are committed in one SQLite transaction.
"""

from contextlib import contextmanager
from datetime import date, datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from .push_timing import MOSCOW, NOT_BEFORE, delivery_allowed


HORIZON_DAYS = 7
OVERDUE_GRACE_SECONDS = 15 * 60
SYNC_FRESH_SECONDS = 120
SYNC_HEALTH_SECONDS = 10 * 60
RETRY_DELAY_SECONDS = 60
SOURCE_ID = "mailru_2"
ACCOUNT_EMAIL = "gpb.control@mail.ru"
_STATES = ("pending", "sending", "accepted", "failed", "uncertain", "cancelled", "expired")
_RECEIPTS = {"accepted", "uncertain", "sending"}
_RULES = ("h1", "d0", "d1", "d3", "w1", "w2", "m1", "m2", "m3", "m6", "y1", "y2")
_CACHE_FIELDS = ("source_id", "calendar_id", "calendar_name", "href", "etag", "data")
_REMINDER_FIELDS = {
    "source_id", "calendar_id", "calendar_name", "account_email", "uid", "recurrence_id",
    "recipient", "title", "event_start", "event_end", "all_day", "location", "rule",
    "notify_at", "category", "recurring", "stable_id",
}
_DOMAIN = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+\Z")
_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z")


class ControlEmailQueueError(Exception):
    """Safe diagnostic without addresses, event contents, or storage paths."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def _timestamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ControlEmailQueueError("Для расписания писем требуется время с часовым поясом.")
    return value.timestamp()


def _iso(value):
    return None if value is None else datetime.fromtimestamp(value, timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)
    except (TypeError, ValueError):
        raise ControlEmailQueueError("Некорректные данные расписания писем.") from None


def _digest(identity):
    return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


def _address(value):
    if (not isinstance(value, str) or not value.isascii() or value != value.lower()
            or len(value) > 254):
        return False
    parts = value.split("@")
    return (len(parts) == 2 and len(parts[0]) <= 64
            and bool(_LOCAL.fullmatch(parts[0])) and bool(_DOMAIN.fullmatch(parts[1])))


def _single_line(value, *, nonempty=False):
    return (isinstance(value, str) and (not nonempty or bool(value.strip()))
            and not any(ord(character) < 32 or ord(character) == 127 for character in value))


def _cache_row(resource):
    if not isinstance(resource, dict) or set(resource) != set(_CACHE_FIELDS):
        raise ControlEmailQueueError("Неполный результат чтения контрольного календаря.")
    if (resource["source_id"] != SOURCE_ID
            or any(not isinstance(resource[field], str) for field in _CACHE_FIELDS if field != "etag")
            or any(not resource[field] for field in ("calendar_id", "href", "data"))
            or (resource["etag"] is not None and not isinstance(resource["etag"], str))):
        raise ControlEmailQueueError("Некорректный результат чтения контрольного календаря.")
    return tuple(resource[field] for field in _CACHE_FIELDS)


def _reminder_row(reminder):
    if not isinstance(reminder, dict) or set(reminder) != _REMINDER_FIELDS:
        raise ControlEmailQueueError("Неполное индивидуальное напоминание; расписание не сохранено.")
    if (reminder["source_id"] != SOURCE_ID or reminder["account_email"] != ACCOUNT_EMAIL
            or reminder["category"] != "control_email" or reminder["rule"] not in _RULES
            or type(reminder["all_day"]) is not bool or type(reminder["recurring"]) is not bool
            or not _single_line(reminder["calendar_id"], nonempty=True)
            or not _single_line(reminder["uid"], nonempty=True)
            or not _single_line(reminder["title"])
            or not _single_line(reminder["calendar_name"])
            or not _address(reminder["recipient"]) or reminder["recipient"] == ACCOUNT_EMAIL
            or (reminder["recurrence_id"] is not None
                and not _single_line(reminder["recurrence_id"], nonempty=True))
            or (reminder["location"] is not None
                and (not isinstance(reminder["location"], str) or "\x00" in reminder["location"]))):
        raise ControlEmailQueueError("Некорректное индивидуальное напоминание; расписание не сохранено.")
    identity = [reminder[key] for key in ("source_id", "calendar_id", "uid", "recurrence_id", "rule", "recipient")]
    stable_id = _digest(identity)
    if reminder["stable_id"] != stable_id:
        raise ControlEmailQueueError("Не подтверждена идентичность индивидуального напоминания.")
    try:
        due_moment = datetime.fromisoformat(reminder["notify_at"])
        due = _timestamp(due_moment)
        if not delivery_allowed(due_moment):
            raise ValueError("outside delivery window")
        if reminder["all_day"]:
            start = date.fromisoformat(reminder["event_start"])
            end = None if reminder["event_end"] is None else date.fromisoformat(reminder["event_end"])
            if start.isoformat() != reminder["event_start"] or (end and end.isoformat() != reminder["event_end"]):
                raise ValueError("date format")
        else:
            start = datetime.fromisoformat(reminder["event_start"])
            _timestamp(start)
            end = None if reminder["event_end"] is None else datetime.fromisoformat(reminder["event_end"])
            if end is not None:
                _timestamp(end)
        if end is not None and end < start:
            raise ValueError("negative duration")
    except (TypeError, ValueError):
        raise ControlEmailQueueError("Некорректное время индивидуального напоминания.") from None
    # A rule can change while its recipient/occurrence/transmission instant does
    # not. Preserve that second identity to prevent configuration-based repeats.
    slot_id = _digest([reminder[key] for key in ("source_id", "calendar_id", "uid", "recurrence_id", "recipient")]
                      + [_iso(due)])
    return stable_id, slot_id, due, _json(reminder)


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


class ControlEmailStore:
    """Use only under the exclusive process lock from control_email_store."""

    def __init__(self, directory, directory_fd):
        self.connection = None
        descriptor = _private_open("schedule.sqlite3", directory_fd)
        try:
            for suffix in ("-journal", "-wal", "-shm"):
                try:
                    info = os.stat("schedule.sqlite3" + suffix, dir_fd=directory_fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
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
                raise ControlEmailQueueError("Версия защищённого расписания писем не поддерживается.")
            with self.connection:
                self.connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS resources (
                    source_id TEXT NOT NULL, calendar_id TEXT NOT NULL, calendar_name TEXT NOT NULL,
                    href TEXT NOT NULL, etag TEXT, data TEXT NOT NULL,
                    PRIMARY KEY (source_id,calendar_id,href))""")
                self.connection.execute("""CREATE TABLE IF NOT EXISTS reminders (
                    stable_id TEXT PRIMARY KEY, slot_id TEXT NOT NULL, notify_at REAL NOT NULL,
                    payload TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN
                        ('pending','sending','accepted','failed','uncertain','cancelled','expired')),
                    retryable INTEGER NOT NULL DEFAULT 0, retry_at REAL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
                self.connection.execute("CREATE INDEX IF NOT EXISTS reminders_due ON reminders(state,notify_at)")
                self.connection.execute("CREATE INDEX IF NOT EXISTS reminders_slots ON reminders(slot_id,state)")
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
        activation = self._get("activated_at")
        self.connection.execute("""UPDATE reminders SET state='expired',retryable=0,retry_at=NULL,updated_at=?
            WHERE (state='pending' OR (state='failed' AND retryable=1)) AND
            (notify_at<? OR (? IS NOT NULL AND notify_at<?))""",
            (stamp, stamp - OVERDUE_GRACE_SECONDS, activation, activation))

    def recover(self, now):
        with self.connection:
            self.connection.execute("UPDATE reminders SET state='uncertain',retryable=0,retry_at=NULL,updated_at=? "
                                    "WHERE state='sending'", (_timestamp(now),))
            self._expire(now)

    def load_cache(self):
        return [dict(row) for row in self.connection.execute(
            "SELECT source_id,calendar_id,calendar_name,href,etag,data FROM resources ORDER BY source_id,calendar_id,href")]

    def _sync_metadata(self, now, calendar_count, sync_summary, *, observed_at=None):
        completed = _timestamp(now)
        observed = completed if observed_at is None else _timestamp(observed_at)
        if (type(calendar_count) is not int or calendar_count < 0 or not isinstance(sync_summary, dict)
                or observed > completed
                or self.connection.execute("SELECT COUNT(DISTINCT calendar_id) FROM resources").fetchone()[0] > calendar_count):
            raise ControlEmailQueueError("Некорректный результат сверки контрольного календаря.")
        self._set("last_sync_at", completed)
        self._set("last_observation_at", observed)
        self._set("calendars", calendar_count)
        self._set("sync_summary", sync_summary)

    def commit_plan(self, cache, reminders, *, now, horizon, observed_at=None,
                    calendar_count, sync_summary, policy_version="1"):
        """Commit only a complete successful source read; cache=None retains it."""
        stamp, end = _timestamp(now), _timestamp(horizon)
        if not stamp < end <= stamp + HORIZON_DAYS * 86400:
            raise ControlEmailQueueError("Некорректный горизонт расписания писем.")
        if not isinstance(policy_version, str) or not policy_version or len(policy_version) > 64:
            raise ControlEmailQueueError("Некорректная версия правил писем.")
        cache_rows = None if cache is None else [_cache_row(resource) for resource in cache]
        if cache_rows is not None and len({(row[0], row[1], row[3]) for row in cache_rows}) != len(cache_rows):
            raise ControlEmailQueueError("Повторные ресурсы контрольного календаря.")
        plan = [_reminder_row(reminder) for reminder in reminders]
        if len({row[0] for row in plan}) != len(plan) or any(row[2] >= end for row in plan):
            raise ControlEmailQueueError("Повторное напоминание или время вне горизонта расписания.")
        # The planner already coalesces these; repeat it at the durability
        # boundary so a caller cannot accidentally send overlapping lead rules.
        by_slot = {}
        for row in plan:
            priority = _RULES.index(json.loads(row[3])["rule"])
            previous = by_slot.get(row[1])
            if previous is None or priority < previous[0]:
                by_slot[row[1]] = (priority, row)
        plan = [row for _, row in by_slot.values()]
        with self.connection:
            # Receipts live forever. Keep only the current plan's identities in
            # a temporary index; never load historical recipient/body payloads
            # just to reconcile this week's work.
            self.connection.execute("CREATE TEMP TABLE IF NOT EXISTS current_plan (stable_id TEXT PRIMARY KEY)")
            self.connection.execute("DELETE FROM current_plan")
            self.connection.executemany("INSERT INTO current_plan VALUES (?)", [(row[0],) for row in plan])
            if cache_rows is not None:
                self.connection.execute("DELETE FROM resources")
                self.connection.executemany("INSERT INTO resources VALUES (?,?,?,?,?,?)", cache_rows)
            for stable_id, slot_id, due, payload in plan:
                prior = self.connection.execute("SELECT * FROM reminders WHERE stable_id=?", (stable_id,)).fetchone()
                if prior is not None and (prior["state"] in _RECEIPTS
                        or (prior["state"] == "failed" and not prior["retryable"])):
                    continue
                state, retryable, retry_at = "pending", 1, None
                consumed = self.connection.execute(
                    "SELECT 1 FROM reminders WHERE slot_id=? AND state IN ('sending','accepted','uncertain') LIMIT 1",
                    (slot_id,),
                ).fetchone()
                if consumed is not None:
                    state, retryable = "cancelled", 0
                elif prior is not None:
                    if prior["state"] == "failed":
                        # Title or body edits are not a reason to skip backoff.
                        state, retryable, retry_at = "failed", 1, prior["retry_at"]
                    elif prior["state"] in {"expired", "cancelled"} and due < stamp:
                        state, retryable = prior["state"], 0
                self.connection.execute("""INSERT INTO reminders
                    (stable_id,slot_id,notify_at,payload,state,retryable,retry_at,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(stable_id) DO UPDATE SET
                    slot_id=excluded.slot_id,notify_at=excluded.notify_at,payload=excluded.payload,state=excluded.state,
                    retryable=excluded.retryable,retry_at=excluded.retry_at,updated_at=excluded.updated_at""",
                    (stable_id, slot_id, due, payload, state, retryable, retry_at, stamp, stamp))
            self.connection.execute("""UPDATE reminders SET state='cancelled',retryable=0,retry_at=NULL,updated_at=?
                WHERE (state='pending' OR (state='failed' AND retryable=1))
                AND NOT EXISTS (SELECT 1 FROM current_plan WHERE current_plan.stable_id=reminders.stable_id)""",
                (stamp,))
            self._sync_metadata(now, calendar_count, sync_summary, observed_at=observed_at)
            self._set("planned_until", end)
            self._set("policy_version", policy_version)
            self._expire(now)
            self.connection.execute("DROP TABLE current_plan")

    def mark_sync(self, now, calendar_count, sync_summary, *, observed_at=None):
        with self.connection:
            self._sync_metadata(now, calendar_count, sync_summary, observed_at=observed_at)
            self._expire(now)

    def activate(self, now):
        stamp = _timestamp(now)
        with self.connection:
            if self._get("activated_at") is None:
                self._set("activated_at", stamp)
            self._expire(now)

    def due(self, now, limit=50):
        stamp = _timestamp(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ControlEmailQueueError("Некорректный размер очереди писем.")
        activation = self._get("activated_at")
        if activation is None or not delivery_allowed(now):
            return []
        rows = self.connection.execute("""SELECT work.payload FROM reminders AS work
            WHERE (work.state='pending' OR (work.state='failed' AND work.retryable=1))
            AND work.notify_at>=? AND work.notify_at>=? AND work.notify_at<=?
            AND (work.retry_at IS NULL OR work.retry_at<=?)
            AND NOT EXISTS (SELECT 1 FROM reminders AS receipt WHERE receipt.slot_id=work.slot_id
                AND receipt.state IN ('sending','accepted','uncertain'))
            ORDER BY work.notify_at,work.stable_id LIMIT ?""",
            (activation, stamp - OVERDUE_GRACE_SECONDS, stamp, stamp, limit))
        return [json.loads(row["payload"]) for row in rows]

    def claim(self, stable_id, *, now):
        """Durable checkpoint immediately before SMTP DATA, after recipient setup."""
        stamp = _timestamp(now)
        activation = self._get("activated_at")
        observed = self._get("last_observation_at")
        if (not delivery_allowed(now) or activation is None or observed is None
                or not 0 <= stamp - observed <= SYNC_FRESH_SECONDS):
            return False
        with self.connection:
            updated = self.connection.execute("""UPDATE reminders AS work
                SET state='sending',retryable=0,retry_at=NULL,updated_at=?
                WHERE stable_id=? AND (state='pending' OR (state='failed' AND retryable=1))
                AND notify_at>=? AND notify_at>=? AND notify_at<=? AND (retry_at IS NULL OR retry_at<=?)
                AND NOT EXISTS (SELECT 1 FROM reminders AS receipt WHERE receipt.slot_id=work.slot_id
                    AND receipt.state IN ('sending','accepted','uncertain'))""",
                (stamp, stable_id, activation, stamp - OVERDUE_GRACE_SECONDS, stamp, stamp))
        return updated.rowcount == 1

    def complete(self, stable_id, state, *, now, retryable=False):
        stamp = _timestamp(now)
        if state not in {"accepted", "uncertain", "failed"}:
            raise ControlEmailQueueError("Некорректный результат отправки письма.")
        retryable = state == "failed" and bool(retryable)
        expected = ("sending",) if state == "accepted" else ("sending", "pending", "failed")
        placeholders = ",".join("?" for _ in expected)
        with self.connection:
            updated = self.connection.execute(
                "UPDATE reminders SET state=?,retryable=?,retry_at=?,updated_at=? "
                f"WHERE stable_id=? AND state IN ({placeholders})",
                (state, int(retryable), stamp + RETRY_DELAY_SECONDS if retryable else None,
                 stamp, stable_id, *expected))
            if updated.rowcount != 1:
                raise ControlEmailQueueError("Состояние письма изменилось; повторная отправка остановлена.")

    def summary(self, now):
        stamp = _timestamp(now)
        with self.connection:
            self._expire(now)
        activated, synced, observed = self._get("activated_at"), self._get("last_sync_at"), self._get("last_observation_at")
        counts = dict.fromkeys(_STATES, 0)
        counts.update({row["state"]: row["total"] for row in self.connection.execute(
            "SELECT state,COUNT(*) AS total FROM reminders GROUP BY state")})
        next_due = None
        if activated is not None:
            next_due = self.connection.execute("""SELECT MIN(MAX(work.notify_at,COALESCE(work.retry_at,work.notify_at)))
                FROM reminders AS work WHERE (work.state='pending' OR (work.state='failed' AND work.retryable=1))
                AND work.notify_at>=? AND NOT EXISTS (SELECT 1 FROM reminders AS receipt
                    WHERE receipt.slot_id=work.slot_id AND receipt.state IN ('sending','accepted','uncertain'))""",
                (activated,)).fetchone()[0]
        return {"activated_at": _iso(activated), "last_sync_at": _iso(synced), "last_observation_at": _iso(observed),
                "sync_recent": synced is not None and 0 <= stamp - synced <= SYNC_HEALTH_SECONDS,
                "sync_fresh_for_delivery": observed is not None and 0 <= stamp - observed <= SYNC_FRESH_SECONDS,
                "next_due_at": _iso(next_due), "planned_until": _iso(self._get("planned_until")),
                "policy_version": self._get("policy_version"), "calendars": self._get("calendars", 0),
                "resources": self.connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
                "counts": counts, "delivery_not_before": NOT_BEFORE.strftime("%H:%M"), "delivery_timezone": MOSCOW.key}


@contextmanager
def control_email_store(directory: Path, *, now=None):
    """Secure a separate queue without chmod of Render's managed mount root."""
    now = datetime.now(timezone.utc) if now is None else now
    _timestamp(now)
    root_fd = lock_fd = private_fd = None
    store = None
    try:
        try:
            directory = Path(directory)
            configured = os.environ.get("DIGEST_STATE_DIR")
            if configured and Path(configured).resolve() != directory.resolve():
                raise ControlEmailQueueError("Каталог расписания должен совпадать с DIGEST_STATE_DIR.")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            lock_fd = _private_open(".control-email.lock", root_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ControlEmailQueueError("Операция с расписанием писем уже выполняется.", retryable=True) from None
                raise
            _same_file(".control-email.lock", lock_fd, root_fd)
            try:
                os.mkdir("control-email", mode=0o700, dir_fd=root_fd)
                os.fsync(root_fd)
            except FileExistsError:
                pass
            private_fd = os.open("control-email", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=root_fd)
            if os.fstat(private_fd).st_uid != os.geteuid():
                raise OSError("private directory belongs to another user")
            os.fchmod(private_fd, 0o700)
            _same_file("control-email", private_fd, root_fd)
            store = ControlEmailStore(directory / "control-email", private_fd)
            _same_file("control-email", private_fd, root_fd)
            _same_file(".control-email.lock", lock_fd, root_fd)
            store.recover(now)
        except ControlEmailQueueError:
            raise
        except Exception:
            raise ControlEmailQueueError("Не удалось открыть защищённое расписание контрольных писем.") from None
        try:
            yield store
        except sqlite3.Error:
            raise ControlEmailQueueError("Не удалось сохранить состояние контрольных писем.") from None
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
