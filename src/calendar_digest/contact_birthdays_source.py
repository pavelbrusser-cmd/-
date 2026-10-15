"""Request the name, UID and birthday fields of iCloud CardDAV contacts.

Some servers return extra fields or contacts despite the requested projection
and filter; extra fields are discarded. No address book is modified and no raw
vCard is returned or persisted. A failed
or incomplete response aborts the whole collection. The stored birthday omits
the year, including Apple's placeholder year for contacts without a known year.
"""

from dataclasses import dataclass, field
from datetime import date
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import niquests

from .calendar_collection import _quiet_library_logs
from .contact_identity import canonical_contact_book_id
from .icloud import Credentials
from .readonly_caldav import CalendarConnectionError, connect_readonly


_DAV = "{DAV:}"
_CARD = "{urn:ietf:params:xml:ns:carddav}"
_HOST = r"(?:contacts|p[0-9]+-contacts)\.icloud\.com"
_ROOT = "https://contacts.icloud.com/"
MAX_CONTACTS = 10_000
MAX_ADDRESSBOOKS = 32
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class ContactBirthday:
    book_id: str
    uid: str = field(repr=False)
    name: str = field(repr=False)
    month: int
    day: int


@dataclass(frozen=True)
class BirthdayCollection:
    contacts: list[ContactBirthday]
    addressbooks: int


def _error():
    return CalendarConnectionError(
        "Не удалось полностью прочитать дни рождения из контактов iCloud; обновление остановлено."
    )


def _url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not re.fullmatch(_HOST, parsed.hostname or "")
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or re.search(r"[\x00-\x20\x7f\\]", value)):
            raise ValueError
        decoded = unquote(parsed.path, errors="strict")
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
        return niquests.Request("GET", urlunsplit((
            "https", parsed.hostname.lower(), parsed.path or "/", "", "",
        ))).prepare().url
    except (TypeError, ValueError, UnicodeError, niquests.exceptions.RequestException):
        raise _error() from None


def _join(base, href):
    if not isinstance(href, str) or not href:
        raise _error()
    return _url(urljoin(base, href))


def _same_resource(first, second):
    a, b = urlsplit(first), urlsplit(second)
    return a.hostname == b.hostname and unquote(a.path).rstrip("/") == unquote(b.path).rstrip("/")


def _member(base, href):
    url = _join(base.rstrip("/") + "/", href)
    root, item = urlsplit(base), urlsplit(url)
    root_path, path = unquote(root.path).rstrip("/"), unquote(item.path)
    if root.hostname != item.hostname:
        raise _error()
    if path.rstrip("/") == root_path:
        return None
    if not path.startswith(root_path + "/"):
        raise _error()
    if "/" in path[len(root_path) + 1:].rstrip("/"):
        raise _error()
    return url


def _xml(content):
    if b"\x00" in content or re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", content, re.I):
        raise _error()
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise _error() from None
    if root.tag != _DAV + "multistatus" or any(item.tag != _DAV + "response" for item in root):
        raise _error()
    return root


def _request(client, method, url, body, *, depth=None, budget):
    url = _url(url)
    headers = {"Content-Type": "application/xml; charset=utf-8"}
    if depth is not None:
        headers["Depth"] = str(depth)
    response = client.session.request(
        method, url, data=body, headers=headers, auth=client.auth,
        timeout=TIMEOUT_SECONDS, verify=True, allow_redirects=True, stream=True,
    )
    try:
        if response.status_code != 207:
            raise _error()
        actual_url = _url(response.url or url)
        body_bytes = bytearray()
        for chunk in response.iter_content(chunk_size=65_536):
            body_bytes.extend(chunk)
            budget[0] += len(chunk)
            if len(body_bytes) > MAX_RESPONSE_BYTES or budget[0] > MAX_TOTAL_BYTES:
                raise _error()
        return _xml(bytes(body_bytes)), actual_url
    finally:
        response.close()


def _status(node):
    parts = (node.text or "").split() if node is not None else []
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise _error()
    return int(parts[1])


