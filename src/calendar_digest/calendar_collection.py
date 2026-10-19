"""Collect complete VEVENT resources from every enabled calendar source.

This initial implementation rereads every event resource on every run. Keeping
the full recurrence sets makes moved-out overrides visible to digest selection;
a date-filtered REPORT cannot guarantee that. Incremental ETag/sync updates are
not implemented yet. Nothing is persisted by this module.

Limits reject the entire collection rather than returning incomplete data.
Response size is checked after receipt, before the DAV parser processes it;
this is not a streaming network or memory quota. Parsed DAV objects are released
before the next calendar is fetched, but one complete REPORT is still buffered.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
import logging
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree

from icalendar import Calendar

from .icloud import Credentials, _icloud_client
from .mailru import MailRuCredentials, _mailru_client
from .google_auth import google_enabled
from .readonly_caldav import CalendarConnectionError


MAX_RESOURCES = 10_000
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_RESPONSE_BYTES = 30 * 1024 * 1024
COLLECTION_TIMEOUT_SECONDS = 60


class CollectionLimitError(CalendarConnectionError):
    """The complete collection exceeds a supported size limit."""


@dataclass(frozen=True)
class CalendarResource:
    source_id: str
    calendar_id: str
    calendar_name: str
    account_email: str = field(repr=False)
    data: str = field(repr=False)


@dataclass(frozen=True)
class Collection:
    resources: list[CalendarResource]
    calendar_count: int


@contextmanager
def _quiet_library_logs():
    # DAV libraries can log raw responses, including event contents. Collection
    # is synchronous; restore the application's logging threshold on every exit.
    previous = logging.root.manager.disable
    response_logger = logging.getLogger("caldav.response")
    previous_response_level = response_logger.level
    # caldav 3.4 checks this logger's numeric level before serializing the whole
    # XML response for debug logging. disable() alone suppresses the output but
    # still lets that otherwise unnecessary full-document allocation happen.
    response_logger.setLevel(logging.CRITICAL)
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        response_logger.setLevel(previous_response_level)
        logging.disable(previous)


def _successful_status(element):
    fields = (element.text or "").split() if element is not None else []
    return len(fields) >= 2 and fields[1].isdigit() and 200 <= int(fields[1]) < 300


def _check_response(response, *, report_counts=None, **kwargs):
    """Reject oversized or partial calendar-query responses before parsing."""
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise CollectionLimitError("Ответ календаря превышает допустимый размер; сбор остановлен.")
    if response.request.method != "REPORT" or response.status_code != 207:
        return response
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        raise CalendarConnectionError("Календарь вернул некорректный XML; сбор остановлен.") from None
    if root.tag != "{DAV:}multistatus":
        raise CalendarConnectionError("Календарь вернул некорректный REPORT; сбор остановлен.")
    collection_url = urlsplit(response.url or response.request.url)
    record_count = 0
    for item in root.findall("{DAV:}response"):
        href = item.find("{DAV:}href")
        if href is not None and href.text:
            item_url = urlsplit(urljoin(response.url or response.request.url, href.text))
            if (
                item_url.hostname == collection_url.hostname
                and unquote(item_url.path).rstrip("/") == unquote(collection_url.path).rstrip("/")
            ):
                # iCloud includes the queried calendar collection itself, with
                # calendar-data in a 404 propstat. It is not an event resource.
                continue
        direct_status = item.find("{DAV:}status")
        if direct_status is not None and not _successful_status(direct_status):
            raise CalendarConnectionError("Не все записи календаря доступны; сбор остановлен.")
        data_elements = [
            element
            for propstat in item.findall("{DAV:}propstat")
            if _successful_status(propstat.find("{DAV:}status"))
            for element in propstat.findall("{DAV:}prop/{urn:ietf:params:xml:ns:caldav}calendar-data")
        ]
        if len(data_elements) != 1 or not data_elements[0].text:
            raise CalendarConnectionError("Ответ календаря содержит неполные записи; сбор остановлен.")
        try:
            events = Calendar.from_ical(data_elements[0].text).walk("VEVENT")
        except Exception:
            raise CalendarConnectionError("Календарь вернул некорректную запись; сбор остановлен.") from None
        if not events:
            raise CalendarConnectionError("CalDAV вернул запись без события VEVENT; сбор остановлен.")
        record_count += 1
    if report_counts is not None:
        report_counts.append(record_count)
    return response


def collect_all() -> Collection:
    """Fetch all VEVENT calendars, retaining raw masters and overrides.

    The agreed selection is all event calendars in iCloud and three separate
    Mail.ru accounts, plus Google when explicitly enabled. Failure in any
    account aborts the result for all accounts.
    Account emails identify participation locally and label digest sources.
    """
    resources = []
    calendar_count = 0
    total_bytes = 0
    source_id = "configuration"
    with _quiet_library_logs():
        try:
            sources = [
                ("icloud", Credentials.from_environment(), _icloud_client),
                *[
                    (f"mailru_{account}", MailRuCredentials.from_environment(account), _mailru_client)
                    for account in (1, 2, 3)
                ],
            ]
            for source_id, credentials, connect in sources:
                with connect(credentials) as client:
                    # Full history queries are larger than discovery probes.
                    client.timeout = COLLECTION_TIMEOUT_SECONDS
                    report_counts = []
                    client.session.hooks.setdefault("response", []).append(
                        partial(_check_response, report_counts=report_counts)
                    )
                    for calendar in client.get_calendars():
                        if "VEVENT" not in calendar.get_supported_components():
                            continue
                        calendar_count += 1
                        calendar_id = calendar.id
                        if not isinstance(calendar_id, str) or not calendar_id:
                            raise CalendarConnectionError("У календаря отсутствует идентификатор; сбор остановлен.")
                        calendar_name = calendar.get_display_name() or "Без названия"
                        # No date filter or expansion: preserve exception-only
                        # resources and overrides moved arbitrarily far away.
                        first_report = len(report_counts)
                        records = calendar.search(event=True, expand=False)
                        try:
                            if len(records) != sum(report_counts[first_report:]):
                                raise CalendarConnectionError("Обработаны не все записи календаря; сбор остановлен.")
                            for record in records:
                                if len(resources) >= MAX_RESOURCES:
                                    raise CollectionLimitError("Число записей календарей превышает лимит; сбор остановлен.")
                                data = record.data
                                if isinstance(data, bytes):
                                    data = data.decode("utf-8")
                                if not isinstance(data, str) or not data.strip():
                                    raise CalendarConnectionError("Календарь вернул пустую запись; сбор остановлен.")
                                total_bytes += len(data.encode("utf-8"))
                                if total_bytes > MAX_TOTAL_BYTES:
                                    raise CollectionLimitError("Общий объём календарей превышает лимит; сбор остановлен.")
                                if not Calendar.from_ical(data).walk("VEVENT"):
                                    raise CalendarConnectionError("CalDAV вернул запись без события VEVENT; сбор остановлен.")
                                resources.append(CalendarResource(
                                    source_id=source_id,
                                    calendar_id=calendar_id,
                                    calendar_name=calendar_name,
                                    account_email=credentials.username,
                                    data=data,
                                ))
                        finally:
                            # Extra server properties (e.g. getetag) remain lxml
                            # elements in caldav's Event.props. One such element
                            # retains the complete REPORT tree. Do not carry it,
                            # or the result list/last loop variable, into the
                            # next search, which buffers another full response.
                            for record in records:
                                record.props.clear()
                            records.clear()
                            record = None
                            del records
            source_id = "google"
            if google_enabled():
                # The Google adapter returns complete recurrence sets too. Do
                # not return the already collected DAV data on an OAuth/API
                # failure, which would make existing Google events disappear.
                from .google_calendar import collect_google

                google = collect_google()
                calendar_count += google.calendar_count
                for item in google.resources:
                    if len(resources) >= MAX_RESOURCES:
                        raise CollectionLimitError("Число записей календарей превышает лимит; сбор остановлен.")
                    total_bytes += len(item.data.encode("utf-8"))
                    if total_bytes > MAX_TOTAL_BYTES:
                        raise CollectionLimitError("Общий объём календарей превышает лимит; сбор остановлен.")
                    resources.append(CalendarResource(
                        source_id="google", calendar_id=item.calendar_id,
                        calendar_name=item.calendar_name, account_email=item.account_email,
                        data=item.data,
                    ))
        except CalendarConnectionError:
            raise
        except Exception:
            raise CalendarConnectionError(
                f"Не удалось полностью прочитать {source_id}; сбор остановлен."
            ) from None
    return Collection(resources=resources, calendar_count=calendar_count)
