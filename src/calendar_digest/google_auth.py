"""Google read-only offline OAuth, with credentials encrypted on private disk.

The calendar source stays disabled until its owner has completed OAuth and the
primary calendar has proved the expected account identity. No Google passwords
are collected, and no access/refresh token is stored in a browser cookie.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import fcntl
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import stat
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken


SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALLBACK_PATH = "/settings/google/callback"
_MAX_BYTES = 64 * 1024
_FLOW_TTL = 600
_EMAIL = re.compile(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+\Z")


class GoogleAuthError(Exception):
    """Only safe diagnostics: never include tokens or raw server responses."""


def _secret(value, name, limit=8192):
    if (not isinstance(value, str) or not value or len(value) > limit
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                   for character in value)):
        raise GoogleAuthError(f"Проверьте защищённую настройку {name}.")
    return value


def _email(value):
    if not isinstance(value, str) or len(value) > 254 or not _EMAIL.fullmatch(value):
        raise GoogleAuthError("Укажите адрес ожидаемого аккаунта Google.")
    return value.casefold()


def _configuration(client_id, client_secret, expected_email):
    _secret(client_id, "GOOGLE_CLIENT_ID", 512)
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.apps\.googleusercontent\.com", client_id):
        raise GoogleAuthError("Укажите OAuth Client ID веб-приложения Google.")
    return {"client_id": client_id,
            "client_secret": _secret(client_secret, "GOOGLE_CLIENT_SECRET", 1024),
            "expected_email": _email(expected_email)}


def _expected_account(value):
    expected = _email(value)
    configured = os.environ.get("GOOGLE_CALENDAR_EMAIL")
    if configured and _email(configured) != expected:
        raise GoogleAuthError("Подключение должно относиться к аккаунту, указанному в GOOGLE_CALENDAR_EMAIL.")
    return expected


def _request_error(host, status, raw, *, refresh=False):
    """Map only known provider codes to static, safe owner diagnostics.

    Google descriptions, messages, links and metadata can contain credentials
    or private identifiers. Never include them, or unknown codes, in an error.
    """
    try:
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
    except (ValueError, UnicodeError):
        error = None
    if host == "oauth2.googleapis.com":
        if status == 429 or 500 <= status <= 599:
            code, message = "GOOGLE_OAUTH_RETRY", "Google временно не выдаёт разрешение. Начните подключение заново позже."
        elif error == "invalid_client":
            code, message = "GOOGLE_OAUTH_INVALID_CLIENT", "Google отклонил OAuth-клиент. Проверьте, что GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET принадлежат одному действующему веб-клиенту."
        elif error == "invalid_grant":
            if refresh:
                code, message = "GOOGLE_OAUTH_RECONNECT", "Google отклонил сохранённое разрешение. Подключите аккаунт заново через настройки сервиса."
            else:
                code, message = "GOOGLE_OAUTH_INVALID_GRANT", "Google отклонил код входа. Начните подключение заново через кнопку «Подключить через Google»; прежний код повторно не используется."
        elif error == "redirect_uri_mismatch":
            code, message = "GOOGLE_OAUTH_REDIRECT", "Google отклонил адрес возврата. Проверьте Authorized redirect URIs у OAuth-клиента: адрес должен совпадать с указанным на странице подключения сервиса."
        elif error == "invalid_scope":
            code, message = "GOOGLE_OAUTH_SCOPE", "Google отклонил запрошенное разрешение на чтение календарей. Проверьте настройки OAuth-приложения."
        elif error in ("access_denied", "unauthorized_client"):
            code, message = "GOOGLE_OAUTH_DENIED", "Google запретил этому OAuth-клиенту завершить вход. Проверьте доступ аккаунта к приложению и тип клиента «Веб-приложение»."
        else:
            code, message = "GOOGLE_OAUTH_HTTP", "Google отклонил получение разрешения OAuth. Проверьте настройки клиента; подключение не активировано."
    else:
        reasons = set()
        if isinstance(error, dict):
            for field in ("errors", "details"):
                entries = error.get(field, [])
                if isinstance(entries, list):
                    for item in entries:
                        if isinstance(item, dict) and isinstance(item.get("reason"), str):
                            reasons.add(item["reason"])
        if status == 403 and reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            code, message = "GOOGLE_CALENDAR_API_DISABLED", "Google Calendar API выключен для проекта OAuth-клиента. Включите Google Calendar API именно в этом проекте, затем начните подключение заново."
        elif status == 403 and reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}:
            code, message = "GOOGLE_CALENDAR_SCOPE", "Google не разрешил чтение календарей. Подключите аккаунт заново и подтвердите доступ к чтению календарей."
        elif status == 429 or 500 <= status <= 599 or (status == 403 and reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED"}):
            code, message = "GOOGLE_CALENDAR_RETRY", "Google временно ограничил чтение календаря. Повторите подключение позже; сохранённые настройки не изменены."
        elif status == 401:
            code, message = "GOOGLE_CALENDAR_AUTH", "Google отклонил токен доступа при чтении календаря. Подключите аккаунт заново через настройки сервиса."
        elif status == 404:
            code, message = "GOOGLE_CALENDAR_NOT_FOUND", "Основной календарь Google не найден или недоступен для подключаемого аккаунта."
        else:
            code, message = "GOOGLE_CALENDAR_HTTP", "Google отклонил чтение основного календаря после получения разрешения. Проверьте Google Calendar API и разрешения аккаунта."
    return GoogleAuthError(f"{message} [{code}]")


def _json_request(host, path, *, form=None, access_token=None):
    """Fixed-origin HTTPS calls; http.client never follows redirects."""
    connection = None
    try:
        connection = http.client.HTTPSConnection(host, 443, timeout=30,
                                                context=ssl.create_default_context())
        headers = {"Accept": "application/json"}
        body = None
        if form is not None:
            body = urlencode(form).encode("ascii")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if access_token is not None:
            headers["Authorization"] = "Bearer " + _secret(access_token, "access_token")
        connection.request("POST" if form is not None else "GET", path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise GoogleAuthError("Ответ Google превышает допустимый размер. Подключение не изменено. [GOOGLE_RESPONSE_TOO_LARGE]")
        if response.status != 200:
            raise _request_error(host, response.status, raw,
                                 refresh=form is not None and form.get("grant_type") == "refresh_token")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except GoogleAuthError:
        raise
    except Exception:
        raise GoogleAuthError("Не удалось безопасно получить ответ Google. Повторите подключение позже.") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _token_response(result):
    if str(result.get("token_type", "")).casefold() != "bearer":
        raise GoogleAuthError("Google не предоставил подходящий токен доступа.")
    if "scope" in result and SCOPE not in str(result["scope"]).split():
        raise GoogleAuthError("Google не предоставил разрешение на чтение календаря.")
    return _secret(result.get("access_token"), "access_token")


class GoogleConnectionStore:
    """One small, atomic encrypted record guarded across worker processes."""

    def __init__(self, directory=None, signing_key=None):
        self.directory = Path(directory or os.environ.get("DIGEST_STATE_DIR", ".local"))
        self.signing_key = signing_key if signing_key is not None else os.environ.get("DIGEST_ACTION_SECRET", "")

    def _cipher(self):
        if not isinstance(self.signing_key, str) or len(self.signing_key) < 32:
            raise GoogleAuthError("Для защищённого подключения Google необходим DIGEST_ACTION_SECRET.")
        key = hashlib.sha256(b"calendar-digest/google-oauth/v1\0" + self.signing_key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(key))

    @contextmanager
    def _locked(self, *, create):
        root_fd = directory_fd = lock_fd = None
        try:
            if create:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                root_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                if create:
                    try:
                        os.mkdir("google", mode=0o700, dir_fd=root_fd)
                    except FileExistsError:
                        pass
                directory_fd = os.open("google", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                       dir_fd=root_fd)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            info = os.fstat(directory_fd)
            if info.st_uid != os.geteuid():
                raise GoogleAuthError("Не удалось открыть защищённые настройки Google.")
            os.fchmod(directory_fd, 0o700)
            lock_fd = os.open(".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=directory_fd)
            info = os.fstat(lock_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise GoogleAuthError("Не удалось открыть защищённые настройки Google.")
            os.fchmod(lock_fd, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            yield directory_fd
        except GoogleAuthError:
            raise
        except (OSError, ValueError, TypeError):
            raise GoogleAuthError("Не удалось открыть защищённые настройки Google.") from None
        finally:
            for descriptor in (lock_fd, directory_fd, root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _read(self, directory_fd):
        if directory_fd is None:
            return {}
        try:
            descriptor = os.open("connection.enc", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=directory_fd)
        except FileNotFoundError:
            return {}
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077):
                raise GoogleAuthError("Настройки Google должны храниться в защищённом файле.")
            encrypted = stream.read(_MAX_BYTES + 1)
        try:
            if len(encrypted) > _MAX_BYTES:
                raise ValueError
            result = json.loads(self._cipher().decrypt(encrypted))
            if not isinstance(result, dict) or result.get("version") != 1:
                raise ValueError
            return result
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise GoogleAuthError("Не удалось прочитать защищённое подключение Google.") from None

    def _write(self, directory_fd, value):
        data = self._cipher().encrypt(json.dumps({**value, "version": 1}).encode("utf-8"))
        if len(data) > _MAX_BYTES:
            raise GoogleAuthError("Настройки Google превышают допустимый размер.")
        temporary = ".pending-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory_fd)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "connection.enc", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def connection(self):
        with self._locked(create=False) as directory_fd:
            return self._read(directory_fd).get("connection")

    def begin(self, client_id, client_secret, expected_email, *, redirect_uri, browser_binding):
        configuration = _configuration(client_id, client_secret, _expected_account(expected_email))
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        flow = {**configuration, "state_hash": hashlib.sha256(state.encode()).hexdigest(),
                "binding": hashlib.sha256(browser_binding.encode()).hexdigest(),
                "verifier": verifier, "expires_at": time.time() + _FLOW_TTL,
                "redirect_uri": redirect_uri}
        with self._locked(create=True) as directory_fd:
            value = self._read(directory_fd)
            value["flow"] = flow
            value.pop("completing", None)
            self._write(directory_fd, value)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPE, "state": state, "access_type": "offline", "prompt": "consent",
            "login_hint": configuration["expected_email"], "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def consume(self, state, *, browser_binding, redirect_uri):
        with self._locked(create=False) as directory_fd:
            value = self._read(directory_fd)
            flow = value.get("flow") or {}
            matches = (isinstance(state, str) and len(state) == 43 and browser_binding
                       and hmac.compare_digest(flow.get("state_hash", ""), hashlib.sha256(state.encode()).hexdigest())
                       and hmac.compare_digest(flow.get("binding", ""), hashlib.sha256(browser_binding.encode()).hexdigest())
                       and flow.get("redirect_uri") == redirect_uri
                       and isinstance(flow.get("expires_at"), (float, int)) and flow["expires_at"] > time.time())
            if not matches:
                raise GoogleAuthError("Ссылка подключения устарела или уже использована. Начните подключение заново.")
            value.pop("flow")
            value["completing"] = flow["state_hash"]
            self._write(directory_fd, value)
            return flow

    def complete(self, flow, code):
        """Exchange a single-use code, verify account, then activate atomically."""
        _expected_account(flow["expected_email"])
        result = _json_request("oauth2.googleapis.com", "/token", form={
            "client_id": flow["client_id"], "client_secret": flow["client_secret"],
            "code": _secret(code, "authorization_code"), "code_verifier": flow["verifier"],
            "grant_type": "authorization_code", "redirect_uri": flow["redirect_uri"],
        })
        access_token = _token_response(result)
        refresh_token = _secret(result.get("refresh_token"), "refresh_token")
        primary = _json_request("www.googleapis.com", "/calendar/v3/calendars/primary", access_token=access_token)
        if not isinstance(primary.get("id"), str) or primary["id"].casefold() != flow["expected_email"]:
            raise GoogleAuthError("Выбран другой аккаунт Google. Подключение не активировано.")
        connection = {name: flow[name] for name in ("client_id", "client_secret", "expected_email")}
        connection.update(refresh_token=refresh_token, activated_at=time.time())
        with self._locked(create=False) as directory_fd:
            value = self._read(directory_fd)
            if not hmac.compare_digest(value.get("completing", ""), flow["state_hash"]):
                raise GoogleAuthError("Начато другое подключение Google. Повторите вход из настроек.")
            value["connection"] = connection
            value.pop("completing", None)
            self._write(directory_fd, value)

    def rotate_token(self, old, new):
        with self._locked(create=False) as directory_fd:
            value = self._read(directory_fd)
            connection = value.get("connection") or {}
            if connection.get("refresh_token") == old:
                connection["refresh_token"] = new
                self._write(directory_fd, value)


def google_enabled():
    value = os.environ.get("GOOGLE_CALENDAR_ENABLED")
    if value is not None:
        if value.strip().casefold() in {"true", "1", "yes"}:
            return True
        if value.strip().casefold() in {"false", "0", "no"}:
            return False
        raise GoogleAuthError("GOOGLE_CALENDAR_ENABLED должен иметь значение true или false.")
    if os.environ.get("GOOGLE_REFRESH_TOKEN"):
        # A new direct grant must not inherit activation from an older browser
        # connection for a different account or OAuth client.
        return False
    return bool(GoogleConnectionStore().connection())


@dataclass(frozen=True)
class GoogleCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expected_email: str
    _store: GoogleConnectionStore | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        value = _configuration(self.client_id, self.client_secret, self.expected_email)
        _secret(self.refresh_token, "GOOGLE_REFRESH_TOKEN")
        object.__setattr__(self, "expected_email", value["expected_email"])

    @classmethod
    def from_environment(cls):
        names = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GOOGLE_CALENDAR_EMAIL")
        # A complete direct protected configuration is supported for deployments
        # that already hold an offline OAuth grant. Never mix two token owners.
        if os.environ.get("GOOGLE_REFRESH_TOKEN"):
            if any(not os.environ.get(name) for name in names):
                raise GoogleAuthError("Заполните все защищённые настройки подключения Google.")
            return cls(*(os.environ[name] for name in names))
        store = GoogleConnectionStore()
        connection = store.connection()
        if not connection:
            raise GoogleAuthError("Подключите ожидаемый аккаунт Google в защищённых настройках сервиса.")
        _expected_account(connection.get("expected_email"))
        return cls(*(connection[name] for name in ("client_id", "client_secret", "refresh_token", "expected_email")), _store=store)

    def access_token(self):
        result = _json_request("oauth2.googleapis.com", "/token", form={
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        })
        access = _token_response(result)
        if result.get("refresh_token") and self._store is not None:
            refreshed = _secret(result["refresh_token"], "refresh_token")
            self._store.rotate_token(self.refresh_token, refreshed)
        return access
