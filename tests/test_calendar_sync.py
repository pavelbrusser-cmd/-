"""Incremental calendar synchronization through real clients and fake HTTP."""

import base64
from dataclasses import replace
import logging
import os
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import niquests

from calendar_digest.calendar_collection import CollectionLimitError
from calendar_digest.calendar_sync import CachedResource, _guard_response, sync_calendars, to_calendar_resources
from calendar_digest.mailru import MailRuCredentials, _mailru_client
from calendar_digest.readonly_caldav import CalendarConnectionError
from test_calendar_collection import PASSWORD, RAW_SERIES, RAW_EXCEPTION, supported_properties
from test_icloud import dav_response, multistatus, response_for


class CalendarSyncTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("calendar_digest.calendar_sync.google_enabled", return_value=False))
        environment = {}
        self.accounts = {}
        for source, prefix in (("icloud", "ICLOUD"), ("mailru_1", "MAILRU_1"),
                               ("mailru_2", "MAILRU_2"), ("mailru_3", "MAILRU_3")):
            username = f"fictional-{source}@example.invalid"
            environment[prefix + "_USERNAME"] = username
            environment[prefix + "_APP_PASSWORD"] = PASSWORD
            token = base64.b64encode(f"{username}:{PASSWORD}".encode()).decode()
            self.accounts["Basic " + token] = source
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        transport = self.enterContext(patch.object(niquests.Session, "send", autospec=True))
        transport.side_effect = self.send
        self.calendars = {source: {"family": "Family"} for source in self.accounts.values()}
        self.records = {("icloud", "family"): {"event.ics": ('"v1"', RAW_SERIES)},
                        ("mailru_1", "family"): {"event.ics": ('"v1"', RAW_EXCEPTION)}}
        self.downloads = []
        self.inventories = []
        self.mutate = lambda kind, source, calendar, body: body
        self.fail_source = None
        self.http_status = None

    def send(self, session, request, **kwargs):
        source = self.accounts[request.headers["Authorization"]]
        if source == "icloud":
            root = "https://caldav.icloud.com/"
            principal = root + "123/principal/"
            home = root + "123/calendars/"
        else:
            root = "https://calendar.mail.ru/principals/"
            principal = root + "users/example/"
            home = "https://calendar.mail.ru/calendars/users/example/"
        self.assertTrue(kwargs["verify"])
        self.assertIn(request.method, {"PROPFIND", "REPORT"})
        query = ET.fromstring(request.body) if request.body else ET.Element("{DAV:}propfind")
        needs_etag = query.find("{DAV:}prop/{DAV:}getetag") is not None
        calendar = None
        if request.url == root:
            kind = "root"
            body = multistatus(dav_response(root,
                f"<d:current-user-principal><d:href>{principal}</d:href></d:current-user-principal>"))
        elif request.url == principal:
            kind = "principal"
            body = multistatus(dav_response(principal,
                f"<c:calendar-home-set><d:href>{home}</d:href></c:calendar-home-set>"))
        elif request.url == home:
            kind = "discovery"
            body = multistatus(*[dav_response(home + key + "/", supported_properties(name, "VEVENT"))
                for key, name in self.calendars[source].items()],
                dav_response(home + "tasks/", supported_properties("Tasks", "VTODO")))
        else:
            self.assertTrue(request.url.startswith(home))
            calendar = request.url[len(home):].strip("/")
            if request.method == "PROPFIND" and not needs_etag:
                kind = "properties"
                supported = "VTODO" if calendar == "tasks" else "VEVENT"
                body = multistatus(dav_response(request.url,
                    supported_properties(self.calendars[source].get(calendar, "Tasks"), supported)))
            elif request.method == "PROPFIND":
                kind = "inventory"
                self.assertNotEqual(calendar, "tasks")
                self.assertEqual(request.headers["Depth"], "1")
                self.assertFalse(kwargs["allow_redirects"])
                self.inventories.append((source, calendar))
                records = self.records.get((source, calendar), {})
                body = multistatus(
                    dav_response(request.url, "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"),
                    *[dav_response(request.url + filename,
                        f"<d:getetag>{escape(etag)}</d:getetag><d:resourcetype/>")
                      for filename, (etag, _) in records.items()])
            else:
                kind = "download"
                self.assertEqual(query.tag, "{urn:ietf:params:xml:ns:caldav}calendar-multiget")
                self.assertNotIn("Depth", request.headers)
                self.assertFalse(kwargs["allow_redirects"])
                filenames = [value.text.rsplit("/", 1)[-1] for value in query.findall("{DAV:}href")]
                self.downloads.append((source, calendar, filenames))
                records = self.records[source, calendar]
                body = multistatus(*[dav_response(request.url + filename,
                    f"<d:getetag>{escape(records[filename][0])}</d:getetag>"
                    f"<c:calendar-data>{escape(records[filename][1])}</c:calendar-data>")
                    for filename in filenames])
        if source == self.fail_source:
            raise RuntimeError(f"private-response {PASSWORD} {request.url}")
        body = self.mutate(kind, source, calendar, body)
        response = response_for(request, body, status=self.http_status or 207)
        for hook in session.hooks.get("response", []):
            response = hook(response) or response
        return response

    def test_initial_fetch_preserves_all_events_and_repeat_fetches_only_metadata(self):
        first = sync_calendars([])
        self.assertEqual((first.calendar_count, first.changed, first.fetched, first.deleted), (4, 2, 2, 0))
        self.assertEqual(len(self.inventories), 4)
        self.assertEqual(first.resources[0].data, RAW_SERIES.replace("\r\n", "\n"))
        self.assertEqual(first.resources[0].source_id, "icloud")
        self.assertNotIn("Fictional master", repr(first))
        self.downloads.clear()
        second = sync_calendars(first.resources)
        self.assertEqual(second.resources, first.resources)
        self.assertEqual((second.changed, second.fetched, second.deleted), (0, 0, 0))
        self.assertEqual(self.downloads, [])
        self.assertEqual(len(self.inventories), 8)

    def test_empty_propfind_discovery_body_is_allprop_with_real_client(self):
        credentials = MailRuCredentials("fictional-mailru_1@example.invalid", PASSWORD)
        with _mailru_client(credentials) as client:
            client.session.hooks.setdefault("response", []).append(_guard_response)
            response = client.request("https://calendar.mail.ru/principals/", "PROPFIND", "")
            self.assertEqual(response.status, 207)

    def test_empty_propfind_still_rejects_partial_records_and_empty_report_is_invalid(self):
        for method, status in (("PROPFIND", "403 Forbidden"), ("REPORT", "200 OK")):
            for empty in (None, "", b""):
                with self.subTest(method=method, body=empty):
                    request = niquests.Request(method, "https://calendar.mail.ru/principals/").prepare()
                    request.body = empty
                    response = response_for(request, multistatus(dav_response(
                        request.url, "<d:displayname>Fixture</d:displayname>", status)))
                    with self.assertRaises(CalendarConnectionError):
                        _guard_response(response)

    def test_only_changed_and_new_resources_are_downloaded(self):
        first = sync_calendars([])
        self.downloads.clear()
        self.records["icloud", "family"]["event.ics"] = ('"v2"', RAW_EXCEPTION)
        self.records["icloud", "family"]["new.ics"] = ('"new"', RAW_SERIES)
        second = sync_calendars(first.resources)
        self.assertEqual((second.changed, second.fetched, second.deleted), (2, 2, 0))
        self.assertEqual(self.downloads, [("icloud", "family", ["event.ics", "new.ics"])])

    def test_complete_deletion_and_calendar_removal_do_not_download(self):
        first = sync_calendars([])
        self.downloads.clear()
        del self.records["icloud", "family"]["event.ics"]
        del self.calendars["mailru_1"]["family"]
        second = sync_calendars(first.resources)
        self.assertEqual((len(second.resources), second.calendar_count, second.changed,
                          second.fetched, second.deleted), (0, 3, 2, 0, 2))
        self.assertEqual(self.downloads, [])

    def test_new_calendar_and_rename_cause_replanning(self):
        first = sync_calendars([])
        self.downloads.clear()
        self.calendars["icloud"]["family"] = "Renamed"
        self.calendars["mailru_3"]["new"] = "New calendar"
        self.records["mailru_3", "new"] = {"new.ics": ('"a"', RAW_EXCEPTION)}
        second = sync_calendars(first.resources)
        self.assertEqual((second.calendar_count, second.changed, second.fetched, second.deleted), (5, 2, 1, 0))
        self.assertEqual(second.resources[0].calendar_name, "Renamed")
        self.assertEqual(self.downloads, [("mailru_3", "new", ["new.ics"])])

    def test_multiget_batches_are_bounded(self):
        self.records["icloud", "family"] = {f"{n}.ics": ('"a"', RAW_EXCEPTION) for n in range(103)}
        result = sync_calendars([])
        self.assertEqual(result.fetched, 104)
        self.assertEqual([len(items) for source, _, items in self.downloads if source == "icloud"], [50, 50, 3])

    def test_total_byte_limit_stops_before_fetching_further_batches(self):
        self.records["icloud", "family"] = {f"{n}.ics": ('"a"', RAW_EXCEPTION) for n in range(103)}
        with patch("calendar_digest.calendar_sync.MAX_TOTAL_BYTES", 10):
            with self.assertRaises(CollectionLimitError):
                sync_calendars([])
        self.assertEqual(len(self.downloads), 1)

    def test_collection_etag_can_be_unavailable_without_hiding_members(self):
        def collection_without_etag(kind, source, calendar, body):
            if kind == "inventory":
                return body.replace("</d:propstat></d:response>",
                    '</d:propstat><d:propstat><d:prop><d:getetag/></d:prop>'
                    '<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response>', 1)
            return body
        self.mutate = collection_without_etag
        self.assertEqual(sync_calendars([]).fetched, 2)

    def test_icloud_member_resource_type_404_uses_etag_and_validated_event_body(self):
        def missing_member_type(kind, source, calendar, body):
            if kind == "inventory" and source == "icloud":
                return body.replace('<d:resourcetype/></d:prop><d:status>HTTP/1.1 200 OK</d:status>',
                    '</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>'
                    '<d:propstat><d:prop><d:resourcetype/></d:prop>'
                    '<d:status>HTTP/1.1 404 Not Found</d:status>')
            return body
        self.mutate = missing_member_type
        first = sync_calendars([])
        self.assertEqual(first.fetched, 2)
        second = sync_calendars(first.resources)
        self.assertEqual((second.changed, second.fetched), (0, 0))
        self.records["icloud", "family"]["event.ics"] = ('"v2"', 'not a calendar')
        with self.assertRaises(CalendarConnectionError):
            sync_calendars(first.resources)

    def test_missing_member_resource_type_alone_is_accepted_but_self_marker_is_required(self):
        self.mutate = lambda kind, source, cal, body: body.replace('<d:resourcetype/>', '') \
            if kind == "inventory" else body
        self.assertEqual(sync_calendars([]).fetched, 2)
        self.mutate = lambda kind, source, cal, body: body.replace(
            '<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>', '') \
            if kind == "inventory" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars([])

    def test_partial_inventory_never_turns_into_deletion(self):
        first = sync_calendars([])
        saved = list(first.resources)
        self.mutate = lambda kind, source, cal, body: body.replace("200 OK", "403 Forbidden") \
            if kind == "inventory" and source == "mailru_1" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars(first.resources)
        self.assertEqual(first.resources, saved)

    def test_missing_collection_marker_is_not_a_complete_empty_inventory(self):
        self.mutate = lambda kind, source, cal, body: multistatus() if kind == "inventory" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars([])

    def test_failed_calendar_discovery_cannot_be_silently_dropped(self):
        self.mutate = lambda kind, source, cal, body: body.replace("200 OK", "403 Forbidden") \
            if kind == "discovery" and source == "mailru_3" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars([])

    def test_missing_resource_type_in_discovery_cannot_drop_calendar(self):
        self.mutate = lambda kind, source, cal, body: body.replace("<d:resourcetype>", "<d:unknown>") \
            .replace("</d:resourcetype>", "</d:unknown>") if kind == "discovery" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars([])

    def test_provider_failure_and_auth_failures_hide_response_details(self):
        previous_level = logging.root.manager.disable
        for fail, status in (("mailru_3", None), (None, 401), (None, 403), (None, 302), (None, 500)):
            with self.subTest(status=status, source=fail):
                self.fail_source, self.http_status = fail, status
                with self.assertRaises(CalendarConnectionError) as error:
                    sync_calendars([])
                for private in (PASSWORD, "private-response", "https://", "example.invalid", "Fictional"):
                    self.assertNotIn(private, str(error.exception))
                self.assertEqual(logging.root.manager.disable, previous_level)

    def test_incomplete_multiget_fails_entire_cycle(self):
        self.mutate = lambda kind, source, cal, body: multistatus() if kind == "download" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars([])

    def test_changed_etag_during_multiget_fails_entire_cycle(self):
        self.mutate = lambda kind, source, cal, body: body.replace('"v1"', '"v2"') \
            if kind == "download" else body
        with self.assertRaises(CalendarConnectionError):
            sync_calendars([])

    def test_multiget_duplicate_extra_and_failed_records_are_rejected(self):
        variants = (
            lambda body: body.replace("</d:multistatus>", body[body.index("<d:response>"):body.index("</d:multistatus>")] + "</d:multistatus>"),
            lambda body: body.replace("event.ics", "unrequested.ics"),
            lambda body: body.replace("200 OK", "404 Not Found"),
            lambda body: body.replace("<c:calendar-data>", "<c:unknown>").replace("</c:calendar-data>", "</c:unknown>"),
        )
        for transform in variants:
            with self.subTest(transform=variants.index(transform)):
                self.mutate = lambda kind, source, cal, body: transform(body) if kind == "download" else body
                with self.assertRaises(CalendarConnectionError):
                    sync_calendars([])

    def test_bad_inventory_hrefs_never_reach_download(self):
        invalid = ["https://evil.example/private.ics", "http://caldav.icloud.com/123/calendars/family/event.ics",
                   "https://p42-caldav.icloud.com/123/calendars/family/event.ics",
                   "/123/calendars/other/event.ics", "/123/calendars/family/../other/event.ics",
                   "/123/calendars/family/%2e%2e/other/event.ics",
                   "/123/calendars/family/%252e%252e%252fother.ics", "event.ics?secret=x", "event.ics#fragment",
                   "https://user:password@caldav.icloud.com/123/calendars/family/event.ics"]
        original = "https://caldav.icloud.com/123/calendars/family/event.ics"
        for href in invalid:
            with self.subTest(href=href):
                self.mutate = lambda kind, source, cal, body: body.replace(original, escape(href)) \
                    if kind == "inventory" and source == "icloud" else body
                with self.assertRaises(CalendarConnectionError):
                    sync_calendars([])
                self.assertEqual(self.downloads, [])

    def test_relative_hrefs_are_supported(self):
        self.mutate = lambda kind, source, cal, body: body.replace(
            "https://caldav.icloud.com/123/calendars/family/event.ics", "event.ics") \
            if source == "icloud" else body
        result = sync_calendars([])
        self.assertEqual(result.fetched, 2)

    def test_duplicate_inventory_href_missing_etag_or_member_collection_type_fails(self):
        transforms = (
            lambda body: body.replace("</d:multistatus>", dav_response(
                "event.ics", '<d:getetag>"v1"</d:getetag><d:resourcetype/>') + "</d:multistatus>"),
            lambda body: body.replace("<d:getetag>", "<d:unknown>").replace("</d:getetag>", "</d:unknown>"),
            lambda body: body.replace("<d:resourcetype/>", "<d:resourcetype><d:collection/></d:resourcetype>"),
        )
        for transform in transforms:
            with self.subTest(transform=transforms.index(transform)):
                self.mutate = lambda kind, source, cal, body: transform(body) \
                    if kind == "inventory" and source == "icloud" else body
                with self.assertRaises(CalendarConnectionError):
                    sync_calendars([])

    def test_empty_etag_and_invalid_resource_body_are_safe_failures(self):
        for etag, data in (("", RAW_SERIES), ('"v1"', "private invalid body " + PASSWORD),
                           ('"v1"', "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:fake\r\n"
                            "END:VTODO\r\nEND:VCALENDAR\r\n")):
            with self.subTest(etag_empty=not etag):
                self.records["icloud", "family"]["event.ics"] = (etag, data)
                with self.assertRaises(CalendarConnectionError) as error:
                    sync_calendars([])
                self.assertNotIn(PASSWORD, str(error.exception))

    def test_resource_total_and_response_limits_fail_instead_of_truncation(self):
        for constant, value in (("MAX_RESOURCES", 1), ("MAX_TOTAL_BYTES", 1), ("MAX_RESPONSE_BYTES", 1)):
            with self.subTest(constant=constant), patch("calendar_digest.calendar_sync." + constant, value):
                with self.assertRaises(CollectionLimitError):
                    sync_calendars([])

    def test_dtd_and_multistatus_error_marker_are_rejected(self):
        transforms = (
            lambda body: '<!DOCTYPE root [<!ENTITY private "private data">]>' + body,
            lambda body: body.replace("</d:multistatus>", "<d:error><d:number-of-matches-within-limits/></d:error></d:multistatus>"),
            lambda body: "private invalid XML " + PASSWORD,
        )
        for transform in transforms:
            with self.subTest(transform=transforms.index(transform)):
                self.mutate = lambda kind, source, cal, body: transform(body) if kind == "inventory" else body
                with self.assertRaises(CalendarConnectionError) as error:
                    sync_calendars([])
                self.assertNotIn(PASSWORD, str(error.exception))

    def test_existing_cache_is_validated_and_does_not_leak_raw_data(self):
        first = sync_calendars([])
        for cache in ([first.resources[0], first.resources[0]], [replace(first.resources[0], source_id="unknown")],
                      [replace(first.resources[0], href="http://evil.invalid/private")]):
            with self.subTest(items=len(cache)):
                with self.assertRaises(CalendarConnectionError):
                    sync_calendars(cache)

    def test_conversion_keeps_event_data_and_drops_account_identity(self):
        result = sync_calendars([])
        resources = to_calendar_resources(result.resources)
        self.assertEqual(len(resources), 2)
        self.assertEqual(resources[0].data, result.resources[0].data)
        self.assertEqual(resources[0].account_email, "")
        self.assertNotIn("Fictional master", repr(resources))


if __name__ == "__main__":
    unittest.main()
