"""Pushover transport with mandatory end-to-end encryption and no retries.

A successful response confirms server acceptance, not display on the iPhone.
The notification submission boundary is explicit so callers can durably record
an attempt before a response can be lost. Device validation does not send a push.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
import gzip
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import ssl
from typing import Callable
from urllib.parse import urlencode, urlsplit


_HOST = "api.pushover.net"
_VALIDATE_PATH = "/1/users/validate.json"
_MESSAGE_PATH = "/1/messages.json"
_TIMEOUT = 30
_MAX_RESPONSE = 32 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9]{30}\Z")
_DEVICE = re.compile(r"[A-Za-z0-9_-]{1,25}\Z")
_KEY = re.compile(r"[0-9a-fA-F]{64}\Z")


class PushError(Exception):
    """A safe diagnostic that does not include credentials or server replies."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class PushUncertain(PushError):
    """The provider may have accepted the notification; do not resend it."""


@dataclass(frozen=True)
class PushoverSettings:
    app_token: str = field(repr=False)
    user_key: str = field(repr=False)
    device: str = field(repr=False)
    encryption_key: str = field(repr=False)

    def __post_init__(self):
        for value, name in ((self.app_token, "PUSHOVER_APP_TOKEN"),
                            (self.user_key, "PUSHOVER_USER_KEY")):
            if not isinstance(value, str) or not _TOKEN.fullmatch(value):
                raise PushError(f"{name} должен содержать ключ Pushover из 30 букв и цифр.")
        if not isinstance(self.device, str) or not _DEVICE.fullmatch(self.device):
            raise PushError("PUSHOVER_DEVICE должен содержать имя одного устройства: до 25 букв, цифр, дефисов или подчёркиваний.")
        if not isinstance(self.encryption_key, str) or not _KEY.fullmatch(self.encryption_key):
            raise PushError("PUSHOVER_ENCRYPTION_KEY должен содержать ключ шифрования из 64 шестнадцатеричных символов.")

    @classmethod
    def from_environment(cls) -> "PushoverSettings":
        names = ("PUSHOVER_APP_TOKEN", "PUSHOVER_USER_KEY", "PUSHOVER_DEVICE", "PUSHOVER_ENCRYPTION_KEY")
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise PushError("Добавьте защищённые переменные среды: " + ", ".join(missing))
        return cls(*(os.environ[name] for name in names))


