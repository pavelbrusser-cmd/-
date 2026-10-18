"""Complete calendar inventories with bounded, changed-only DAV downloads.

This read-only adapter does not persist anything. A caller must replace its
cache only after every enabled account succeeds. Every DAV calendar is inventoried with
Depth: 1 PROPFIND; only new or changed ETags require calendar-multiget bodies.
Response limits apply after receipt, before XML parsing, as in collection.py.
Google uses a complete bounded snapshot; its stable grouped-resource ETags and
identities keep unchanged events from causing needless reminder replanning.
"""

from dataclasses import dataclass, field
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from icalendar import Calendar
import niquests

from .calendar_collection import (
    CalendarResource, CollectionLimitError, _quiet_library_logs,
    COLLECTION_TIMEOUT_SECONDS, MAX_RESOURCES, MAX_RESPONSE_BYTES, MAX_TOTAL_BYTES,
)
from .icloud import Credentials, _icloud_client
from .mailru import MailRuCredentials, _mailru_client
from .google_auth import google_enabled
from .readonly_caldav import CalendarConnectionError


_DAV = "{DAV:}"
_CAL = "{urn:ietf:params:xml:ns:caldav}"
MULTIGET_BATCH_SIZE = 50
_SOURCES = {"icloud", "mailru_1", "mailru_2", "mailru_3", "google"}


@dataclass(frozen=True)
class CachedResource:
    source_id: str
    calendar_id: str
    calendar_name: str
    href: str
    etag: str
    data: str = field(repr=False)


@dataclass(frozen=True)
class SyncResult:
    resources: list[CachedResource]
    calendar_count: int
    changed: int
    fetched: int
    deleted: int


def _error():
    return CalendarConnectionError(
        "Не удалось полностью обновить календари; предыдущее состояние сохранено."
    )


def _xml(content):
    if len(content) > MAX_RESPONSE_BYTES:
        raise CollectionLimitError("Ответ календаря превышает допустимый размер; обновление остановлено.")
    # ElementTree never resolves external entities. Reject internal entity/DTD
    # declarations too, before parsing, including UTF-16/32 encodings.
    if b"\x00" in content or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.I):
        raise _error()
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise _error() from None
    if root.tag != _DAV + "multistatus":
        raise _error()
    if any(child.tag != _DAV + "response" for child in root):
        # error/limit markers must never be mistaken for a complete inventory.
        raise _error()
    return root


def _status(element):
    parts = (element.text or "").split() if element is not None else []
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise _error()
    return int(parts[1])


def _properties(item):
    direct = item.findall(_DAV + "status")
    if len(direct) > 1 or (direct and not 200 <= _status(direct[0]) < 300):
        raise _error()
    result = {}
    propstats = item.findall(_DAV + "propstat")
    if not propstats:
        raise _error()
    for part in propstats:
        status = _status(part.find(_DAV + "status"))
        properties = part.findall(_DAV + "prop")
        if len(properties) != 1 or (not 200 <= status < 300 and status != 404):
            raise _error()
        for prop in properties[0]:
            if prop.tag in result:
                raise _error()
            result[prop.tag] = prop if 200 <= status < 300 else None
    return result


