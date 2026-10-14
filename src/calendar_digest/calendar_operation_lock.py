"""Nonblocking, cross-process gate for memory-intensive calendar operations."""

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat


class CalendarLockError(Exception):
    """Safe lock diagnostics that never contain filesystem paths."""


class CalendarBusy(CalendarLockError):
    """Another collector or web action currently owns the calendar gate."""


@contextmanager
def calendar_operation_lock(directory: Path):
    """Hold one shared state-directory gate, or fail immediately.

    The lock file is persistent: removing it would allow different processes to
    acquire different inodes. A process exit releases flock automatically. The
    nonblocking attempt also prevents Gunicorn threads from waiting on a long
    calendar collection and exhausting the available worker pool.
    """
    directory_fd = lock_fd = None
    acquired = False
    try:
        try:
            directory = Path(directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory_fd = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            os.fchmod(directory_fd, 0o700)
            lock_fd = os.open(
                '.calendar-operation.lock',
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=directory_fd,
            )
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CalendarLockError('Не удалось открыть защищённую блокировку календарей.')
            os.fchmod(lock_fd, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise CalendarBusy(
                        'Календари сейчас обновляются. Повторите попытку немного позже.'
                    ) from None
                raise
            acquired = True
        except CalendarLockError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError):
            raise CalendarLockError('Не удалось открыть защищённую блокировку календарей.') from None
        yield
    finally:
        # Closing is sufficient to release flock, even after unexpected work
        # failures. Cleanup failures must not replace the original diagnostic.
        if acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        for descriptor in (lock_fd, directory_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
