"""Shared authenticated, read-only CalDAV transport with safe diagnostics."""

from contextlib import contextmanager
import re
from urllib.parse import urlsplit

from caldav import DAVClient
from caldav.lib.error import AuthorizationError, DAVError
from lxml.etree import XMLSyntaxError
import niquests


class CalendarConnectionError(Exception):
    """An actionable error safe to display without server response details."""


class ReadOnlyCalDAVSession(niquests.Session):
    def __init__(self, *, hostname_pattern: str, initial_host: str, provider: str):
        super().__init__()
        self.hostname_pattern = hostname_pattern
        self.last_host = initial_host
        self.provider = provider

    def send(self, request, **kwargs):
        try:
            parsed = urlsplit(request.url)
            host = parsed.hostname or ""
            port = parsed.port
        except ValueError:
            raise CalendarConnectionError(
                f"{self.provider} вернул некорректный адрес сервера CalDAV."
            ) from None
        if (
            parsed.scheme != "https"
            or port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or not re.fullmatch(self.hostname_pattern, host)
        ):
            raise CalendarConnectionError(
                f"{self.provider} вернул неподдерживаемый адрес сервера CalDAV."
            )
        if request.method not in {"PROPFIND", "GET", "OPTIONS", "REPORT"}:
            raise CalendarConnectionError("Адаптер календарей допускает только чтение.")
        self.last_host = host
        # Restore Basic auth after redirects only after validating the target.
        if self.auth is not None:
            self.auth(request)
        return super().send(request, **kwargs)


@contextmanager
def connect_readonly(
    *, username: str, password: str, url: str, hostname_pattern: str, provider: str,
    session: ReadOnlyCalDAVSession | None = None,
):
    session = session if session is not None else ReadOnlyCalDAVSession(
        hostname_pattern=hostname_pattern,
        initial_host=urlsplit(url).hostname or "",
        provider=provider,
    )
    try:
        with DAVClient(
            url=url, username=username, password=password, auth_type="basic",
            timeout=20, ssl_verify_cert=True, enable_rfc6764=False,
        ) as client:
            client.session.close()
            session.auth = client.auth
            client.session = session
            yield client
    except CalendarConnectionError:
        raise
    except niquests.exceptions.ProxyError:
        raise CalendarConnectionError(
            f"Прокси не пропускает подключение к {session.last_host}. "
            "Проверьте разрешённые домены в настройках среды."
        ) from None
    except niquests.exceptions.SSLError:
        raise CalendarConnectionError(f"Не удалось проверить TLS-сертификат {provider}.") from None
    except niquests.exceptions.Timeout:
        raise CalendarConnectionError(f"Сервер {provider} не ответил за отведённое время.") from None
    except AuthorizationError:
        raise CalendarConnectionError(
            "Сервер отказал в доступе. Проверьте логин, пароль приложения "
            "и сетевой доступ; содержимое ответа скрыто."
        ) from None
    except niquests.exceptions.RequestException:
        raise CalendarConnectionError(f"Не удалось установить соединение с {provider}.") from None
    except (DAVError, XMLSyntaxError):
        raise CalendarConnectionError("Сервер не вернул корректный ответ CalDAV.") from None
    finally:
        session.close()