def _guard_response(response, **kwargs):
    """Protect discovery too: do not silently omit forbidden calendar records."""
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise CollectionLimitError("Ответ календаря превышает допустимый размер; обновление остановлено.")
    if response.status_code != 207:
        if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("Location"):
            # Discovery can legitimately move from caldav.icloud.com to an
            # account's shard. The existing read-only session checks each
            # redirect destination before attaching credentials.
            return response
        raise _error()
    root = _xml(content)
    request = response.request
    request_body = request.body or b""
    try:
        if not request_body and request.method == "PROPFIND":
            # RFC 4918 §9.1: no body is equivalent to an allprop request.
            # caldav uses this for some Mail.ru discovery/property queries.
            query = ET.Element(_DAV + "propfind")
        else:
            query = ET.fromstring(request_body)
    except (ET.ParseError, ValueError):
        raise _error() from None
    needs_type = query.find(_DAV + "prop/" + _DAV + "resourcetype") is not None
    inventory_query = (request.method == "PROPFIND" and request.headers.get("Depth") == "1"
                       and query.find(_DAV + "prop/" + _DAV + "getetag") is not None)
    seen = set()
    for item in root:
        hrefs = item.findall(_DAV + "href")
        if len(hrefs) != 1 or not hrefs[0].text or hrefs[0].text in seen:
            raise _error()
        seen.add(hrefs[0].text)
        props = _properties(item)
        if needs_type and props.get(_DAV + "resourcetype") is None:
            # iCloud reports resourcetype as 404 for regular event members.
            # Accept this only in our ETag inventory, never calendar discovery
            # or the collection's own record. Downloaded bodies still must be
            # VEVENT resources before they can enter the cache.
            etag = props.get(_DAV + "getetag")
            if (not inventory_query or _href(request.url, hrefs[0].text) is None
                    or etag is None or not etag.text or not etag.text.strip()):
                raise _error()
    return response


def _url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError
        if re.search(r"[\x00-\x20\x7f\\]", value):
            raise ValueError
        decoded = unquote(parsed.path, errors="strict")
        # Repeatedly encoded traversal has different meanings to proxies and
        # servers. Reject it, together with encoded separators and controls.
        checked = decoded
        for _ in range(4):
            if (re.search(r"[\x00-\x1f\x7f\\]", checked)
                    or any(piece in (".", "..") for piece in checked.split("/"))):
                raise ValueError
            again = unquote(checked, errors="strict")
            if again == checked:
                break
            if again.count("/") != checked.count("/"):
                raise ValueError
            checked = again
        else:
            raise ValueError
        if decoded.count("/") != parsed.path.count("/"):
            raise ValueError
        prepared = niquests.Request("GET", urlunsplit((
            "https", parsed.hostname.lower(), parsed.path, "", "",
        ))).prepare().url
        return prepared, decoded, parsed.hostname.lower()
    except (TypeError, ValueError, UnicodeError, niquests.exceptions.RequestException):
        raise _error() from None


def _href(calendar_url, href):
    if not isinstance(href, str) or not href:
        raise _error()
    base, base_path, host = _url(calendar_url)
    url, path, found_host = _url(urljoin(base.rstrip("/") + "/", href))
    if found_host != host:
        raise _error()
    if path.rstrip("/") == base_path.rstrip("/"):
        return None
    if not path.startswith(base_path.rstrip("/") + "/"):
        raise _error()
    # Depth 1 must return immediate event members, not nested collections.
    if "/" in path[len(base_path.rstrip("/")) + 1:]:
        raise _error()
    return url


def _item_href(item, calendar_url):
    hrefs = item.findall(_DAV + "href")
    if len(hrefs) != 1:
        raise _error()
    return _href(calendar_url, hrefs[0].text)


def _request(client, method, calendar_url, body, *, depth=None):
    headers = {"Content-Type": "application/xml; charset=utf-8"}
    if depth is not None:
        headers["Depth"] = str(depth)
    response = client.session.request(
        method, calendar_url, data=body, headers=headers,
        auth=client.auth, timeout=COLLECTION_TIMEOUT_SECONDS,
        verify=True, allow_redirects=False,
    )
    try:
        if response.status_code != 207 or response.history:
            raise _error()
        return _xml(response.content)
    finally:
        response.close()


def _inventory(client, calendar_url):
    root = _request(client, "PROPFIND", calendar_url,
        b'<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/><d:resourcetype/>'
        b'</d:prop></d:propfind>', depth=1)
    resources = {}
    seen_paths = set()
    collection_seen = False
    for item in root:
        href = _item_href(item, calendar_url)
        props = _properties(item)
        kind = props.get(_DAV + "resourcetype")
        if href is None:
            if collection_seen or kind is None or kind.find(_DAV + "collection") is None:
                raise _error()
            collection_seen = True
            continue
        path = unquote(urlsplit(href).path)
        if path in seen_paths or (kind is not None and len(kind)):
            raise _error()
        seen_paths.add(path)
        etag = props.get(_DAV + "getetag")
        if etag is None or not etag.text or not etag.text.strip():
            raise _error()
        resources[href] = etag.text.strip()
        if len(resources) > MAX_RESOURCES:
            raise CollectionLimitError("Число записей календарей превышает лимит; обновление остановлено.")
    if not collection_seen:
        raise _error()
    return resources


