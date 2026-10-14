"""Complete collection contracts through real CalDAV parsing and fake HTTP."""

import base64
import logging
import os
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import niquests
from icalendar import Calendar

from calendar_digest.calendar_collection import (
    CollectionLimitError,
    _check_response,
    collect_all,
)
from calendar_digest.readonly_caldav import CalendarConnectionError
from test_icloud import calendar_properties, dav_response, multistatus, response_for


PASSWORD = "fictional-collection-password"
RAW_SERIES = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Collection Tests//EN",
    "BEGIN:VEVENT", "UID:fictional-series@example.invalid",
    "DTSTAMP:20261001T000000Z", "DTSTART:20261014T090000Z",
    "DTEND:20261014T100000Z", "RRULE:FREQ=DAILY;COUNT=3",
    "SUMMARY:Fictional master", "END:VEVENT",
    "BEGIN:VEVENT", "UID:fictional-series@example.invalid",
    "DTSTAMP:20261001T000000Z", "RECURRENCE-ID:20261014T090000Z",
    "DTSTART:20261220T090000Z", "DTEND:20261220T100000Z",
    "SUMMARY:Fictional move beyond the daily query", "END:VEVENT",
    "BEGIN:VEVENT", "UID:fictional-series@example.invalid",
    "DTSTAMP:20261001T000000Z", "RECURRENCE-ID:20261015T090000Z",
    "DTSTART:20261015T090000Z", "DTEND:20261015T100000Z",
    "STATUS:CANCELLED", "END:VEVENT", "END:VCALENDAR", "",
])
RAW_EXCEPTION = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Collection Tests//EN",
    "BEGIN:VEVENT", "UID:fictional-exception@example.invalid",
    "DTSTAMP:20261001T000000Z", "RECURRENCE-ID:20261014T090000Z",
    "DTSTART:20261220T090000Z", "DTEND:20261220T100000Z",
    "END:VEVENT", "END:VCALENDAR", "",
])


def supported_properties(name, kind):
    return (
        calendar_properties(name)
        + f'<c:supported-calendar-component-set><c:comp name="{kind}"/>'
        "</c:supported-calendar-component-set>"
    )


