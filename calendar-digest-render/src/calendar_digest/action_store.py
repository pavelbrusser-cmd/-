"""Private action links and durable, single-claim operation results.

Reading a link never consumes it. Expiry prevents viewing or claiming an action;
a validly signed action already running may still record its outcome after
expiry, so a slow calendar write does not lose its result.
"""

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from base64 import urlsafe_b64encode


class StoreError(Exception):
    """Safe storage errors contain no links, private paths or event contents."""


_TOKEN = re.compile(r"([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})")
_ACTION = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TARGET_FIELDS = frozenset({
    "source_id", "calendar_id", "uid", "expected_start", "expected_end",
    "recurrence_id", "expected_event_status", "expected_participation",
})
_FORBIDDEN_RESULT_KEYS = frozenset({
    "password", "app_password", "credentials", "credential", "secret",
    "signing_key", "token", "access_token", "refresh_token", "authorization",
    "username", "account_email", "ics", "calendar_data", "raw_ics",
})


def _json(value: dict, *, target=False) -> str:
    if not isinstance(value, dict):
        raise StoreError("Некорректные данные действия.")
    if target:
        if set(value) != _TARGET_FIELDS:
            raise StoreError("Некорректные поля события для действия.")
        for key, item in value.items():
            if key == "recurrence_id" and item is None:
                continue
            if not isinstance(item, str) or not item.strip():
                raise StoreError("Некорректные данные события для действия.")

    def validate(item, depth=0):
        if depth > 16:
            raise StoreError("Данные действия слишком сложны.")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or (
                    not target and key.casefold().replace("-", "_") in _FORBIDDEN_RESULT_KEYS
                ):
                    raise StoreError("Недопустимые поля данных действия.")
                validate(child, depth + 1)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate(child, depth + 1)
        elif isinstance(item, str) and (
            "BEGIN:VCALENDAR" in item.upper() or "BEGIN:VEVENT" in item.upper()
        ):
            raise StoreError("Сырые календарные данные нельзя сохранять в действиях.")

    validate(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise StoreError("Данные действия превышают допустимый размер.")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise StoreError("Некорректные данные действия.") from None


class ActionStore:
    def __init__(self, path: Path, signing_key: str):
        if not isinstance(signing_key, str) or len(signing_key) < 32:
            raise StoreError("Для ссылок действий нужен ключ не короче 32 символов.")
        try:
            self._key = signing_key.encode("utf-8")
            self.path = Path(path).expanduser().absolute()
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(
                self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise StoreError("Хранилище действий должно быть обычным файлом.")
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except (OSError, ValueError, TypeError, UnicodeError):
            raise StoreError("Не удалось открыть защищённое хранилище действий.") from None
        with self._connection() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY,
                    target_json TEXT NOT NULL,
                    action TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('ready', 'running', 'done', 'failed', 'uncertain')
                    ),
                    result_json TEXT
                )
            """)

    @contextmanager
    def _connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            yield connection
        except sqlite3.OperationalError:
            raise StoreError("Хранилище действий занято или недоступно; повторите позже.") from None
        except sqlite3.Error:
            raise StoreError("Не удалось прочитать или сохранить состояние действия.") from None
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

    def _signature(self, identifier: str) -> str:
        digest = hmac.new(self._key, identifier.encode("ascii"), hashlib.sha256).digest()
        return urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _identifier(self, token: str) -> str:
        match = _TOKEN.fullmatch(token) if isinstance(token, str) else None
        if match is None:
            raise StoreError("Ссылка действия недействительна.")
        identifier, signature = match.groups()
        if not hmac.compare_digest(self._signature(identifier), signature):
            raise StoreError("Ссылка действия недействительна.")
        return identifier

    @staticmethod
    def _row(connection, identifier: str, *, check_expiry=True):
        row = connection.execute("SELECT * FROM actions WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise StoreError("Ссылка действия недействительна.")
        if check_expiry and row["expires_at"] <= time.time():
            raise StoreError("Срок действия ссылки истёк.")
        return row

    def issue(self, target: dict, action: str, ttl_seconds: int = 48 * 3600) -> str:
        target_json = _json(target, target=True)
        if not isinstance(action, str) or _ACTION.fullmatch(action) is None:
            raise StoreError("Некорректное действие.")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 2**31:
            raise StoreError("Некорректный срок действия ссылки.")
        identifier = secrets.token_urlsafe(32)
        created = time.time()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO actions (id,target_json,action,created_at,expires_at,state) "
                "VALUES (?,?,?,?,?,'ready')",
                (identifier, target_json, action, created, created + ttl_seconds),
            )
        return identifier + "." + self._signature(identifier)

    def read(self, token: str) -> dict:
        identifier = self._identifier(token)
        with self._connection() as connection:
            row = self._row(connection, identifier)
            try:
                target = json.loads(row["target_json"])
                result = json.loads(row["result_json"]) if row["result_json"] is not None else None
                _json(target, target=True)
                if result is not None:
                    _json(result)
            except (TypeError, ValueError, RecursionError):
                raise StoreError("Сохранённое состояние действия повреждено.") from None
            return {
                "id": row["id"], "target": target, "action": row["action"],
                "created_at": row["created_at"], "expires_at": row["expires_at"],
                "state": row["state"], "result": result,
            }

    def claim(self, token: str) -> bool:
        identifier = self._identifier(token)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._row(connection, identifier)
            changed = connection.execute(
                "UPDATE actions SET state='running' WHERE id=? AND state='ready'", (identifier,)
            ).rowcount
            connection.commit()
            return changed == 1

    def complete(self, token: str, result: dict):
        self._finish(token, result, state="done")

    def fail(self, token: str, result: dict, *, uncertain=False):
        self._finish(token, result, state="uncertain" if uncertain else "failed")

    def _finish(self, token: str, result: dict, *, state: str):
        identifier = self._identifier(token)
        result_json = _json(result)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Expiry controls starting an operation, not recording its outcome.
            row = self._row(connection, identifier, check_expiry=False)
            if row["state"] == state:
                connection.commit()
                return
            if row["state"] != "running":
                raise StoreError("Действие уже завершено или не было начато.")
            connection.execute(
                "UPDATE actions SET state=?, result_json=? WHERE id=? AND state='running'",
                (state, result_json, identifier),
            )
            connection.commit()