def _multiget(client, calendar_url, requested):
    query = ET.Element(_CAL + "calendar-multiget")
    props = ET.SubElement(query, _DAV + "prop")
    ET.SubElement(props, _DAV + "getetag")
    ET.SubElement(props, _CAL + "calendar-data")
    for href in requested:
        ET.SubElement(query, _DAV + "href").text = urlsplit(href).path
    root = _request(client, "REPORT", calendar_url, ET.tostring(query, encoding="utf-8"))
    found = {}
    for item in root:
        href = _item_href(item, calendar_url)
        if href not in requested or href in found:
            raise _error()
        props = _properties(item)
        etag = props.get(_DAV + "getetag")
        data = props.get(_CAL + "calendar-data")
        if (etag is None or not etag.text or etag.text.strip() != requested[href]
                or data is None or not data.text or not data.text.strip()):
            # A resource changed between inventory and download. A later full
            # cycle can retry; never mix body versions or infer a deletion.
            raise _error()
        try:
            parsed = Calendar.from_ical(data.text)
            if (parsed.name != "VCALENDAR" or not parsed.walk("VEVENT")
                    or any(child.name not in {"VEVENT", "VTIMEZONE"}
                           for child in parsed.subcomponents)):
                raise ValueError
        except Exception:
            raise _error() from None
        found[href] = data.text
    if set(found) != set(requested):
        raise _error()
    return found


def _key(resource):
    return resource.source_id, resource.calendar_id, resource.href


def _validate_google_resource(item):
    # These are stable synthetic identifiers, not remote URLs to be fetched.
    # Still validate stored identities and event bodies before comparing a new
    # snapshot with the cache; a corrupt cache must not imply event deletion.
    from .google_calendar import valid_google_resource_identity

    if (any(not isinstance(value, str) or not value for value in (
            item.calendar_id, item.calendar_name, item.href, item.etag, item.data))
            or not valid_google_resource_identity(item.calendar_id, item.href)):
        raise _error()
    try:
        parsed = Calendar.from_ical(item.data)
        events = parsed.walk("VEVENT")
        uids = {str(event.get("UID", "")) for event in events}
        if (parsed.name != "VCALENDAR" or not events or len(uids) != 1 or "" in uids
                or any(child.name not in {"VEVENT", "VTIMEZONE"}
                       for child in parsed.subcomponents)):
            raise ValueError
    except Exception:
        raise _error() from None


