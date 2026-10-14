#!/usr/bin/env python3
"""Profile create_preview through real CalDAV parsing, without network access.

Usage (run each code version in a fresh process):
  .venv/bin/python scripts/profile_preview_memory.py SNAPSHOT --day 2026-10-14
  .venv/bin/python scripts/profile_preview_memory.py SNAPSHOT --day 2026-10-14 \
      --source-root /tmp/baseline

The source root must contain src/calendar_digest. The input is read-only; all
preview outputs go to an automatically removed private temporary directory.
Calendar names and credentials are fictional. Only counts, a digest-event hash,
timings and memory measurements reach stdout; failures disclose only their type.

This is a conservative in-process simulation: the fake server retains another
copy of the complete current ICS inventory in the measured process. It excludes
the web server and does not model a provider's exact XML/compression/latency.
Only calendars represented by current resources can be discovered from a saved
snapshot. Synthetic account emails also mean real self-PARTSTAT matching is not
measured. Compare hashes between runs of THIS harness, not with a live digest.
Requires the project environment, including ijson, for both compared versions.
"""

from argparse import ArgumentParser
import base64
from collections import defaultdict
from datetime import date
import hashlib
import inspect
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import resource
import shutil
import socket
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import quote, unquote
import warnings
from xml.etree import ElementTree
from xml.sax.saxutils import escape


SOURCES = ("icloud", "mailru_1", "mailru_2", "mailru_3")
CALDAV = "{urn:ietf:params:xml:ns:caldav}"


def _peak_mib():
    scale = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale, 2)


def _emit(payload):
    print(json.dumps(payload, sort_keys=True), flush=True)