def _text(value: str, *, limit: int, multiline: bool = False):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PushError("Текст уведомления пуст или превышает допустимую длину.")
    allowed = "\n\t" if multiline else ""
    if any((ord(character) < 32 and character not in allowed) or ord(character) == 127
           for character in value):
        raise PushError("Текст уведомления содержит недопустимые управляющие символы.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise PushError("Не удалось прочитать текст уведомления в UTF-8.") from None


def _encrypt_field(value: str, key: bytes) -> str:
    # Pushover's published E2EE format: gzip, AES-256-CBC/PKCS7, then
    # HMAC-SHA256(IV || ciphertext) with the same 256-bit key. No plaintext
    # fallback is permitted if the cryptography package is unavailable.
    try:
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise PushError("Для защищённых уведомлений необходимо установить зависимости проекта.") from None
    iv = secrets.token_bytes(16)
    compressed = gzip.compress(value.encode("utf-8"), compresslevel=9, mtime=0)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(compressed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    authenticated = iv + ciphertext
    mac = hmac.new(key, authenticated, hashlib.sha256).digest()
    return base64.b64encode(authenticated + mac).decode("ascii")


def _payload(settings: PushoverSettings, *, title: str, message: str, url: str | None) -> bytes:
    _text(title, limit=250)
    _text(message, limit=1024, multiline=True)
    if url is not None:
        _text(url, limit=512)
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                     and not parsed.password and parsed.port in {None, 443}
                     and not any(character.isspace() for character in url))
        except ValueError:
            valid = False
        if not valid:
            raise PushError("Ссылка уведомления должна быть корректным HTTPS-адресом без данных входа.")
    key = bytes.fromhex(settings.encryption_key)
    try:
        values = {"token": settings.app_token, "user": settings.user_key, "device": settings.device,
                  "encrypted": "1", "priority": "0", "title": _encrypt_field(title, key),
                  "message": _encrypt_field(message, key)}
        if url is not None:
            values["url"] = _encrypt_field(url, key)
        return urlencode(values).encode("ascii")
    except PushError:
        raise
    except Exception:
        raise PushError("Не удалось зашифровать уведомление; отправка не выполнялась.") from None


def _close(connection):
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass


def _connect():
    connection = None
    try:
        connection = http.client.HTTPSConnection(_HOST, 443, timeout=_TIMEOUT,
                                                context=ssl.create_default_context())
        connection.connect()
        return connection
    except ssl.SSLError:
        _close(connection)
        raise PushError("Не удалось подтвердить защищённое соединение с Pushover.") from None
    except (OSError, http.client.HTTPException):
        _close(connection)
        raise PushError("Не удалось подключиться к Pushover до отправки уведомления.", retryable=True) from None


def _invalid_reply(*, submitting: bool):
    if submitting:
        raise PushUncertain("Результат отправки уведомления неизвестен; автоматический повтор запрещён.") from None
    raise PushError("Pushover не подтвердил проверку устройства.", retryable=True) from None


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _post(connection, path: str, body: bytes, *, submitting: bool) -> dict:
    try:
        connection.request("POST", path, body=body,
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Accept": "application/json"})
        response = connection.getresponse()
        status = response.status
        if 400 <= status < 500:
            raise PushError("Pushover отклонил запрос. Проверьте ключи, устройство и доступный лимит уведомлений.")
        if 300 <= status < 400:
            raise PushError("Pushover вернул перенаправление; переход на другой адрес запрещён.")
        if status != 200:
            _invalid_reply(submitting=submitting)
        raw = response.read(_MAX_RESPONSE + 1)
        if len(raw) > _MAX_RESPONSE:
            _invalid_reply(submitting=submitting)
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            _invalid_reply(submitting=submitting)
        if not isinstance(data, dict) or type(data.get("status")) is not int:
            _invalid_reply(submitting=submitting)
        if data["status"] == 0:
            raise PushError("Pushover отклонил запрос. Проверьте ключи, устройство и доступный лимит уведомлений.")
        if data["status"] != 1:
            _invalid_reply(submitting=submitting)
        return data
    except PushError:
        raise
    except (OSError, http.client.HTTPException):
        _invalid_reply(submitting=submitting)


def _validate_device(connection, settings: PushoverSettings):
    body = urlencode({"token": settings.app_token, "user": settings.user_key,
                      "device": settings.device}).encode("ascii")
    data = _post(connection, _VALIDATE_PATH, body, submitting=False)
    devices = data.get("devices")
    if (not isinstance(devices, list) or not all(isinstance(device, str) for device in devices)
            or settings.device not in devices):
        raise PushError("Pushover не подтвердил выбранное устройство. Проверьте личный User Key и имя iPhone.")


def probe_pushover(settings: PushoverSettings) -> dict:
    """Validate the personal user/device without sending any notification.

    This confirms registration, not that the iPhone has the matching E2EE key.
    """
    connection = _connect()
    try:
        _validate_device(connection, settings)
    finally:
        _close(connection)
    return {"pushover": "ok", "device_valid": True, "sent": False}


def send_notification(settings: PushoverSettings, *, title: str, message: str,
                      url: str | None = None, before_send: Callable[[], None]) -> dict:
    """Send one encrypted notification after the caller's durable checkpoint.

    Pushover falls back to all devices when a supplied device becomes invalid.
    Validation reduces that risk but cannot eliminate a concurrent device change;
    settings must contain a personal User Key, never a delivery-group key.
    """
    body = _payload(settings, title=title, message=message, url=url)
    connection = _connect()
    try:
        _validate_device(connection, settings)
        # Any exception raised by the caller prevents submission. Keep it intact
        # so the outbox can distinguish its own durable-state failure.
        before_send()
        _post(connection, _MESSAGE_PATH, body, submitting=True)
    finally:
        _close(connection)
    return {"provider": "pushover", "state": "accepted", "encrypted": True}