def _cache(previous):
    if not isinstance(previous, list) or len(previous) > MAX_RESOURCES:
        raise _error()
    cache = {}
    total = 0
    for item in previous:
        if (not isinstance(item, CachedResource) or item.source_id not in _SOURCES
                or any(not isinstance(value, str) or not value for value in (
                    item.calendar_id, item.calendar_name, item.href, item.etag, item.data))):
            raise _error()
        href, _, _ = _url(item.href)
        if href != item.href or _key(item) in cache:
            raise _error()
        total += len(item.data.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise CollectionLimitError("Общий объём календарей превышает лимит; обновление остановлено.")
        if item.source_id == "google":
            _validate_google_resource(item)
        cache[_key(item)] = item
    return cache


def sync_calendars(previous: list[CachedResource]) -> SyncResult:
    """Return a complete replacement cache or fail without any partial result."""
    resources = []
    calendar_count = fetched = total_bytes = 0
    seen_calendars = set()
    with _quiet_library_logs():
        try:
            old = _cache(previous)
            sources = [
                ("icloud", Credentials.from_environment(), _icloud_client),
                *[(f"mailru_{number}", MailRuCredentials.from_environment(number), _mailru_client)
                  for number in (1, 2, 3)],
            ]
            for source_id, credentials, connect in sources:
                with connect(credentials) as client:
                    client.timeout = COLLECTION_TIMEOUT_SECONDS
                    client.session.hooks.setdefault("response", []).append(_guard_response)
                    for calendar in client.get_calendars():
                        if "VEVENT" not in calendar.get_supported_components():
                            continue
                        calendar_id = calendar.id
                        if not isinstance(calendar_id, str) or not calendar_id:
                            raise _error()
                        identity = source_id, calendar_id
                        if identity in seen_calendars:
                            raise _error()
                        seen_calendars.add(identity)
                        calendar_count += 1
                        name = calendar.get_display_name() or "Без названия"
                        if not isinstance(name, str):
                            raise _error()
                        calendar_url, _, _ = _url(str(calendar.url))
                        inventory = _inventory(client, calendar_url)
                        if len(resources) + len(inventory) > MAX_RESOURCES:
                            raise CollectionLimitError("Число записей календарей превышает лимит; обновление остановлено.")
                        needed = [href for href, etag in inventory.items()
                                  if (source_id, calendar_id, href) not in old
                                  or old[source_id, calendar_id, href].etag != etag]
                        needed_set = set(needed)
                        total_bytes += sum(len(old[source_id, calendar_id, href].data.encode("utf-8"))
                                           for href in inventory if href not in needed_set)
                        if total_bytes > MAX_TOTAL_BYTES:
                            raise CollectionLimitError("Общий объём календарей превышает лимит; обновление остановлено.")
                        changed_bodies = {}
                        for offset in range(0, len(needed), MULTIGET_BATCH_SIZE):
                            batch = {href: inventory[href]
                                     for href in needed[offset:offset + MULTIGET_BATCH_SIZE]}
                            bodies = _multiget(client, calendar_url, batch)
                            total_bytes += sum(len(data.encode("utf-8")) for data in bodies.values())
                            if total_bytes > MAX_TOTAL_BYTES:
                                raise CollectionLimitError("Общий объём календарей превышает лимит; обновление остановлено.")
                            fetched += len(bodies)
                            changed_bodies.update(bodies)
                        for href, etag in inventory.items():
                            prior = old.get((source_id, calendar_id, href))
                            data = changed_bodies[href] if href in changed_bodies else prior.data
                            resources.append(CachedResource(source_id, calendar_id, name, href, etag, data))
            if google_enabled():
                from .google_calendar import collect_google

                google = collect_google()
                calendar_count += google.calendar_count
                for item in google.resources:
                    if len(resources) >= MAX_RESOURCES:
                        raise CollectionLimitError("Число записей календарей превышает лимит; обновление остановлено.")
                    total_bytes += len(item.data.encode("utf-8"))
                    if total_bytes > MAX_TOTAL_BYTES:
                        raise CollectionLimitError("Общий объём календарей превышает лимит; обновление остановлено.")
                    cached = CachedResource("google", item.calendar_id, item.calendar_name,
                                            item.href, item.etag, item.data)
                    _validate_google_resource(cached)
                    resources.append(cached)
                # This adapter downloads a complete Google snapshot even when
                # its ETags are unchanged. Report that work separately from
                # actual changes, which determine whether to replan reminders.
                fetched += len(google.resources)
            current = {_key(item): item for item in resources}
            if len(current) != len(resources):
                raise _error()
            deleted = len(old.keys() - current.keys())
            changed = deleted + sum(old.get(key) != value for key, value in current.items())
            return SyncResult(resources, calendar_count, changed, fetched, deleted)
        except CalendarConnectionError:
            raise
        except Exception:
            raise _error() from None


def to_calendar_resources(resources: list[CachedResource]) -> list[CalendarResource]:
    """Push policies include all invitations; account email is unnecessary."""
    return [CalendarResource(item.source_id, item.calendar_id, item.calendar_name, "", item.data)
            for item in resources]
