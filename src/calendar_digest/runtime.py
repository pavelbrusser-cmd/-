"""Supervise the web service and its optional, separate scheduling process.

Neither importing this module nor running with the schedule disabled reads SMTP
credentials or starts a mail job. Process groups let the supervisor stop a
calendar collection even when the scheduling process itself has died.
"""

import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Mapping


class RuntimeConfigurationError(Exception):
    """A configuration error that does not contain environment values."""


def schedule_enabled(environment: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if environment is None else environment
    value = environment.get("DIGEST_SCHEDULE_ENABLED", "false")
    if value not in {"true", "false"}:
        raise RuntimeConfigurationError("DIGEST_SCHEDULE_ENABLED должен быть true или false.")
    return value == "true"


def service_port(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    value = environment.get("PORT", "10000")
    if not isinstance(value, str) or not 1 <= len(value) <= 5 or not value.isascii() or not value.isdecimal():
        raise RuntimeConfigurationError("PORT должен быть целым числом от 1 до 65535.")
    port = int(value)
    if not 1 <= port <= 65535:
        raise RuntimeConfigurationError("PORT должен быть целым числом от 1 до 65535.")
    return port


def validate_smtp_settings():
    # MailSettings has no network side effects. Its module keeps rendering and
    # calendar imports inside the preparation functions to remain lightweight.
    from .smtp_delivery import MailSettings

    MailSettings.from_environment()


def service_commands(root: Path, *, port: int, enabled: bool) -> list[list[str]]:
    commands = [[
        str(root / ".venv/bin/gunicorn"), "calendar_digest.action_web:create_app()",
        "--bind", f"0.0.0.0:{port}", "--workers", "1", "--threads", "4",
        "--timeout", "90", "--access-logfile", "/dev/null", "--error-logfile", "-",
    ]]
    if enabled:
        commands.append([sys.executable, "-m", "calendar_digest.scheduler"])
    return commands


def _signal_group(process, signum: int):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def stop_children(children, *, grace: float = 10.0):
    """Bounded shutdown, including descendants of an already dead leader."""
    for child in children:
        _signal_group(child, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while any(child.poll() is None for child in children) and time.monotonic() < deadline:
        time.sleep(0.1)
    # Always address the original groups: poll() only tells us about the leader,
    # and a killed scheduler may have left its collection process running.
    for child in children:
        _signal_group(child, signal.SIGKILL)
    for child in children:
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def supervise(commands: list[list[str]], *, root: Path, stop: threading.Event) -> int:
    children = []
    try:
        for command in commands:
            if stop.is_set():
                return 0
            children.append(subprocess.Popen(command, cwd=root, start_new_session=True))
        while not stop.wait(0.2):
            if any(child.poll() is not None for child in children):
                print("Процесс сервиса остановился. Завершаем остальные процессы для перезапуска.",
                      file=sys.stderr, flush=True)
                return 1
        return 0
    except OSError:
        print("Не удалось запустить процессы сервиса.", file=sys.stderr, flush=True)
        return 1
    finally:
        stop_children(children)


def main() -> int:
    try:
        enabled = schedule_enabled()
        port = service_port()
        if enabled:
            validate_smtp_settings()
    except Exception as error:
        # Configuration exceptions from the SMTP layer are safe, but unexpected
        # third-party exceptions must not print environment values or credentials.
        if isinstance(error, RuntimeConfigurationError):
            print(str(error), file=sys.stderr, flush=True)
        else:
            print("Проверьте защищённые настройки SMTP перед включением расписания.",
                  file=sys.stderr, flush=True)
        return 1
    stop = threading.Event()
    old_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        root = Path(__file__).resolve().parents[2]
        print("Расписание включено." if enabled else "Расписание выключено.", flush=True)
        return supervise(service_commands(root, port=port, enabled=enabled), root=root, stop=stop)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
