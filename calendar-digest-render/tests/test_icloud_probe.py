"""Probe calendars through the real DAV and iCalendar parsers, without network."""

from datetime import datetime
import unittest
from unittest.mock import patch
from xml.etree import ElementTree
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import niquests

from calendar_digest.icloud import Credentials, ICloudError, probe_calendars
from test_icloud import (
    PASSWORD,
    PRINCIPAL,
    ROOT,
    USERNAME,
    calendar_properties,
    dav_response,
    multistatus,
    response_for,
)


MOSCOW = ZoneInfo("Europe/Moscow")
START = datetime(2026, 10, 14, tzinfo=MOSCOW)
END = datetime(2026, 10, 15, tzinfo=MOSCOW)
CALDAV_NS = "{urn:ietf:params:xml:ns:caldav}"
CALENDAR_HOME = ROOT + "123/calendars/"

# One resource containing a recurring master and one overridden occurrence.
# Its two VEVENT components are not a count of occurrences in the query range.
RECURRING_EVENT = "\r\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Calendar Digest Tests//EN",
    "BEGIN:VEVENT",
    "UID:fictional-recurring-event@example.invalid",
    "DTSTAMP:20261001T000000Z",
    "DTSTART:20261012T090000Z",
    "DTEND:20261012T093000Z",
    "RRULE:FREQ=DAILY;COUNT=4",
    "SUMMARY:Fictional recurring appointment",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:fictional-recurring-event@example.invalid",
    "DTSTAMP:20261001T000000Z",
    "RECURRENCE-ID:20261014T090000Z",
    "DTSTART:20261014T100000Z",
    "DTEND:20261014T103000Z",
    "SUMMARY:Fictional rescheduled appointment",
    "END:VEVENT",
    "END:VCALENDAR",
    "",
])


def component_properties(name, components):
    return (
        calendar_properties(name)
        + "<c:supported-calendar-component-set>"
        + "".join(f'<c:comp name="{component}"/>' for component in components)
        + "</c:supported-calendar-component-set>"
    )


class ICloudProbeTests(unittest.TestCase):
    def setUp(self):
        self.transport = self.enterContext(patch.object(niquests.Session, "send"))
        self.transport.side_effect = AssertionError("Unexpected HTTP request")
        self.credentials = Credentials(USERNAME, PASSWORD)
        self.reported_urls = []

    def install_calendar_server(self, event_data=RECURRING_EVENT):
        calendars = {
            CALENDAR_HOME + "family-a/": ("Family", ["VEVENT"]),
            CALENDAR_HOME + "family-b/": ("Family", ["VEVENT"]),
            CALENDAR_HOME + "reminders/": ("Reminders", ["VTODO"]),
        }

        def send(request, **kwargs):
            method, url = request.method, request.url
            self.assertIn(method, {"PROPFIND", "REPORT"})
            self.assertTrue(kwargs["verify"])
            if method == "PROPFIND":
                if url == ROOT:
                    body = multistatus(dav_response(
                        "/", "<d:current-user-principal><d:href>/123/principal/"
                        "</d:href></d:current-user-principal>",
                    ))
                elif url == PRINCIPAL:
                    body = multistatus(dav_response(
                        "/123/principal/",
                        f"<c:calendar-home-set><d:href>{CALENDAR_HOME}</d:href>"
                        "</c:calendar-home-set>",
                    ))
                elif url == CALENDAR_HOME:
                    body = multistatus(*[
                        dav_response(href, component_properties(name, components))
                        for href, (name, components) in calendars.items()
                    ])
                elif url in calendars:
                    name, components = calendars[url]
                    body = multistatus(dav_response(url, component_properties(name, components)))
                else:
                    self.fail(f"Unexpected PROPFIND destination: {url}")
            else:
                self.assertIn(url, calendars)
                self.assertIn("VEVENT", calendars[url][1], "VTODO-only calendars must not be searched")
                self.reported_urls.append(url)
                query = ElementTree.fromstring(request.body)
                self.assertEqual(query.tag, CALDAV_NS + "calendar-query")
                self.assertIsNone(query.find(".//" + CALDAV_NS + "expand"))
                event_filter = query.find(".//" + CALDAV_NS + "comp-filter[@name='VEVENT']")
                self.assertIsNotNone(event_filter)
                time_range = event_filter.find(CALDAV_NS + "time-range")
                self.assertIsNotNone(time_range)
                self.assertEqual(time_range.attrib, {
                    "start": "20261013T210000Z",
                    "end": "20261014T210000Z",
                })
                if url.endswith("family-a/") and event_data is not None:
                    body = multistatus(dav_response(
                        url + "fictional-event.ics",
                        '<d:getetag>"fictional-etag"</d:getetag>'
                        f"<c:calendar-data>{escape(event_data)}</c:calendar-data>",
                    ))
                else:
                    body = multistatus()
            return response_for(request, body)

        self.transport.side_effect = send

    def test_counts_resources_and_components_for_every_event_calendar(self):
        self.install_calendar_server()
        probes = probe_calendars(self.credentials, start=START, end=END)
        self.assertEqual(
            [(probe.id, probe.name, probe.supported_components,
              probe.resource_count, probe.vevent_components) for probe in probes],
            [
                ("family-a", "Family", ["VEVENT"], 1, 2),
                ("family-b", "Family", ["VEVENT"], 0, 0),
                ("reminders", "Reminders", ["VTODO"], None, None),
            ],
        )
        self.assertEqual(self.reported_urls, [
            CALENDAR_HOME + "family-a/", CALENDAR_HOME + "family-b/",
        ])

    def test_empty_search_results_are_zero_counts(self):
        self.install_calendar_server(event_data=None)
        probes = probe_calendars(self.credentials, start=START, end=END)
        self.assertEqual([(probe.resource_count, probe.vevent_components) for probe in probes], [
            (0, 0), (0, 0), (None, None),
        ])
        self.assertEqual(len(self.reported_urls), 2)

    def test_naive_or_nonpositive_intervals_fail_before_network(self):
        for start, end in (
            (START.replace(tzinfo=None), END),
            (START, END.replace(tzinfo=None)),
            (START.replace(tzinfo=None), END.replace(tzinfo=None)),
            (START, START),
            (END, START),
        ):
            with self.subTest(start=start, end=end), self.assertRaises(ICloudError):
                probe_calendars(self.credentials, start=start, end=end)
        self.transport.assert_not_called()

    def test_non_event_record_is_not_counted_as_a_valid_event_resource(self):
        todo = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Calendar Digest Tests//EN",
            "BEGIN:VTODO", "UID:fictional-task@example.invalid",
            "DTSTAMP:20261001T000000Z", "SUMMARY:Fictional task",
            "END:VTODO", "END:VCALENDAR", "",
        ])
        self.install_calendar_server(event_data=todo)
        with self.assertRaises(ICloudError) as error:
            probe_calendars(self.credentials, start=START, end=END)
        for private in (USERNAME, PASSWORD, "Fictional task", "fictional-task@example.invalid"):
            self.assertNotIn(private, str(error.exception))


if __name__ == "__main__":
    unittest.main()
