"""Read the authorised Google primary calendar as complete iCalendar series.

Only two fixed Calendar API endpoints are read. Returned links are never
followed. Masters and exceptions are collected without a date filter so an
exception moved out of the digest window remains visible to selection.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import time
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, vCalAddress
import niquests

from .google_auth import GoogleAuthError, GoogleCredentials
from .readonly_caldav import CalendarConnectionError


API_ROOT = "https://www.googleapis.com/calendar/v3/calendars/primary"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_EVENTS = 10_000
MAX_PAGES = 100
COLLECTION_TIMEOUT_SECONDS = 300
PAGE_SIZE = 250
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_RECURRENCE_PROPERTIES = {"RRULE", "EXRULE", "RDATE", "EXDATE"}
_PARTSTAT = {
    "accepted": "ACCEPTED", "declined": "DECLINED",
    "tentative": "TENTATIVE", "needsAction": "NEEDS-ACTION",
}


@dataclass(frozen=True)
class GoogleResource:
    calendar_id: str = field(repr=False)
    calendar_name: str = field(repr=False)
    account_email: str = field(repr=False)
    data: str = field(repr=False)
    href: str = field(repr=False)
    etag: str


@dataclass(frozen=True)
class GoogleCollection:
    resources: list[GoogleResource]
    calendar_count: int


def _invalid():
    return CalendarConnectionError(
        "Google Календарь вернул неполные или некорректные данные; сбор остановлен."
    )


def _limit():
    return CalendarConnectionError(
        "Данные Google Календаря превышают допустимый предел; сбор остановлен."
    )


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _href(calendar_id, master_id):
    return (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{_digest(calendar_id)}/events/{_digest(master_id)}.ics"
    )


def valid_google_resource_identity(calendar_id, href):
    """Validate our synthetic cache identity; this URL is never requested."""
    if (not isinstance(calendar_id, str) or not isinstance(href, str)
            or len(calendar_id) > 254 or calendar_id != calendar_id.casefold()
            or re.fullmatch(r"[^\s@/\\\x00-\x1f\x7f]+@[^\s@/\\\x00-\x1f\x7f]+", calendar_id) is None):
        return False
    prefix = (
        "https://www.googleapis.com/calendar/v3/calendars/"
        f"{_digest(calendar_id)}/events/"
    )
    return href.startswith(prefix) and re.fullmatch(r"[0-9a-f]{64}\.ics", href[len(prefix):]) is not None


def _text(value, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise _invalid()
    if "\x00" in value:
        raise _invalid()
    return value


def _identifier(value):
    value = _text(value, required=True)
    if len(value) > 1024 or re.search(r"[\x00-\x20\x7f]", value):
        raise _invalid()
    return value


def _timestamp(value):
    value = _text(value, required=True)
    if "T" not in value:
        raise _invalid()
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise _invalid()
    return result


def _temporal(value, *, recurring=False):
    if not isinstance(value, dict) or ("date" in value) == ("dateTime" in value):
        raise _invalid()
    if "date" in value:
        raw = _text(value["date"], required=True)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            raise _invalid()
        return date.fromisoformat(raw)
    raw = _text(value["dateTime"], required=True)
    if "T" not in raw:
        raise _invalid()
    instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    zone_name = value.get("timeZone")
    if zone_name is not None:
        zone = ZoneInfo(_text(zone_name, required=True))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=zone)
            # A nonexistent wall time must not silently jump across a DST gap.
            if instant.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != instant.replace(tzinfo=None):
                raise _invalid()
        else:
            # RFC3339 may encode this instant in UTC while timeZone specifies
            # the local zone used to expand recurrence. Keep both meanings.
            instant = instant.astimezone(zone)
        # TZID wall times cannot express the second occurrence of an ambiguous
        # hour. Preserve its instant in UTC for a one-off, reject a master that
        # cannot be represented faithfully as a recurring local wall time.
        if instant.fold:
            if recurring:
                raise _invalid()
            instant = instant.astimezone(timezone.utc)
    elif recurring or instant.tzinfo is None:
        # Google requires the recurrence expansion zone on recurring masters.
        raise _invalid()
    else:
        # datetime.timezone(+03:00) would be emitted as the non-IANA TZID
        # "UTC+03:00". UTC keeps the instant portable through ICS parsing.
        instant = instant.astimezone(timezone.utc)
    return instant


def _same_type(left, right):
    return isinstance(left, datetime) == isinstance(right, datetime)


def _check_interval(start, end):
    if not _same_type(start, end):
        raise _invalid()
    if isinstance(start, datetime):
        if end.astimezone(timezone.utc) < start.astimezone(timezone.utc):
            raise _invalid()
    elif end <= start:
        raise _invalid()


def _end_from_master(start, master_interval):
    old_start, old_end = master_interval
    if not _same_type(start, old_start):
        raise _invalid()
    if isinstance(start, datetime):
        duration = old_end.astimezone(timezone.utc) - old_start.astimezone(timezone.utc)
        return (start.astimezone(timezone.utc) + duration).astimezone(old_start.tzinfo)
    return start + (old_end - old_start)


def _add_recurrence(event, lines, start):
    if not isinstance(lines, list) or not lines or len(lines) > 1000:
        raise _invalid()
    seen_rules = set()
    for line in sorted(lines):
        if not isinstance(line, str) or "\r" in line or "\n" in line or "\x00" in line:
            raise _invalid()
        key = line.split(";", 1)[0].split(":", 1)[0].upper()
        if key not in _RECURRENCE_PROPERTIES:
            raise _invalid()
        parsed = Event.from_ical(f"BEGIN:VEVENT\r\n{line}\r\nEND:VEVENT\r\n")
        if parsed.errors or len(parsed) != 1 or key not in parsed:
            raise _invalid()
        prop = parsed[key]
        if key in {"RRULE", "EXRULE"}:
            if key in seen_rules or not prop.get("FREQ"):
                raise _invalid()
            seen_rules.add(key)
        else:
            for item in prop.dts:
                # Google date lists are points, not RFC5545 PERIOD values.
                if not isinstance(item.dt, (date, datetime)) or not _same_type(start, item.dt):
                    raise _invalid()
                if isinstance(item.dt, datetime) and item.dt.tzinfo is None:
                    raise _invalid()
        event.add(key, prop)


def _add_people(event, raw, *, account_email):
    organizer = raw.get("organizer")
    if organizer is not None:
        if not isinstance(organizer, dict):
            raise _invalid()
        if organizer.get("email"):
            address = vCalAddress("mailto:" + _identifier(organizer["email"]))
            if organizer.get("displayName"):
                address.params["CN"] = _text(organizer["displayName"])
            event.add("organizer", address)
    attendees = raw.get("attendees", [])
    if not isinstance(attendees, list) or raw.get("attendeesOmitted"):
        raise _invalid()
    prepared = []
    self_count = 0
    for attendee in attendees:
        if not isinstance(attendee, dict):
            raise _invalid()
        if "self" in attendee and type(attendee["self"]) is not bool:
            raise _invalid()
        is_self = attendee.get("self", False)
        self_count += int(is_self)
        if self_count > 1:
            raise _invalid()
        email = _identifier(attendee.get("email"))
        status = attendee.get("responseStatus", "needsAction")
        if status not in _PARTSTAT:
            raise _invalid()
        # Google identifies the current attendee even when an invitation was
        # addressed to an account alias. This ICS is only our read model, so
        # use the verified primary identity for the shared participation logic.
        participant_email = account_email if is_self else email
        address = vCalAddress("mailto:" + participant_email)
        if is_self and email.casefold() != account_email.casefold():
            address.params["X-GOOGLE-EMAIL"] = email
        address.params["PARTSTAT"] = _PARTSTAT[status]
        if attendee.get("displayName"):
            address.params["CN"] = _text(attendee["displayName"])
        if attendee.get("optional"):
            address.params["ROLE"] = "OPT-PARTICIPANT"
        if attendee.get("resource"):
            address.params["CUTYPE"] = "RESOURCE"
        prepared.append(address)
    for address in sorted(prepared, key=lambda item: (str(item).casefold(), item.to_ical())):
        event.add("attendee", address)


def _component(raw, uid, *, account_email, master_interval=None):
    status = raw.get("status", "confirmed")
    if status not in {"confirmed", "tentative", "cancelled"}:
        raise _invalid()
    is_override = "recurringEventId" in raw
    if not is_override and "originalStartTime" in raw:
        raise _invalid()
    recurrence = raw.get("recurrence")
    if is_override and recurrence:
        raise _invalid()
    rid = _temporal(raw["originalStartTime"]) if is_override else None
    if is_override and master_interval is not None and not _same_type(rid, master_interval[0]):
        raise _invalid()
    if "start" in raw:
        start = _temporal(raw["start"], recurring=bool(recurrence))
    elif status == "cancelled" and rid is not None:
        start = rid
    else:
        raise _invalid()
    if "end" in raw:
        end = _temporal(raw["end"])
    elif status == "cancelled":
        if master_interval is not None:
            end = _end_from_master(start, master_interval)
        else:
            end = start if isinstance(start, datetime) else start + timedelta(days=1)
    else:
        raise _invalid()
    _check_interval(start, end)
    event = Event()
    event.add("uid", uid)
    event.add("dtstamp", _timestamp(raw["updated"]).astimezone(timezone.utc) if raw.get("updated") else _EPOCH)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("status", status.upper())
    if rid is not None:
        event.add("recurrence-id", rid)
    if recurrence:
        _add_recurrence(event, recurrence, start)
    elif recurrence is not None and recurrence != []:
        raise _invalid()
    for source, target in (("summary", "summary"), ("description", "description"), ("location", "location")):
        if source in raw:
            event.add(target, _text(raw[source], required=False) or "")
    for source, target in (("created", "created"), ("updated", "last-modified")):
        if source in raw:
            event.add(target, _timestamp(raw[source]).astimezone(timezone.utc))
    if "sequence" in raw:
        sequence = raw["sequence"]
        if type(sequence) is not int or sequence < 0:
            raise _invalid()
        event.add("sequence", sequence)
    if raw.get("iCalUID"):
        event.add("x-google-icaluid", _text(raw["iCalUID"], required=True))
    if raw.get("eventType") == "birthday":
        event.add("categories", ["birthdays"])
    if raw.get("transparency") in {"opaque", "transparent"}:
        event.add("transp", raw["transparency"].upper())
    _add_people(event, raw, account_email=account_email)
    return event, (start, end)


def _resources(items, *, calendar_id, calendar_name, account_email):
    records = {}
    groups = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise _invalid()
        event_id = _identifier(raw.get("id"))
        if event_id in records:
            raise _invalid()
        records[event_id] = raw
        master_id = _identifier(raw["recurringEventId"]) if "recurringEventId" in raw else event_id
        groups.setdefault(master_id, []).append(raw)
    resources = []
    total = 0
    for master_id, members in sorted(groups.items()):
        master = records.get(master_id)
        if master is None:
            # Deleted-instance tombstones may outlive an already deleted
            # series. Active exceptions without a master are not a snapshot.
            if any(raw.get("status") != "cancelled" for raw in members):
                raise _invalid()
            continue
        if "recurringEventId" in master:
            raise _invalid()
        if master.get("status") == "cancelled" and "start" not in master:
            # Google guarantees only id for deleted standalone events. A
            # deleted master removes its whole series, including stale children.
            continue
        uid = "google-" + _digest(calendar_id + "\x00" + master_id) + "@calendar-digest.local"
        component, interval = _component(master, uid, account_email=account_email)
        calendar = Calendar()
        calendar.add("version", "2.0")
        calendar.add("prodid", "-//Calendar Digest//Google Calendar Read Only//EN")
        calendar.add_component(component)
        seen_rids = set()
        children = [raw for raw in members if raw is not master]
        if children and master.get("status") != "cancelled" and not master.get("recurrence"):
            raise _invalid()
        if master.get("status") != "cancelled":
            for raw in sorted(children, key=lambda item: item["id"]):
                exception, _ = _component(raw, uid, account_email=account_email, master_interval=interval)
                rid = exception["RECURRENCE-ID"].dt
                identity = rid.astimezone(timezone.utc).isoformat() if isinstance(rid, datetime) else rid.isoformat()
                if identity in seen_rids:
                    raise _invalid()
                seen_rids.add(identity)
                calendar.add_component(exception)
        data_bytes = calendar.to_ical()
        total += len(data_bytes)
        if total > MAX_TOTAL_BYTES:
            raise _limit()
        resources.append(GoogleResource(
            calendar_id=calendar_id, calendar_name=calendar_name, account_email=account_email,
            data=data_bytes.decode("utf-8"), href=_href(calendar_id, master_id),
            etag='"' + hashlib.sha256(data_bytes).hexdigest() + '"',
        ))
    return resources


class _Reader:
    def __init__(self, session, token):
        self.session = session
        self.token = token
        self.total_bytes = 0
        self.deadline = time.monotonic() + COLLECTION_TIMEOUT_SECONDS

    def read(self, url, *, params=None):
        if url not in {API_ROOT, API_ROOT + "/events"}:
            raise _invalid()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _limit()
        with self.session.get(
            url, params=params,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/json"},
            timeout=(min(10, remaining), min(45, remaining)),
            allow_redirects=False, verify=True, stream=True,
        ) as response:
            if response.status_code in {401, 403}:
                raise CalendarConnectionError("Google Календарь отказал в доступе; проверьте подключение аккаунта.")
            if response.status_code != 200:
                raise CalendarConnectionError("Не удалось полностью прочитать Google Календарь; сбор остановлен.")
            content_length = response.headers.get("Content-Length")
            if content_length is not None and int(content_length) > MAX_RESPONSE_BYTES:
                raise _limit()
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if not isinstance(chunk, bytes):
                    raise _invalid()
                content.extend(chunk)
                self.total_bytes += len(chunk)
                if (len(content) > MAX_RESPONSE_BYTES or self.total_bytes > MAX_TOTAL_BYTES
                        or time.monotonic() > self.deadline):
                    raise _limit()
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise _invalid()
            return payload


def collect_google(credentials=None) -> GoogleCollection:
    """Read one verified primary account, or fail without a partial result."""
    try:
        credentials = credentials if credentials is not None else GoogleCredentials.from_environment()
        token = credentials.access_token()
        if not isinstance(token, str) or not token or re.search(r"[\x00-\x20\x7f]", token):
            raise _invalid()
        expected_email = _identifier(credentials.expected_email).casefold()
        with niquests.Session() as session:
            reader = _Reader(session, token)
            metadata = reader.read(API_ROOT)
            calendar_id = _identifier(metadata.get("id")).casefold()
            if calendar_id != expected_email:
                raise CalendarConnectionError("Подключён другой Google аккаунт; чтение календаря остановлено.")
            calendar_name = _text(metadata.get("summary")) or "Google Calendar"
            items = []
            page_token = None
            seen_tokens = set()
            for _ in range(MAX_PAGES):
                params = {
                    "singleEvents": "false", "showDeleted": "true",
                    "showHiddenInvitations": "true", "maxResults": PAGE_SIZE,
                }
                if page_token is not None:
                    params["pageToken"] = page_token
                payload = reader.read(API_ROOT + "/events", params=params)
                if payload.get("kind") != "calendar#events" or "error" in payload:
                    raise _invalid()
                if payload.get("accessRole") not in {None, "owner", "writer", "reader"}:
                    raise _invalid()
                batch = payload.get("items", [])
                if not isinstance(batch, list):
                    raise _invalid()
                items.extend(batch)
                if len(items) > MAX_EVENTS:
                    raise _limit()
                page_token = payload.get("nextPageToken")
                if page_token is None:
                    break
                page_token = _text(page_token, required=True)
                if len(page_token) > 4096 or page_token in seen_tokens:
                    raise _invalid()
                seen_tokens.add(page_token)
            else:
                raise _limit()
            return GoogleCollection(
                resources=_resources(items, calendar_id=calendar_id, calendar_name=calendar_name,
                                     account_email=expected_email),
                calendar_count=1,
            )
    except GoogleAuthError as error:
        raise CalendarConnectionError(str(error)) from None
    except CalendarConnectionError:
        raise
    except Exception:
        raise CalendarConnectionError("Не удалось полностью прочитать Google Календарь; сбор остановлен.") from None
