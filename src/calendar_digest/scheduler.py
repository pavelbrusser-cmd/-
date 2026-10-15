"""A lightweight Moscow-time scheduler; calendar work lives in subprocesses.

The process owns one advisory lock for its lifetime. The durable outbox, rather
than this loop's memory, decides whether a delivery can be retried after restart.
"""

from contextlib import contextmanager
from datetime import date, datetime, time as day_time, timedelta, timezone
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
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
PREPARE_TIME = day_time(16, 55)
SEND_TIME = day_time(17)
CUTOFF_TIME = day_time(17, 30)
RETRY_DELAY = timedelta(minutes=5)
BUSY_RETRY_DELAY = timedelta(seconds=30)
TEMPORARY_BUSY_EXIT_CODE = 75
MAX_JOB_SECONDS = 15 * 60
HEARTBEAT_NAME = "schedule-status.json"


class SchedulerError(Exception):
    """A fixed diagnostic without calendar or account data."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _local(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise SchedulerError("Планировщику требуется время с часовым поясом.")
    return now.astimezone(MOSCOW)


def digest_day(now: datetime) -> date:
    return _local(now).date() + timedelta(days=1)


def next_action(now: datetime, delivery: dict | None, *, retry_after: datetime | None = None) -> str | None:
    """Choose only today's next-day job; never backfill or replay uncertainty."""
    local = _local(now)
    if not PREPARE_TIME <= local.time() < CUTOFF_TIME:
        return None
    if retry_after is not None and now < retry_after:
        return None
    if delivery is None:
        return "prepare-delivery"
    if delivery.get("date") != digest_day(now).isoformat() or delivery.get("kind") != "daily":
        return None
    state = delivery.get("state")
    if state in {"accepted", "uncertain", "submitting"}:
        return None
    if state in {"preparing", "failed"}:
        return "prepare-delivery" if delivery.get("retryable") is True else None
    if state == "ready" and local.time() >= SEND_TIME:
        return "send-ready"
    return None