def _records(root, base):
    seen = set()
    for item in root:
        hrefs = item.findall(_DAV + "href")
        if len(hrefs) != 1:
            raise _error()
        href = _join(base, hrefs[0].text)
        identity = urlsplit(href).hostname, unquote(urlsplit(href).path).rstrip("/")
        if identity in seen or len(seen) >= MAX_CONTACTS + MAX_ADDRESSBOOKS + 1:
            raise _error()
        seen.add(identity)
        direct = item.findall(_DAV + "status")
        if len(direct) > 1 or (direct and not 200 <= _status(direct[0]) < 300):
            raise _error()
        properties = {}
        parts = item.findall(_DAV + "propstat")
        if not parts:
            raise _error()
        for part in parts:
            status = _status(part.find(_DAV + "status"))
            props = part.findall(_DAV + "prop")
            if len(props) != 1 or (not 200 <= status < 300 and status != 404):
                raise _error()
            for prop in props[0]:
                if prop.tag in properties:
                    raise _error()
                properties[prop.tag] = prop if 200 <= status < 300 else None
        yield href, properties


def _propfind(client, url, name, *, depth=0, budget):
    query = ET.Element(_DAV + "propfind")
    props = ET.SubElement(query, _DAV + "prop")
    ET.SubElement(props, name)
    return _request(client, "PROPFIND", url, ET.tostring(query, encoding="utf-8"), depth=depth, budget=budget)


def _href_property(client, url, name, *, budget):
    root, actual_url = _propfind(client, url, name, budget=budget)
    records = list(_records(root, actual_url))
    if len(records) != 1 or not _same_resource(records[0][0], actual_url):
        raise _error()
    prop = records[0][1].get(name)
    hrefs = [] if prop is None else prop.findall(_DAV + "href")
    if len(hrefs) != 1:
        raise _error()
    return _join(actual_url, hrefs[0].text)


def _books(client, home, *, budget):
    root, actual_url = _propfind(client, home, _DAV + "resourcetype", depth=1, budget=budget)
    result = []
    home_seen = False
    for href, props in _records(root, actual_url):
        kind = props.get(_DAV + "resourcetype")
        if kind is None:
            raise _error()
        member = _member(actual_url, href)
        if member is None:
            if home_seen or kind.find(_DAV + "collection") is None:
                raise _error()
            home_seen = True
        elif kind.find(_CARD + "addressbook") is not None:
            if kind.find(_DAV + "collection") is None or len(result) >= MAX_ADDRESSBOOKS:
                raise _error()
            result.append(member)
    if not home_seen:
        raise _error()
    return result


def _text(value):
    result = []
    index = 0
    escapes = {"n": "\n", "N": "\n", "\\": "\\", ",": ",", ";": ";"}
    while index < len(value):
        if value[index] == "\\":
            index += 1
            if index == len(value) or value[index] not in escapes:
                raise _error()
            result.append(escapes[value[index]])
        else:
            result.append(value[index])
        index += 1
    return "".join(result)


def _property(line):
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == ":" and not quoted:
            header = line[:index]
            name = header.split(";", 1)[0].rsplit(".", 1)[-1].upper()
            if not re.fullmatch(r"[A-Z0-9-]+", name):
                raise _error()
            return name, header, line[index + 1:]
    raise _error()