def _dav_response(href, properties):
    return (f"<d:response><d:href>{escape(href)}</d:href><d:propstat>"
            f"<d:prop>{properties}</d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


def _multistatus(responses):
    # Assemble bytes once, avoiding a second complete Unicode REPORT string.
    with BytesIO() as stream:
        stream.write(b'<d:multistatus xmlns:d="DAV:" '
                     b'xmlns:c="urn:ietf:params:xml:ns:caldav">')
        for response in responses:
            stream.write(response.encode("utf-8"))
        stream.write(b"</d:multistatus>")
        return stream.getvalue()


def _calendar_properties(number):
    return ("<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
            f"<d:displayname>Fixture calendar {number}</d:displayname>"
            '<c:supported-calendar-component-set><c:comp name="VEVENT"/>'
            "</c:supported-calendar-component-set>")


class _OfflineTransport:
    def __init__(self, snapshot):
        import ijson

        self.groups = {source: defaultdict(list) for source in SOURCES}
        self.environment = {}
        self.accounts = {}
        self.calls = 0
        self.reports = 0
        for source in SOURCES:
            username = f"memory-fixture-{source}@example.invalid"
            password = "fictional-memory-fixture-password"
            prefix = "ICLOUD" if source == "icloud" else source.upper()
            self.environment[prefix + "_USERNAME"] = username
            self.environment[prefix + "_APP_PASSWORD"] = password
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.accounts["Basic " + token] = source
        # Do not let fixture loading itself create the legacy json.loads peak
        # which this profiler is intended to compare against streaming readers.
        with snapshot.open("rb") as stream:
            for item in ijson.items(stream, "current.item"):
                source = item["source_id"]
                calendar_id, data = item["calendar_id"], item["data"]
                if source not in self.groups or not isinstance(calendar_id, str) or not isinstance(data, str):
                    raise ValueError("invalid fixture resource")
                self.groups[source][calendar_id].append(data)

    def send(self, session, request, **kwargs):
        import niquests

        self.calls += 1
        if request.method not in {"PROPFIND", "REPORT"} or not kwargs.get("verify"):
            raise RuntimeError("fixture rejected transport operation")
        source = self.accounts[request.headers["Authorization"]]
        if source == "icloud":
            root = "https://caldav.icloud.com/"
            principal = "https://caldav.icloud.com/123/principal/"
            home = "https://caldav.icloud.com/123/calendars/"
        else:
            root = "https://calendar.mail.ru/principals/"
            principal = "https://calendar.mail.ru/principals/users/fixture/"
            home = "https://calendar.mail.ru/calendars/users/fixture/"
        calendars = {
            home + quote(unquote(identifier), safe="@") + "/": (index, rows)
            for index, (identifier, rows) in enumerate(self.groups[source].items(), 1)
        }
        if request.method == "PROPFIND":
            if request.url == root:
                entries = [_dav_response(root, "<d:current-user-principal>"
                    f"<d:href>{principal}</d:href></d:current-user-principal>")]
            elif request.url == principal:
                entries = [_dav_response(principal, "<c:calendar-home-set>"
                    f"<d:href>{home}</d:href></c:calendar-home-set>")]
            elif request.url == home:
                entries = (_dav_response(url, _calendar_properties(number))
                           for url, (number, _) in calendars.items())
            elif request.url in calendars:
                entries = [_dav_response(request.url, _calendar_properties(calendars[request.url][0]))]
            else:
                raise RuntimeError("unexpected fixture discovery destination")
        else:
            query = ElementTree.fromstring(request.body)
            if (query.find(f".//{CALDAV}comp-filter[@name='VEVENT']") is None
                    or query.find(f".//{CALDAV}time-range") is not None
                    or query.find(f".//{CALDAV}expand") is not None):
                raise RuntimeError("fixture requires complete unexpanded inventory")
            self.reports += 1
            _, rows = calendars[request.url]
            entries = (_dav_response(request.url + f"fixture-{index}.ics",
                        f"<c:calendar-data>{escape(data)}</c:calendar-data>"
                        '<d:getetag>"fictional-etag"</d:getetag>')
                       for index, data in enumerate(rows))
        response = niquests.Response()
        response.status_code = 207
        response.headers["Content-Type"] = "application/xml; charset=utf-8"
        response._content = _multistatus(entries)
        response._content_consumed = True
        response.encoding = "utf-8"
        response.url = request.url
        response.request = request
        response.reason = "Multi-Status"
        # Session.send is replaced outright: reproduce its response hook boundary.
        for hook in session.hooks.get("response", []):
            response = hook(response) or response
        return response


def _no_network(*args, **kwargs):
    raise RuntimeError("network disabled in memory profiler")


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--day", type=date.fromisoformat, required=True)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    started = time.monotonic()
    stage = "fixture"
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    try:
        source = args.source_root.resolve() / "src"
        if not (source / "calendar_digest" / "preview.py").is_file():
            raise ValueError("invalid source root")
        sys.path.insert(0, str(source))
        import niquests
        from calendar_digest.preview import create_preview

        transport = _OfflineTransport(args.snapshot)
        fixture_peak = _peak_mib()
        # Input is never rewritten. mkdtemp creates mode 0700; copyfile creates
        # the state under a temporary 0077 umask, so it is private from creation.
        previous_umask = os.umask(0o077)
        try:
            with tempfile.TemporaryDirectory(prefix="digest-memory-") as temporary:
                output = Path(temporary)
                shutil.copyfile(args.snapshot, output / "calendar-state.json")
                os.chmod(output / "calendar-state.json", 0o600)

                def progress(value):
                    nonlocal stage
                    stage = value.name
                    _emit({"stage": stage, "peak_rss_mib": _peak_mib(),
                           "elapsed_seconds": round(time.monotonic() - started, 3)})

                options = {}
                if "progress" in inspect.signature(create_preview).parameters:
                    options["progress"] = progress
                stage = "preview"
                preview_started = time.monotonic()
                # No Mock call history: retaining all requests would itself
                # distort memory usage. The transport never delegates outward.
                def send(session, request, **kwargs):
                    return transport.send(session, request, **kwargs)

                with (patch.dict(os.environ, transport.environment, clear=True),
                      patch.object(niquests.Session, "send", new=send),
                      patch.object(socket.socket, "connect", new=_no_network),
                      patch.object(socket.socket, "connect_ex", new=_no_network),
                      patch.object(socket, "create_connection", new=_no_network)):
                    result = create_preview(args.day, output, **options)
                elapsed = time.monotonic() - preview_started
                stage = "digest_hash"
                with (output / f"preview-{args.day.isoformat()}.json").open(encoding="utf-8") as stream:
                    events = json.load(stream)["events"]
                encoded = json.dumps(events, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")
                _emit({"status": "ok", "transport": "offline_in_process",
                       "extra_fixture_inventory_retained": True,
                       "fixture_peak_rss_mib": fixture_peak, "peak_rss_mib": _peak_mib(),
                       "preview_seconds": round(elapsed, 3),
                       "calendars": result["calendars"], "resources": result["resources"],
                       "events": len(events), "events_sha256": hashlib.sha256(encoded).hexdigest(),
                       "http_calls": transport.calls, "reports": transport.reports})
        finally:
            os.umask(previous_umask)
    except Exception as error:
        _emit({"status": "error", "stage": stage, "error_type": type(error).__name__,
               "peak_rss_mib": _peak_mib(),
               "elapsed_seconds": round(time.monotonic() - started, 3)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