@contextmanager
def scheduler_lock(directory: Path):
    """Do not unlink the lock file: another process could hold its inode."""
    descriptor = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(directory / ".scheduler.lock",
                             os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("not regular")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise SchedulerError("Планировщик уже работает или каталог состояния недоступен.") from None
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def write_heartbeat(directory: Path, *, activity: str, now: datetime):
    if activity not in {"idle", "preparing", "sending", "stopped"}:
        raise SchedulerError("Некорректное состояние планировщика.")
    payload = {
        "updated_at": _local(now).isoformat(),
        "timezone": "Europe/Moscow",
        "activity": activity,
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".schedule-", delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
        os.replace(temporary, directory / HEARTBEAT_NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def scheduler_status(directory: Path, *, now: datetime | None = None) -> dict:
    """Report recent activity, not a guarantee that a future email will arrive."""
    result = {"heartbeat_at": None, "heartbeat_recent": False,
              "activity": "unknown", "timezone": "Europe/Moscow"}
    try:
        with (Path(directory) / HEARTBEAT_NAME).open("r", encoding="utf-8") as stream:
            raw = stream.read(8193)
        if len(raw) > 8192:
            return result
        payload = json.loads(raw)
        moment = datetime.fromisoformat(payload["updated_at"])
        local = _local(moment)
        activity = payload["activity"]
        if activity not in {"idle", "preparing", "sending", "stopped"}:
            return result
        age = (_local(now or _now()) - local).total_seconds()
        result.update(heartbeat_at=local.isoformat(), activity=activity,
                      heartbeat_recent=activity != "stopped" and 0 <= age <= 120)
    except (OSError, ValueError, TypeError, KeyError, SchedulerError):
        pass
    return result


def _daily_status(directory: Path, day: date) -> dict | None:
    # This module, including its status API, deliberately has only lightweight
    # imports. Preparing a preview is imported by the separate CLI process.
    from .delivery import delivery_status

    report = delivery_status(directory, day=day)
    rows = [row for row in report["deliveries"]
            if row.get("date") == day.isoformat() and row.get("kind") == "daily"]
    if len(rows) > 1:
        raise SchedulerError("Не удалось однозначно определить состояние отправки.")
    return rows[0] if rows else None


def _stop_job(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=2)


def run_job(command: str, *, day: date, directory: Path, stop: threading.Event,
            heartbeat, timeout: float = MAX_JOB_SECONDS) -> int:
    """Run one bounded job, inheriting the scheduler's supervised process group."""
    if command not in {"prepare-delivery", "send-ready"}:
        raise SchedulerError("Неизвестная задача планировщика.")
    if stop.is_set() or timeout <= 0:
        return 1
    arguments = [str(Path(sys.executable).with_name("calendar-digest")), command,
                 "--date", day.isoformat(), "--directory", str(directory), "--kind", "daily"]
    # No new session here: killing the scheduler's group must also stop its job.
    process = subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + min(timeout, MAX_JOB_SECONDS)
    next_pulse = 0.0
    try:
        while True:
            now = time.monotonic()
            if now >= next_pulse:
                heartbeat()
                next_pulse = now + 15
            returncode = process.poll()
            if returncode is not None:
                return returncode
            if stop.is_set() or now >= deadline:
                return 1
            stop.wait(min(0.2, max(0.0, deadline - now)))
    finally:
        _stop_job(process)


def _idle_delay(now: datetime) -> float:
    local = _local(now)
    boundaries = [datetime.combine(local.date(), moment, tzinfo=MOSCOW)
                  for moment in (PREPARE_TIME, SEND_TIME, CUTOFF_TIME)]
    upcoming = [(boundary - local).total_seconds() for boundary in boundaries if boundary > local]
    return min([30.0, *upcoming])


def serve(directory: Path, *, stop: threading.Event, clock=_now):
    retry_after = None
    retry_day = None
    while not stop.is_set():
        now = clock()
        local = _local(now)
        day = digest_day(now)
        if retry_day != day:
            retry_after = None
            retry_day = day
        write_heartbeat(directory, activity="idle", now=now)
        if PREPARE_TIME <= local.time() < CUTOFF_TIME and (retry_after is None or now >= retry_after):
            try:
                current = _daily_status(directory, day)
                command = next_action(now, current, retry_after=retry_after)
            except Exception:
                print("Не удалось прочитать состояние отправки. Повтор проверки через пять минут.",
                      file=sys.stderr, flush=True)
                retry_after = now + RETRY_DELAY
                command = None
            if command is not None:
                activity = "preparing" if command == "prepare-delivery" else "sending"
                cutoff = datetime.combine(local.date(), CUTOFF_TIME, tzinfo=MOSCOW)
                remaining = max(0.0, (cutoff - local).total_seconds())
                print("Готовим ежедневный дайджест." if activity == "preparing"
                      else "Передаём ежедневный дайджест почтовому серверу.", flush=True)
                try:
                    code = run_job(command, day=day, directory=directory, stop=stop,
                                   timeout=min(MAX_JOB_SECONDS, remaining),
                                   heartbeat=lambda: write_heartbeat(directory, activity=activity, now=clock()))
                except OSError:
                    code = 1
                if code == TEMPORARY_BUSY_EXIT_CODE:
                    # Another calendar job may be holding the shared lock.
                    # Poll promptly so a short push sync near 16:55 does not
                    # postpone preparation by the full network-error interval.
                    retry_after = clock() + BUSY_RETRY_DELAY
                    print("Календари заняты другой задачей. Повтор проверки через 30 секунд.",
                          file=sys.stderr, flush=True)
                elif code != 0:
                    retry_after = clock() + RETRY_DELAY
                    print("Задача завершилась без подтверждения успеха. Перед повтором проверим журнал отправок.",
                          file=sys.stderr, flush=True)
                else:
                    # Read the durable state again, including after successful
                    # preparation: a restart must make the same decision.
                    retry_after = None
                    continue
        stop.wait(_idle_delay(clock()))


def main() -> int:
    from .runtime import schedule_enabled, validate_smtp_settings

    try:
        if not schedule_enabled():
            print("Расписание выключено.", flush=True)
            return 0
        validate_smtp_settings()
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
        print("Планировщик остановлен. Проверьте настройки, журнал отправок и каталог состояния.",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