def _birthday(vcard, book_id):
    if not isinstance(vcard, str) or not vcard or "\x00" in vcard:
        raise _error()
    unfolded = []
    for line in vcard.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith((" ", "\t")):
            if not unfolded:
                raise _error()
            unfolded[-1] += line[1:]
        elif line:
            unfolded.append(line)
    if (len(unfolded) < 4 or unfolded[0].upper() != "BEGIN:VCARD"
            or unfolded[-1].upper() != "END:VCARD"):
        raise _error()
    wanted = {}
    for line in unfolded[1:-1]:
        name, header, value = _property(line)
        if name in {"BEGIN", "END"}:
            raise _error()
        if name in {"VERSION", "UID", "FN", "BDAY"}:
            if name in wanted or re.search(r";ENCODING=", header, re.I):
                raise _error()
            if name == "BDAY":
                value_types = re.findall(r'(?:^|;)VALUE=(?:"([^"]*)"|([^;]*))', header, re.I)
                if (len(value_types) > 1 or any((quoted or plain).casefold() not in
                        {"date", "date-time", "date-and-or-time"}
                        for quoted, plain in value_types)):
                    # A textual birthday can look like an ISO date; treating
                    # it as a date would silently invent the contact's intent.
                    raise _error()
            wanted[name] = value
    if wanted.get("VERSION") not in {"3.0", "4.0"} or "FN" not in wanted:
        raise _error()
    name = " ".join(_text(wanted["FN"]).split())
    if not name or len(name) > 4096:
        raise _error()
    # iCloud can return contacts without BDAY despite an existence filter.
    # These are not reminders, but they still consume the collection limits.
    if "BDAY" not in wanted:
        return None
    if "UID" not in wanted:
        raise _error()
    uid = _text(wanted["UID"]).strip()
    raw = wanted["BDAY"].strip()
    match = re.fullmatch(r"(?:\d{4}-|--)(\d{2})-(\d{2})", raw)
    if match is None:
        match = re.fullmatch(r"(?:\d{4}|--)(\d{2})(\d{2})", raw)
    if not uid or match is None or len(uid) > 1024:
        raise _error()
    month, day = map(int, match.groups())
    try:
        date(2000, month, day)
    except ValueError:
        raise _error() from None
    return ContactBirthday(book_id, uid, name, month, day)


def _query_birthdays(client, book, *, budget):
    query = ET.Element(_CARD + "addressbook-query")
    props = ET.SubElement(query, _DAV + "prop")
    data = ET.SubElement(props, _CARD + "address-data")
    for name in ("VERSION", "UID", "FN", "BDAY"):
        ET.SubElement(data, _CARD + "prop", name=name)
    filters = ET.SubElement(query, _CARD + "filter", test="allof")
    # An empty prop-filter requires this property to be defined. CardDAV has
    # is-not-defined, but no is-defined element (RFC 6352 section 10.5.1).
    ET.SubElement(filters, _CARD + "prop-filter", name="BDAY")
    return _request(client, "REPORT", book, ET.tostring(query, encoding="utf-8"), depth=1, budget=budget)


def collect_contact_birthdays(credentials=None) -> BirthdayCollection:
    """Collect every projected birthday or fail without returning partial data."""
    with _quiet_library_logs():
        try:
            credentials = credentials if credentials is not None else Credentials.from_environment()
            contacts = []
            cards_read = 0
            identities = set()
            budget = [0]
            with connect_readonly(
                username=credentials.username, password=credentials.app_password,
                url=_ROOT, hostname_pattern=_HOST, provider="Контакты iCloud",
            ) as client:
                principal = _href_property(client, _ROOT, _DAV + "current-user-principal", budget=budget)
                home = _href_property(client, principal, _CARD + "addressbook-home-set", budget=budget)
                books = _books(client, home, budget=budget)
                for book in books:
                    root, actual_book = _query_birthdays(client, book, budget=budget)
                    # Routing hosts and URL spelling are not address-book
                    # identity. Use the final validated collection URL after
                    # redirects so pending work and receipts survive aliases.
                    book_id = canonical_contact_book_id(actual_book)
                    for href, props in _records(root, actual_book):
                        if _member(actual_book, href) is None:
                            raise _error()
                        data = props.get(_CARD + "address-data")
                        if data is None or not data.text or cards_read >= MAX_CONTACTS:
                            raise _error()
                        cards_read += 1
                        contact = _birthday(data.text, book_id)
                        if contact is None:
                            continue
                        identity = contact.book_id, contact.uid
                        if identity in identities:
                            raise _error()
                        identities.add(identity)
                        contacts.append(contact)
                return BirthdayCollection(contacts, len(books))
        except CalendarConnectionError:
            raise
        except Exception:
            raise _error() from None
