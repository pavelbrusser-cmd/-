"""Fresh reads and narrowly scoped, conditional CalDAV event updates.

No calendar creation/deletion or invitation scheduling is implemented here. A
write always rediscovers the resource, compares its strong ETag, performs one
conditional PUT, and verifies the stored VEVENT data with a fresh GET. Unknown
ACL support requires an explicitly verified provider configuration.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import TYPE_CHECKING
from urllib.parse import unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET

from icalendar import Calendar
import niquests
from niquests.auth import HTTPBasicAuth

from .calendar_collection import _quiet_library_logs
from .icloud import Credentials, _icloud_client
from .mailru import MailRuCredentials, _mailru_client

if TYPE_CHECKING:
    from .event_actions import EventTarget


MAX_ACTION_RESPONSE_BYTES = 4 * 1024 * 1024
ACTION_TIMEOUT_SECONDS = 20
_DAV = '{DAV:}'
_CALDAV = '{urn:ietf:params:xml:ns:caldav}'


class CalDAVActionError(Exception):
    """Safe diagnostics: never include server bodies, URLs or credentials."""


class CalDAVConflict(CalDAVActionError):
    """The resource changed after the confirmation page was prepared."""


class CalDAVUncertain(CalDAVActionError):
    """A PUT may have succeeded; do not automatically repeat it."""


@dataclass(frozen=True)
class ResourceVersion:
    data: str = field(repr=False)
    etag: str = field(repr=False)
    account_email: str = field(repr=False)
    source_id: str
    calendar_id: str = field(repr=False)
    uid: str = field(repr=False)
    resource_url: str = field(repr=False)
    calendar_name: str = field(default='', repr=False)
    writable: bool | None = None
    can_organize: bool = False
    automatic_scheduling: bool = False
    account_addresses: tuple[str, ...] = field(default=(), repr=False)
    status_cancellation_supported: bool = True

    @property
    def can_write(self):
        return self.writable


def _source(source_id):
    if source_id == 'icloud':
        return Credentials.from_environment(), _icloud_client, r'(?:caldav|p[0-9]+-caldav)\.icloud\.com'
    if source_id in {'mailru_1', 'mailru_2', 'mailru_3'}:
        return MailRuCredentials.from_environment(int(source_id[-1])), _mailru_client, r'calendar\.mail\.ru'
    raise CalDAVActionError('Неизвестный источник календаря.')


def _valid_url(value, hostname_pattern):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != 'https' or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or not re.fullmatch(hostname_pattern, parsed.hostname or '')
            or any(part in {'.', '..'} for part in unquote(parsed.path).split('/'))
        ):
            raise ValueError
        return parsed
    except (ValueError, TypeError):
        raise CalDAVActionError('Сервер вернул неподдерживаемый адрес ресурса.') from None


def _strong_etag(value):
    if not isinstance(value, str) or not re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value):
        raise CalDAVActionError('Сервер не предоставил сильную версию события; изменение остановлено.')
    return value


def _provider_etag(value, source_id):
    # Mail.ru returns a bare 40-hex token in both HTTP ETag and DAV:getetag.
    # Controlled fixtures established that quoted If-Match values are enforced
    # (wrong token => 412). Normalize only this observed, non-weak provider form.
    if (source_id in {'mailru_1', 'mailru_2', 'mailru_3'}
            and isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{40}', value)):
        value = '"' + value + '"'
    return _strong_etag(value)


def _prepared_url(value, hostname_pattern):
    _valid_url(value, hostname_pattern)
    result = niquests.Request('GET', value).prepare().url
    _valid_url(result, hostname_pattern)
    return result


class ScopedWriteSession(niquests.Session):
    """The only write allowed is PUT to one exact URL with one exact ETag."""

    def __init__(self, *, hostname_pattern, resource_url, etag):
        super().__init__(retries=False)
        self.hostname_pattern = hostname_pattern
        self.resource_url = _prepared_url(resource_url, hostname_pattern)
        self.etag = _strong_etag(etag)

    def send(self, request, **kwargs):
        _valid_url(request.url, self.hostname_pattern)
        if request.method == 'PUT':
            if request.url != self.resource_url or request.headers.get('If-Match') != self.etag:
                raise CalDAVActionError('Запись разрешена только для подтверждённой версии события.')
            # Never follow a redirect or retry a potentially accepted write.
            kwargs['allow_redirects'] = False
        elif request.method not in {'GET', 'REPORT', 'PROPFIND', 'OPTIONS'}:
            raise CalDAVActionError('Этот метод изменения календаря не разрешён.')
        kwargs['verify'] = True
        kwargs['timeout'] = ACTION_TIMEOUT_SECONDS
        if self.auth is not None:
            self.auth(request)
        return super().send(request, **kwargs)


def _request(session, method, url, **kwargs):
    response = session.request(
        method, url, timeout=ACTION_TIMEOUT_SECONDS, verify=True,
        allow_redirects=False, **kwargs,
    )
    if len(response.content) > MAX_ACTION_RESPONSE_BYTES:
        raise CalDAVActionError('Ответ календаря превышает допустимый размер.')
    return response


def _events(data, uid):
    try:
        parsed = Calendar.from_ical(data)
        events = parsed.walk('VEVENT')
        if parsed.name != 'VCALENDAR' or parsed.errors or not events or any(e.errors for e in events):
            raise ValueError
        if any(str(event.get('UID', '')) != uid for event in events):
            raise ValueError
        if any('DTSTART' not in event for event in events):
            raise ValueError
        if parsed.walk('VTODO') or parsed.walk('VJOURNAL'):
            raise ValueError
        return events
    except Exception:
        raise CalDAVActionError('Сервер вернул некорректный состав события.') from None


def _event_fingerprint(data, uid):
    # Parse/serialize to tolerate property ordering and folded lines, while
    # checking every VEVENT property and nested VALARM. Unexpected provider
    # rewrites are reported as uncertain, never accepted as a successful edit.
    return sorted(event.to_ical(sorted=True) for event in _events(data, uid))


def _same_revision_data(first, second, target):
    # Mail.ru changes DTSTAMP on GET while its entity version remains stable.
    # Ignore only that observed field for form-vs-POST comparison. The schedule
    # check retains raw duration-day semantics and recurring time-zone basis.
    from .event_actions import _schedule_snapshot

    def without_read_timestamp(data):
        events = _events(data, target.uid)
        for event in events:
            event.pop('DTSTAMP', None)
        return sorted(event.to_ical(sorted=True) for event in events)

    return (without_read_timestamp(first) == without_read_timestamp(second)
            and _schedule_snapshot(first, target) == _schedule_snapshot(second, target))


def _validate_changed_data(version, new_data):
    if not version.source_id.startswith('mailru_'):
        return
    from .event_actions import _rid

    def cancelled(data):
        return {(str(event['UID']), _rid(event)) for event in _events(data, version.uid)
                if str(event.get('STATUS', '')).strip().upper() == 'CANCELLED'}

    if cancelled(new_data) - cancelled(version.data):
        raise CalDAVActionError(
            'Отмена события в Mail.ru через дайджест пока недоступна. '
            'Изменения не отправлены.'
        )


def validate_change_capability(version: ResourceVersion, change) -> None:
    """Check known provider limitations before presenting a confirmation form.

    Existing cancelled overrides do not prevent changes to other occurrences.
    This check sends no requests and does not replace the write-time check.
    """
    _validate_changed_data(version, change.data)


def _preserved_fingerprint(data, uid, *, ignored_metadata=()):
    """Preserve complete recurrence sets, allowing only server timestamps.

    A semantic action verifier alone checks one occurrence. This independent
    comparison also catches lost overrides, attendees, alarms and other fields.
    Date-time values may be serialized with another equivalent time zone.
    """
    def value_key(value):
        if hasattr(value, 'dt'):
            decoded = value.dt
            if isinstance(decoded, datetime) and decoded.utcoffset() is not None:
                decoded = decoded.astimezone(timezone.utc)
            return str(decoded)
        if hasattr(value, 'dts'):
            return repr(sorted(value_key(item) for item in value.dts))
        serialized = value.to_ical() if hasattr(value, 'to_ical') else str(value)
        return serialized.decode('utf-8') if isinstance(serialized, bytes) else str(serialized)

    def component_key(component):
        properties = []
        for name, value in component.property_items(recursive=False, sorted=True):
            if name in {'DTSTAMP', 'LAST-MODIFIED', 'CREATED', 'BEGIN', 'END'} or name in ignored_metadata:
                continue
            params = getattr(value, 'params', {})
            ignored = {'TZID'} if hasattr(value, 'dt') or hasattr(value, 'dts') else set()
            properties.append((name, value_key(value), tuple(sorted(
                (str(key), str(item)) for key, item in params.items() if key not in ignored
            ))))
        return (component.name, tuple(sorted(properties)),
                tuple(sorted(component_key(child) for child in component.subcomponents)))
    return sorted(component_key(event) for event in _events(data, uid))


def _status_ok(element):
    fields = (element.text or '').split() if element is not None else []
    return len(fields) > 1 and fields[1] == '200'


def _xml(response):
    try:
        root = ET.fromstring(response.content)
        if root.tag != _DAV + 'multistatus':
            raise ValueError
        return root
    except (ET.ParseError, ValueError):
        raise CalDAVActionError('Сервер вернул некорректный ответ CalDAV.') from None


def _depth_zero_response(response, requested_url):
    def key(url):
        parsed = urlsplit(niquests.Request('GET', url).prepare().url)
        return parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), parsed.query, parsed.fragment
    matches = [item for item in _xml(response).findall(_DAV + 'response')
               if item.findtext(_DAV + 'href') and
               key(urljoin(requested_url, item.findtext(_DAV + 'href'))) == key(requested_url)]
    if len(matches) != 1:
        raise CalDAVActionError('Сервер вернул неоднозначные сведения о правах.')
    return matches[0]


def _calendar_writable(session, calendar_url):
    body = '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-privilege-set/></d:prop></d:propfind>'
    response = _request(session, 'PROPFIND', calendar_url, data=body, headers={
        'Depth': '0', 'Content-Type': 'application/xml; charset=utf-8',
    })
    if response.status_code in {403, 404, 405, 501}:
        return None
    if response.status_code != 207:
        raise CalDAVActionError('Не удалось проверить права календаря.')
    for item in [_depth_zero_response(response, calendar_url)]:
        for propstat in item.findall(_DAV + 'propstat'):
            if not _status_ok(propstat.find(_DAV + 'status')):
                continue
            privileges = propstat.find(_DAV + 'prop/' + _DAV + 'current-user-privilege-set')
            if privileges is not None:
                names = {child.tag for child in privileges.findall(_DAV + 'privilege/*')}
                return bool(names & {_DAV + 'all', _DAV + 'write', _DAV + 'write-content'})
    return None


def _principal_addresses(client):
    body = ('<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><c:calendar-user-address-set/></d:prop></d:propfind>')
    principal_url = str(client.get_principal().url)
    response = _request(client.session, 'PROPFIND', principal_url,
        data=body, headers={'Depth': '0', 'Content-Type': 'application/xml; charset=utf-8'})
    if response.status_code in {403, 404, 405, 501}:
        return ()
    if response.status_code != 207:
        raise CalDAVActionError('Не удалось проверить владельца календаря.')
    return tuple(
        (value.text or '')[7:].strip().casefold()
        for item in [_depth_zero_response(response, principal_url)]
        for part in item.findall(_DAV + 'propstat')
        if _status_ok(part.find(_DAV + 'status'))
        for value in part.findall(_DAV + 'prop/' + _CALDAV + 'calendar-user-address-set/' + _DAV + 'href')
        if (value.text or '').lower().startswith('mailto:')
    )


def _automatic_scheduling(session, calendar_url):
    response = _request(session, 'OPTIONS', calendar_url)
    if response.status_code not in {200, 204}:
        return False
    features = {value.strip().lower() for value in response.headers.get('DAV', '').split(',')}
    return 'calendar-auto-schedule' in features


def _listed_url(calendar_url, href, hostname_pattern):
    collection = _valid_url(calendar_url, hostname_pattern)
    url = _prepared_url(urljoin(calendar_url, href), hostname_pattern)
    location = _valid_url(url, hostname_pattern)
    if location.netloc != collection.netloc:
        raise CalDAVActionError('Ресурс находится вне выбранного календаря.')
    if unquote(location.path).rstrip('/') == unquote(collection.path).rstrip('/'):
        return None
    if not unquote(location.path).startswith(unquote(collection.path).rstrip('/') + '/'):
        raise CalDAVActionError('Ресурс находится вне выбранного календаря.')
    return url


def _metadata_uid_lookup(session, calendar_url, uid, hostname_pattern):
    """Find an exact UID basename among authenticated, bounded DAV hrefs."""
    response = _request(session, 'PROPFIND', calendar_url,
        data='<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:resourcetype/></d:prop></d:propfind>',
        headers={'Depth': '1', 'Content-Type': 'application/xml; charset=utf-8'})
    if response.status_code != 207:
        raise CalDAVActionError('Не удалось получить адрес выбранного события.')
    matches = []
    for item in _xml(response).findall(_DAV + 'response'):
        href = item.findtext(_DAV + 'href')
        if not href:
            raise CalDAVActionError('Ответ календаря содержит неполные адреса.')
        url = _listed_url(calendar_url, href, hostname_pattern)
        if url is None:
            continue
        if unquote(urlsplit(url).path).rsplit('/', 1)[-1] == uid + '.ics':
            matches.append(url)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CalDAVActionError('Событие не найдено однозначно; обновите дайджест.')
    return None


def _icloud_uid_fallback(session, calendar_url, uid, hostname_pattern):
    """iCloud rejects UID-filtered REPORT with 412; discover hrefs read-only.

    First try its server-listed UID.ics basename. Opaque names use a bounded
    UID-only calendar-data projection, never an assumed or user-provided URL.
    A provider ignoring the projection still faces the same response size cap.
    """
    listed = _metadata_uid_lookup(session, calendar_url, uid, hostname_pattern)
    if listed is not None:
        return listed
    query = ET.Element(_CALDAV + 'calendar-query')
    prop = ET.SubElement(query, _DAV + 'prop')
    data = ET.SubElement(prop, _CALDAV + 'calendar-data')
    projection = ET.SubElement(data, _CALDAV + 'comp', name='VCALENDAR')
    ET.SubElement(projection, _CALDAV + 'prop', name='VERSION')
    event = ET.SubElement(projection, _CALDAV + 'comp', name='VEVENT')
    ET.SubElement(event, _CALDAV + 'prop', name='UID')
    filt = ET.SubElement(query, _CALDAV + 'filter')
    root_filter = ET.SubElement(filt, _CALDAV + 'comp-filter', name='VCALENDAR')
    ET.SubElement(root_filter, _CALDAV + 'comp-filter', name='VEVENT')
    response = _request(session, 'REPORT', calendar_url, data=ET.tostring(query),
        headers={'Depth': '1', 'Content-Type': 'application/xml; charset=utf-8'})
    return _matching_report_url(response, calendar_url, uid, hostname_pattern,
                                complete_data=False)


def _find_resource(session, calendar_url, uid, hostname_pattern, *, allow_uid_fallback=False,
                   metadata_first=False):
    if metadata_first:
        # Mail.ru can ignore both UID filters and calendar-data projections.
        # Avoid downloading its complete calendar when it lists a UID.ics href.
        listed = _metadata_uid_lookup(session, calendar_url, uid, hostname_pattern)
        if listed is not None:
            return listed
    query = ET.Element(_CALDAV + 'calendar-query')
    prop = ET.SubElement(query, _DAV + 'prop')
    ET.SubElement(prop, _DAV + 'getetag')
    data = ET.SubElement(prop, _CALDAV + 'calendar-data')
    projection = ET.SubElement(data, _CALDAV + 'comp', name='VCALENDAR')
    ET.SubElement(projection, _CALDAV + 'prop', name='VERSION')
    component = ET.SubElement(projection, _CALDAV + 'comp', name='VEVENT')
    ET.SubElement(component, _CALDAV + 'prop', name='UID')
    filt = ET.SubElement(query, _CALDAV + 'filter')
    vcal = ET.SubElement(filt, _CALDAV + 'comp-filter', name='VCALENDAR')
    vevent = ET.SubElement(vcal, _CALDAV + 'comp-filter', name='VEVENT')
    uid_filter = ET.SubElement(vevent, _CALDAV + 'prop-filter', name='UID')
    ET.SubElement(uid_filter, _CALDAV + 'text-match', collation='i;octet').text = uid
    response = _request(session, 'REPORT', calendar_url, data=ET.tostring(query), headers={
        'Depth': '1', 'Content-Type': 'application/xml; charset=utf-8',
    })
    if response.status_code == 412 and allow_uid_fallback:
        return _icloud_uid_fallback(session, calendar_url, uid, hostname_pattern)
    return _matching_report_url(response, calendar_url, uid, hostname_pattern,
                                complete_data=False)


def _matching_report_url(response, calendar_url, uid, hostname_pattern, *, complete_data=True):
    if response.status_code != 207:
        raise CalDAVActionError('Не удалось найти выбранное событие.')
    matches = []
    for item in _xml(response).findall(_DAV + 'response'):
        href = item.findtext(_DAV + 'href')
        if not href:
            raise CalDAVActionError('Ответ календаря содержит неполные записи.')
        url = _listed_url(calendar_url, href, hostname_pattern)
        if url is None:
            continue  # iCloud includes its collection with a 404 calendar-data propstat.
        data = [
            value.text for part in item.findall(_DAV + 'propstat')
            if _status_ok(part.find(_DAV + 'status'))
            for value in part.findall(_DAV + 'prop/' + _CALDAV + 'calendar-data')
        ]
        if len(data) != 1 or not data[0]:
            raise CalDAVActionError('Ответ календаря содержит неполные записи.')
        try:
            events = Calendar.from_ical(data[0]).walk('VEVENT')
        except Exception:
            raise CalDAVActionError('Сервер вернул некорректное событие.') from None
        # CalDAV text-match is substring matching. Verify exact UID locally.
        if any(str(event.get('UID', '')) == uid for event in events):
            if complete_data:
                _events(data[0], uid)
            matches.append(url)
    if len(matches) != 1:
        raise CalDAVActionError('Событие не найдено однозначно; обновите дайджест.')
    return matches[0]


def _read_resource(session, url, *, target, account_email, calendar_name, writable,
                   account_addresses=(), automatic_scheduling=False):
    response = _request(session, 'GET', url)
    if response.status_code != 200:
        raise CalDAVActionError('Не удалось прочитать текущую версию события.')
    try:
        data = response.content.decode('utf-8-sig')
    except UnicodeError:
        raise CalDAVActionError('Сервер вернул неподдерживаемую кодировку события.') from None
    events = _events(data, target.uid)
    organizers = [str(event['ORGANIZER']).strip().casefold().removeprefix('mailto:')
                  for event in events if 'ORGANIZER' in event]
    can_organize = writable is True and bool(organizers) and all(
        value in account_addresses for value in organizers
    )
    return ResourceVersion(
        data=data, etag=_provider_etag(response.headers.get('ETag'), target.source_id),
        account_email=account_email, source_id=target.source_id,
        calendar_id=target.calendar_id, uid=target.uid, resource_url=url,
        calendar_name=calendar_name, writable=writable,
        can_organize=can_organize, account_addresses=account_addresses,
        automatic_scheduling=automatic_scheduling,
        status_cancellation_supported=target.source_id == 'icloud',
    )


class CalDAVActionBackend:
    def __init__(self, *, verified_acl_fallback_sources=()):
        # Populate only after a controlled, attendee-free fixture has verified
        # that this provider enforces ACL and conditional updates correctly.
        allowed = frozenset(verified_acl_fallback_sources)
        if not allowed <= {'icloud', 'mailru_1', 'mailru_2', 'mailru_3'}:
            raise CalDAVActionError('Неизвестный источник календаря.')
        self.verified_acl_fallback_sources = allowed

    def read(self, target: 'EventTarget') -> ResourceVersion:
        with _quiet_library_logs():
            try:
                if not isinstance(target.uid, str) or not target.uid or len(target.uid) > 2048:
                    raise CalDAVActionError('Некорректный идентификатор события.')
                credentials, connect, pattern = _source(target.source_id)
                with connect(credentials) as client:
                    calendars = [c for c in client.get_calendars() if c.id == target.calendar_id]
                    if len(calendars) != 1 or 'VEVENT' not in calendars[0].get_supported_components():
                        raise CalDAVActionError('Выбранный календарь недоступен.')
                    calendar = calendars[0]
                    url = str(calendar.url)
                    _valid_url(url, pattern)
                    resource_url = _find_resource(client.session, url, target.uid, pattern,
                        allow_uid_fallback=target.source_id == 'icloud',
                        metadata_first=target.source_id.startswith('mailru_'))
                    writable = _calendar_writable(client.session, resource_url)
                    if writable is None:
                        # Some providers expose the inherited privilege set on
                        # the collection only. An explicit resource denial wins.
                        writable = _calendar_writable(client.session, url)
                    addresses = _principal_addresses(client)
                    automatic_scheduling = _automatic_scheduling(client.session, url)
                    return _read_resource(
                        client.session, resource_url, target=target,
                        account_email=credentials.username,
                        calendar_name=calendar.get_display_name() or 'Без названия',
                        writable=writable,
                        account_addresses=addresses, automatic_scheduling=automatic_scheduling,
                    )
            except CalDAVActionError:
                raise
            except Exception:
                raise CalDAVActionError('Не удалось безопасно прочитать событие; попробуйте позже.') from None

    def write(self, target: 'EventTarget', version: ResourceVersion, new_data: str,
              *, verify=None) -> ResourceVersion:
        """Write once; an optional semantic verifier may accept server metadata changes.

        ``verify(stored_data)`` must check the intended action and is supplied by
        the trusted mutation layer, never by request parameters.
        """
        with _quiet_library_logs():
            try:
                from .event_actions import _schedule_snapshot
                if (version.source_id, version.calendar_id, version.uid) != (target.source_id, target.calendar_id, target.uid):
                    raise CalDAVActionError('Версия относится к другому событию.')
                _strong_etag(version.etag)
                if not isinstance(new_data, str) or len(new_data.encode('utf-8')) > MAX_ACTION_RESPONSE_BYTES:
                    raise CalDAVActionError('Изменённое событие превышает допустимый размер.')
                expected = _event_fingerprint(new_data, target.uid)
                expected_schedule = _schedule_snapshot(new_data, target)
                # Three controlled accounts returned PUT 201 but kept CONFIRMED
                # for cancellation attempts. Never send that ineffective change.
                _validate_changed_data(version, new_data)
                # Mail.ru updates this provider-owned acknowledgement timestamp
                # when moving an event. All alarm definitions remain compared.
                ignored_metadata = {'X-MOZ-LASTACK'} if target.source_id.startswith('mailru_') else set()
                preserved = _preserved_fingerprint(new_data, target.uid, ignored_metadata=ignored_metadata)
                fresh = self.read(target)
                if (fresh.etag != version.etag or fresh.resource_url != version.resource_url
                        or not _same_revision_data(fresh.data, version.data, target)):
                    raise CalDAVConflict('Событие уже изменилось. Откройте форму заново.')
                if fresh.writable is False or (
                    fresh.writable is None and target.source_id not in self.verified_acl_fallback_sources
                ):
                    raise CalDAVActionError('Право изменения этого календаря пока не подтверждено.')
                credentials, _, pattern = _source(target.source_id)
                with ScopedWriteSession(hostname_pattern=pattern, resource_url=fresh.resource_url, etag=fresh.etag) as session:
                    session.auth = HTTPBasicAuth(credentials.username, credentials.app_password)
                    try:
                        response = _request(session, 'PUT', fresh.resource_url,
                            data=new_data.encode('utf-8'), headers={
                                'If-Match': fresh.etag,
                                'Content-Type': 'text/calendar; charset=utf-8',
                            })
                    except Exception:
                        pass  # Verify once below; never repeat the PUT.
                    else:
                        if response.status_code == 412:
                            raise CalDAVConflict('Событие изменилось перед сохранением. Откройте форму заново.')
                        if response.status_code in {401, 403, 405}:
                            raise CalDAVActionError('Сервер не разрешил изменение события.')
                        if response.status_code == 201 and target.source_id == 'icloud':
                            raise CalDAVUncertain('Сервер не подтвердил условное обновление. Проверьте календарь; не повторяйте действие.')
                    try:
                        stored = _read_resource(session, fresh.resource_url, target=target,
                            account_email=fresh.account_email, calendar_name=fresh.calendar_name,
                            writable=fresh.writable, account_addresses=fresh.account_addresses,
                            automatic_scheduling=fresh.automatic_scheduling)
                        verified = (verify(stored.data) if verify is not None else
                                    _event_fingerprint(stored.data, target.uid) == expected)
                        if (not verified
                                or _schedule_snapshot(stored.data, target) != expected_schedule
                                or _preserved_fingerprint(stored.data, target.uid,
                                    ignored_metadata=ignored_metadata) != preserved):
                            raise ValueError
                        if stored.etag == fresh.etag and stored.data != fresh.data:
                            raise ValueError
                    except Exception:
                        raise CalDAVUncertain(
                            'Результат сохранения не подтверждён. Не повторяйте действие; проверьте календарь.'
                        ) from None
                    # A timeout is resolved only when a fresh GET proves the
                    # requested contents; never send a second PUT automatically.
                    return stored
            except CalDAVActionError:
                raise
            except Exception:
                raise CalDAVActionError('Не удалось безопасно изменить событие.') from None
