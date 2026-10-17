"""Supervise round-the-clock push cycles without importing calendar clients.

One bounded subprocess refreshes calendar changes, reconciles its durable queue
and submits due notifications. This loop only reads safe queue counters. It does
not interpret a successful subprocess exit as a confirmed phone delivery.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from .push_timing import MOSCOW, normalize_notification_time


REFRESH_DELAY = timedelta(minutes=5)
RETRY_DELAY = timedelta(seconds=60)
OVERDUE_DELAY = timedelta(seconds=30)
MAX_JOB_SECONDS = 15 * 60
HEARTBEAT_NAME = "push-schedule-status.json"
ACTIVITIES = {"idle", "syncing", "stopped", "error"}


class PushSchedulerError(Exception):
    """A fixed diagnostic without account or event information."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise PushSchedulerError("Push-планировщику требуется время с часовым поясом.")
    return now.astimezone(MOSCOW)


def next_run(now: datetime, summary: dict) -> datetime:
    """Refresh every five minutes or earlier for a pending notification.

    An overdue retry remains bounded even if a cycle cannot consume a queued
    item. The durable queue, not this loop, controls retry eligibility.
    """
    now = _local(now)
    refresh_at = now + REFRESH_DELAY
    value = summary.get("next_due_at")
    if value is None:
        return refresh_at
    try:
        due = normalize_notification_time(_local(datetime.fromisoformat(value)))
    except (ValueError, TypeError, PushSchedulerError):
        raise PushSchedulerError("Не удалось прочитать время следующего уведомления.") from None
    if due <= now:
        due = normalize_notification_time(now + OVERDUE_DELAY)
    return min(refresh_at, due)


@contextmanager
def scheduler_lock(directory: Path):
    """Keep the same inode for a process-lifetime singleton lock."""
    descriptor = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(directory / ".push-scheduler.lock", os.O_RDWR | os.O_CREAT
                             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("not a private regular file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise PushSchedulerError("Push-планировщик уже работает или каталог состояния недоступен.") from None
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_heartbeat(directory: Path, *, activity: str, now: datetime):
    if activity not in ACTIVITIES:
        raise PushSchedulerError("Некорректное состояние push-планировщика.")
    payload = {"updated_at": _local(now).isoformat(), "timezone": "Europe/Moscow", "activity": activity}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".push-schedule-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
        os.replace(temporary, directory / HEARTBEAT_NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def push_scheduler_status(directory: Path, *, now: datetime | None = None) -> dict:
    result = {"heartbeat_at": None, "heartbeat_recent": False,
              "activity": "unknown", "timezone": "Europe/Moscow"}
    try:
        with (Path(directory) / HEARTBEAT_NAME).open("r", encoding="utf-8") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return result
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return result
        moment = _local(datetime.fromisoformat(payload["updated_at"]))
        activity = payload["activity"]
        if not isinstance(activity, str) or activity not in ACTIVITIES:
            return result
        age = (_local(now or _now()) - moment).total_seconds()
        result.update(heartbeat_at=moment.isoformat(), activity=activity,
                      heartbeat_recent=activity != "stopped" and 0 <= age <= 120)
    except (OSError, ValueError, TypeError, KeyError, PushSchedulerError):
        pass
    return result


def _queue_summary(directory: Path, now: datetime) -> dict:
    from .push_queue import schedule_store

    with schedule_store(directory, now=now) as store:
        return store.summary(now)


def _stop_job(process):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # SIGKILL has already been issued; the supervisor also kills the whole
        # group on shutdown, including a child whose scheduler has exited.
        pass


def run_job(*, directory: Path, stop: threading.Event, heartbeat,
            timeout: float = MAX_JOB_SECONDS) -> int:
    """Inherit the supervised process group so shutdown includes this job."""
    if stop.is_set() or timeout <= 0:
        return 1
    arguments = [str(Path(sys.executable).with_name("calendar-digest")), "push-cycle",
                 "--directory", str(directory)]
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + min(timeout, MAX_JOB_SECONDS)
    next_pulse = 0.0
    try:
        while True:
            now = time.monotonic()
            if now >= next_pulse:
                heartbeat()
                next_pulse = now + 15
            code = process.poll()
            if code is not None:
                return code
            if stop.is_set() or now >= deadline:
                return 1
            stop.wait(min(0.2, max(0.0, deadline - now)))
    finally:
        _stop_job(process)


def serve(directory: Path, *, stop: threading.Event, clock=_now):
    scheduled_at = None
    activity = "idle"
    while not stop.is_set():
        now = _local(clock())
        write_heartbeat(directory, activity=activity, now=now)
        if scheduled_at is None or now >= scheduled_at:
            try:
                code = run_job(directory=directory, stop=stop,
                               heartbeat=lambda: write_heartbeat(directory, activity="syncing", now=clock()))
                if stop.is_set():
                    break
                if code != 0:
                    raise PushSchedulerError("Цикл уведомлений не завершён.")
                completed_at = _local(clock())
                scheduled_at = next_run(completed_at, _queue_summary(directory, completed_at))
                activity = "idle"
            except Exception:
                if stop.is_set():
                    break
                scheduled_at = _local(clock()) + RETRY_DELAY
                activity = "error"
                print("Цикл push-уведомлений не завершён. Повтор проверки через минуту.",
                      file=sys.stderr, flush=True)
            write_heartbeat(directory, activity=activity, now=clock())
        if not stop.is_set():
            wait = max(0.0, (scheduled_at - _local(clock())).total_seconds())
            stop.wait(min(30.0, wait))


def main() -> int:
    from .runtime import push_schedule_enabled

    try:
        if not push_schedule_enabled():
            print("Push-расписание выключено.", flush=True)
            return 0
        directory = Path(os.environ.get("DIGEST_STATE_DIR", ".local"))
        stop = threading.Event()
        old_handlers = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
        try:
            with scheduler_lock(directory):
                try:
                    serve(directory, stop=stop)
                finally:
                    write_heartbeat(directory, activity="stopped", now=_now())
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
        return 0
    except Exception:
        print("Push-планировщик остановлен. Проверьте настройки и каталог состояния.",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