class CalendarCollectionTests(unittest.TestCase):
    def setUp(self):
        environment = {}
        self.accounts = {}
        for source, prefix in (
            ("icloud", "ICLOUD"),
            ("mailru_1", "MAILRU_1"),
            ("mailru_2", "MAILRU_2"),
            ("mailru_3", "MAILRU_3"),
        ):
            username = f"fictional-{source}@example.invalid"
            environment[prefix + "_USERNAME"] = username
            environment[prefix + "_APP_PASSWORD"] = PASSWORD
            authorization = "Basic " + base64.b64encode(f"{username}:{PASSWORD}".encode()).decode()
            self.accounts[authorization] = source
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.transport = self.enterContext(patch.object(niquests.Session, "send", autospec=True))
        self.transport.side_effect = self.send
        self.reported_sources = []
        self.raw_records = {"icloud": RAW_SERIES, "mailru_1": RAW_EXCEPTION}
        self.fail_source = None
        self.partial_source = None
        self.include_collection_response = False

    def send(self, session, request, **kwargs):
        source = self.accounts[request.headers["Authorization"]]
        if source == "icloud":
            root = "https://caldav.icloud.com/"
            principal = "https://caldav.icloud.com/123/principal/"
            home = "https://caldav.icloud.com/123/calendars/"
        else:
            root = "https://calendar.mail.ru/principals/"
            principal = "https://calendar.mail.ru/principals/users/example/"
            home = "https://calendar.mail.ru/calendars/users/example/"
        self.assertTrue(kwargs["verify"])
        self.assertIn(request.method, {"PROPFIND", "REPORT"})
        if request.method == "PROPFIND":
            if request.url == root:
                body = multistatus(dav_response(
                    root, f"<d:current-user-principal><d:href>{principal}</d:href>"
                    "</d:current-user-principal>",
                ))
            elif request.url == principal:
                body = multistatus(dav_response(
                    principal, f"<c:calendar-home-set><d:href>{home}</d:href>"
                    "</c:calendar-home-set>",
                ))
            elif request.url == home:
                body = multistatus(
                    dav_response(home + "family/", supported_properties("Family", "VEVENT")),
                    dav_response(home + "tasks/", supported_properties("Tasks", "VTODO")),
                )
            elif request.url in {home + "family/", home + "tasks/"}:
                kind = "VEVENT" if request.url.endswith("family/") else "VTODO"
                body = multistatus(dav_response(request.url, supported_properties("Family", kind)))
            else:
                self.fail("Unexpected PROPFIND destination")
            response = response_for(request, body)
        else:
            self.assertEqual(request.url, home + "family/", "VTODO calendars must not be fetched")
            query = ElementTree.fromstring(request.body)
            ns = "{urn:ietf:params:xml:ns:caldav}"
            self.assertIsNotNone(query.find(".//" + ns + "comp-filter[@name='VEVENT']"))
            self.assertIsNone(query.find(".//" + ns + "time-range"), "Inventory must include moved-out overrides")
            self.assertIsNone(query.find(".//" + ns + "expand"))
            self.reported_sources.append(source)
            if source == self.fail_source:
                raise RuntimeError(f"private-response {PASSWORD} {request.url}")
            if source == self.partial_source:
                body = multistatus(dav_response(
                    request.url + "unreadable.ics", "<c:calendar-data/>", "403 Forbidden",
                ))
            elif source in self.raw_records:
                body = multistatus(dav_response(
                    request.url + "event.ics",
                    f"<c:calendar-data>{escape(self.raw_records[source])}</c:calendar-data>"
                    '<d:getetag>"fictional-etag"</d:getetag>',
                ))
            else:
                body = multistatus()
            if self.include_collection_response:
                collection_response = dav_response(
                    request.url, "<c:calendar-data/>", "404 Not Found",
                )
                body = body.replace("</d:multistatus>", collection_response + "</d:multistatus>")
            response = response_for(request, body)
        # The fake transport stands in for Session.send, so explicitly dispatch
        # response hooks at the same boundary as niquests' real implementation.
        for hook in session.hooks.get("response", []):
            response = hook(response) or response
        return response

    def test_collects_all_accounts_and_preserves_complete_recurrence_resources(self):
        collection = collect_all()
        self.assertEqual(collection.calendar_count, 4)
        self.assertEqual(len(collection.resources), 2)
        self.assertEqual(self.reported_sources, ["icloud", "mailru_1", "mailru_2", "mailru_3"])
        first, second = collection.resources
        self.assertEqual((first.source_id, first.calendar_id, first.calendar_name), ("icloud", "family", "Family"))
        self.assertEqual(first.account_email, "fictional-icloud@example.invalid")
        events = Calendar.from_ical(first.data).walk("VEVENT")
        self.assertEqual(len(events), 3)
        self.assertIn("RRULE", events[0])
        self.assertEqual(events[1]["DTSTART"].dt.month, 12)
        self.assertEqual(events[1]["RECURRENCE-ID"].dt.month, 10)
        self.assertEqual(events[2]["STATUS"], "CANCELLED")
        exception_only = Calendar.from_ical(second.data).walk("VEVENT")
        self.assertEqual(len(exception_only), 1)
        self.assertIn("RECURRENCE-ID", exception_only[0])
        self.assertNotIn(first.account_email, repr(collection))
        self.assertNotIn("Fictional master", repr(collection))

    def test_late_source_failure_aborts_without_exposing_raw_error(self):
        self.fail_source = "mailru_3"
        previous_logging = logging.root.manager.disable
        with self.assertRaises(CalendarConnectionError) as error:
            collect_all()
        self.assertEqual(self.reported_sources[-1], "mailru_3")
        self.assertEqual(logging.root.manager.disable, previous_logging)
        for private in (PASSWORD, "private-response", "https://", "example.invalid"):
            self.assertNotIn(private, str(error.exception))

    def test_resource_and_total_byte_limits_abort_instead_of_truncating(self):
        for constant, limit in (("MAX_RESOURCES", 1), ("MAX_TOTAL_BYTES", 1)):
            with self.subTest(limit=constant):
                with patch(f"calendar_digest.calendar_collection.{constant}", limit):
                    with self.assertRaises(CollectionLimitError):
                        collect_all()

    def test_oversized_http_response_is_rejected(self):
        request = niquests.Request("REPORT", "https://caldav.icloud.com/").prepare()
        response = response_for(request, "x" * 20)
        with patch("calendar_digest.calendar_collection.MAX_RESPONSE_BYTES", 19):
            with self.assertRaises(CollectionLimitError):
                _check_response(response)

    def test_partial_multistatus_fails_instead_of_silently_omitting_a_record(self):
        self.partial_source = "mailru_1"
        with self.assertRaisesRegex(CalendarConnectionError, "неполные записи"):
            collect_all()
        self.assertNotIn("mailru_2", self.reported_sources)

    def test_icloud_collection_href_without_calendar_data_is_not_an_event(self):
        self.include_collection_response = True
        collection = collect_all()
        self.assertEqual(collection.calendar_count, 4)
        self.assertEqual(len(collection.resources), 2)

    def test_malformed_or_non_event_records_are_safe_failures(self):
        for data in (
            f"private-invalid-data {PASSWORD}",
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\nUID:fake\r\n"
            "END:VTODO\r\nEND:VCALENDAR\r\n",
        ):
            with self.subTest(malformed=data.startswith("private")):
                self.raw_records["icloud"] = data
                with self.assertRaises(CalendarConnectionError) as error:
                    collect_all()
                for private in (PASSWORD, "private-invalid-data", "https://"):
                    self.assertNotIn(private, str(error.exception))


if __name__ == "__main__":
    unittest.main()
