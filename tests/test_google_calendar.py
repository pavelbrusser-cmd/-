"""Google API fixtures exercise actual recurrence selection without network."""

from datetime import date, datetime
import json
import unittest
from unittest.mock import patch

from icalendar import Calendar

from calendar_digest.calendar_collection import CalendarResource
from calendar_digest import google_calendar as google
from calendar_digest.readonly_caldav import CalendarConnectionError
from calendar_digest.reminders import plan_reminders
from calendar_digest.selection import select_day


EMAIL = "fictional@example.invalid"
TOKEN = "fictional-private-token"
METADATA = {"id": EMAIL, "summary": "Fictional Google", "timeZone": "Europe/Berlin"}


class Credentials:
    expected_email = EMAIL

    def access_token(self):
        return TOKEN


def event(event_id="ordinary", **updates):
    raw = {
        "id": event_id, "iCalUID": event_id + "@example.invalid", "status": "confirmed",
        "summary": "Fictional event", "updated": "2026-10-01T01:02:03Z",
        "start": {"dateTime": "2026-10-20T09:00:00+02:00", "timeZone": "Europe/Berlin"},
        "end": {"dateTime": "2026-10-20T10:00:00+02:00", "timeZone": "Europe/Berlin"},
    }
    raw.update(updates)
    return raw


def series(**updates):
    return event("series", recurrence=updates.pop("recurrence", ["RRULE:FREQ=DAILY;COUNT=12"]), **updates)


def exception(event_id="instance", **updates):
    return event(event_id, recurringEventId="series",
                 originalStartTime={"dateTime": "2026-10-20T09:00:00+02:00", "timeZone": "Europe/Berlin"},
                 **updates)


def calendar_resource(resource):
    return CalendarResource("google", resource.calendar_id, resource.calendar_name,
                            resource.account_email, resource.data)


class Response:
    def __init__(self, payload, *, status=200, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self.body = body if body is not None else json.dumps(payload).encode()
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]


class GoogleCalendarTests(unittest.TestCase):
    def setUp(self):
        self.responses = []
        self.requests = []
        self.session = self.enterContext(patch.object(google.niquests, "Session"))
        self.session.return_value.__enter__.return_value.get.side_effect = self.get

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        self.assertIn(url, {google.API_ROOT, google.API_ROOT + "/events"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])
        self.assertTrue(kwargs["stream"])
        if not self.responses:
            self.fail("Unexpected extra HTTP request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def collect(self, items, *, metadata=None, pages=None):
        self.responses = [Response(metadata if metadata is not None else METADATA)]
        self.responses.extend(Response({"kind": "calendar#events", **page}) for page in (pages or [{"items": items}]))
        return google.collect_google(Credentials())

    def test_primary_calendar_checked_and_full_snapshot_paginated(self):
        result = self.collect([], pages=[
            {"items": [series()], "nextPageToken": "second-page"},
            {"items": [exception()]},
        ])
        self.assertEqual(result.calendar_count, 1)
        self.assertEqual(len(result.resources), 1)
        first, = result.resources
        self.assertEqual(first.account_email, EMAIL)
        self.assertEqual(first.calendar_id, EMAIL)
        self.assertTrue(google.valid_google_resource_identity(EMAIL, first.href))
        self.assertNotIn(EMAIL, repr(result))
        self.assertNotIn("Fictional event", repr(result))
        components = Calendar.from_ical(first.data).walk("VEVENT")
        self.assertEqual(len(components), 2)
        self.assertEqual(str(components[0]["UID"]), str(components[1]["UID"]))
        self.assertEqual(components[0]["X-GOOGLE-ICALUID"], "series@example.invalid")
        self.assertIn("RECURRENCE-ID", components[1])
        for _, request in self.requests[1:]:
            self.assertEqual(request["params"]["singleEvents"], "false")
            self.assertEqual(request["params"]["showDeleted"], "true")
            self.assertEqual(request["params"]["showHiddenInvitations"], "true")
            self.assertNotIn("timeMin", request["params"])
            self.assertNotIn("timeMax", request["params"])
        self.assertEqual(self.requests[-1][1]["params"]["pageToken"], "second-page")

    def test_wrong_primary_account_aborts_before_reading_events(self):
        with self.assertRaises(CalendarConnectionError):
            self.collect([], metadata={"id": "different@example.invalid"})
        self.assertEqual(len(self.requests), 1)

    def test_recurring_wall_time_keeps_dst_transition(self):
        resource, = self.collect([series()]).resources
        parsed, = Calendar.from_ical(resource.data).walk("VEVENT")
        self.assertEqual(parsed["DTSTART"].params["TZID"], "Europe/Berlin")
        before, = select_day([calendar_resource(resource)], date(2026, 10, 24))
        after, = select_day([calendar_resource(resource)], date(2026, 10, 25))
        self.assertEqual(before.start, "2026-10-24T10:00:00+03:00")
        self.assertEqual(after.start, "2026-10-25T11:00:00+03:00")

    def test_numeric_offset_without_tzid_preserves_exact_instant(self):
        resource, = self.collect([event(
            start={"dateTime": "2026-10-20T15:00:00+03:00"},
            end={"dateTime": "2026-10-20T15:30:00+03:00"},
        )]).resources
        self.assertNotIn("UTC+03:00", resource.data)
        selected, = select_day([calendar_resource(resource)], date(2026, 10, 20))
        self.assertEqual(selected.start, "2026-10-20T15:00:00+03:00")
        self.assertEqual(selected.end, "2026-10-20T15:30:00+03:00")
        self.assertFalse(selected.timezone_assumed)

    def test_utc_representation_with_iana_expansion_zone_preserves_dst(self):
        resource, = self.collect([series(
            start={"dateTime": "2026-10-20T07:00:00Z", "timeZone": "Europe/Berlin"},
            end={"dateTime": "2026-10-20T08:00:00Z", "timeZone": "Europe/Berlin"},
        )]).resources
        after, = select_day([calendar_resource(resource)], date(2026, 10, 25))
        self.assertEqual(after.start, "2026-10-25T11:00:00+03:00")

    def test_recurring_end_without_zone_preserves_duration_after_dst(self):
        resource, = self.collect([series(
            end={"dateTime": "2026-10-20T10:00:00+02:00"},
        )]).resources
        after, = select_day([calendar_resource(resource)], date(2026, 10, 25))
        self.assertEqual(after.start, "2026-10-25T11:00:00+03:00")
        self.assertEqual(after.end, "2026-10-25T12:00:00+03:00")

    def test_all_day_multiday_and_exclusive_end(self):
        resource, = self.collect([event(start={"date": "2026-10-20"}, end={"date": "2026-10-23"})]).resources
        data = calendar_resource(resource)
        result, = select_day([data], date(2026, 10, 22))
        self.assertTrue(result.all_day)
        self.assertTrue(result.multi_day)
        self.assertEqual(result.start, "2026-10-20")
        self.assertEqual(select_day([data], date(2026, 10, 23)), [])

    def test_far_moved_exception_keeps_original_slot_and_participation(self):
        moved = exception(
            start={"dateTime": "2026-12-20T15:00:00+01:00", "timeZone": "Europe/Berlin"},
            end={"dateTime": "2026-12-20T16:00:00+01:00", "timeZone": "Europe/Berlin"},
            attendees=[{"email": EMAIL, "responseStatus": "tentative"}],
            organizer={"email": "organizer@example.invalid", "displayName": "Fictional organizer"},
        )
        resource, = self.collect([moved, series()]).resources
        selected, = select_day([calendar_resource(resource)], date(2026, 10, 20))
        self.assertEqual(selected.start, "2026-12-20T17:00:00+03:00")
        self.assertEqual(selected.original_start, "2026-10-20T10:00:00+03:00")
        self.assertEqual(selected.participation, "TENTATIVE")
        self.assertEqual(selected.move_basis, "recurrence_id")

    def test_sparse_cancelled_exception_inherits_master_duration_and_title(self):
        cancelled = {"id": "cancelled", "status": "cancelled", "recurringEventId": "series",
                     "originalStartTime": {"dateTime": "2026-10-21T09:00:00+02:00"}}
        resource, = self.collect([series(), cancelled]).resources
        selected, = select_day([calendar_resource(resource)], date(2026, 10, 21))
        self.assertEqual(selected.event_status, "CANCELLED")
        self.assertEqual(selected.title, "Fictional event")
        self.assertEqual(selected.start, "2026-10-21T10:00:00+03:00")
        self.assertEqual(selected.end, "2026-10-21T11:00:00+03:00")

    def test_cancelled_all_day_override(self):
        master = series(start={"date": "2026-10-20"}, end={"date": "2026-10-22"})
        cancelled = {"id": "cancelled", "status": "cancelled", "recurringEventId": "series",
                     "originalStartTime": {"date": "2026-10-23"}}
        resource, = self.collect([master, cancelled]).resources
        components = Calendar.from_ical(resource.data).walk("VEVENT")
        self.assertEqual(components[1]["DTSTART"].dt, date(2026, 10, 23))
        self.assertEqual(components[1]["DTEND"].dt, date(2026, 10, 25))

    def test_master_cancellation_cannot_revive_active_child(self):
        resource, = self.collect([series(status="cancelled"), exception()]).resources
        selected, = select_day([calendar_resource(resource)], date(2026, 10, 20))
        self.assertEqual(selected.event_status, "CANCELLED")
        self.assertEqual(len(Calendar.from_ical(resource.data).walk("VEVENT")), 1)

    def test_deleted_master_and_unknown_cancelled_tombstones_are_removed_resources(self):
        result = self.collect([
            {"id": "series", "status": "cancelled"}, exception(),
            {"id": "standalone", "status": "cancelled"},
            {"id": "orphan", "status": "cancelled", "recurringEventId": "removed-series"},
        ])
        self.assertEqual(result.resources, [])

    def test_active_orphan_or_nonrecurring_master_aborts(self):
        for items in ([exception()], [event("series"), exception()]):
            with self.subTest(items=items), self.assertRaises(CalendarConnectionError):
                self.collect(items)

    def test_birthday_type_uses_birthday_policy(self):
        raw = event("birthday", eventType="birthday", summary="Fictional person",
                    recurrence=["RRULE:FREQ=YEARLY"],
                    start={"date": "2000-10-20"}, end={"date": "2000-10-21"})
        resource, = self.collect([raw]).resources
        plan = plan_reminders([calendar_resource(resource)],
                              window_start=datetime.fromisoformat("2026-10-17T00:00:00+03:00"),
                              window_end=datetime.fromisoformat("2026-10-21T00:00:00+03:00"))
        self.assertEqual(plan.summary["by_category"]["birthdays"], 3)
        self.assertEqual(plan.summary["by_rule"]["d3"], 1)

    def test_all_invitation_partstats_retained(self):
        for google_status, expected in google._PARTSTAT.items():
            with self.subTest(status=google_status):
                raw = event(attendees=[{"email": EMAIL.upper(), "responseStatus": google_status}])
                resource, = self.collect([raw]).resources
                selected, = select_day([calendar_resource(resource)], date(2026, 10, 20))
                self.assertEqual(selected.participation, expected)

    def test_self_attendee_alias_maps_verified_account_and_retains_original_address(self):
        for response in ("accepted", "declined"):
            with self.subTest(response=response):
                resource, = self.collect([event(attendees=[
                    {"email": "alias@example.invalid", "self": True, "responseStatus": response},
                    {"email": "other@example.invalid", "self": False, "responseStatus": "tentative"},
                ])]).resources
                selected, = select_day([calendar_resource(resource)], date(2026, 10, 20))
                self.assertEqual(selected.participation, response.upper())
                component, = Calendar.from_ical(resource.data).walk("VEVENT")
                attendees = {str(address): address for address in component["ATTENDEE"]}
                self.assertEqual(set(attendees), {"mailto:" + EMAIL, "mailto:other@example.invalid"})
                self.assertEqual(attendees["mailto:" + EMAIL].params["X-GOOGLE-EMAIL"], "alias@example.invalid")
                self.assertNotIn("X-GOOGLE-EMAIL", attendees["mailto:other@example.invalid"].params)

    def test_ambiguous_or_malformed_self_attendee_aborts(self):
        for attendees in (
            [{"email": EMAIL, "self": "true"}],
            [{"email": EMAIL, "self": 1}],
            [{"email": EMAIL, "self": True}, {"email": "alias@example.invalid", "self": True}],
        ):
            with self.subTest(attendees=attendees), self.assertRaises(CalendarConnectionError):
                self.collect([event(attendees=attendees)])

    def test_rrule_rdate_exdate_are_preserved(self):
        raw = series(recurrence=["RRULE:FREQ=DAILY;COUNT=2", "EXDATE:20261020T070000Z", "RDATE:20261023T070000Z"])
        resource, = self.collect([raw]).resources
        self.assertEqual(select_day([calendar_resource(resource)], date(2026, 10, 20)), [])
        selected, = select_day([calendar_resource(resource)], date(2026, 10, 23))
        self.assertEqual(selected.start, "2026-10-23T10:00:00+03:00")

    def test_stable_identity_etag_across_order_and_distinct_google_ids(self):
        first = series(attendees=[{"email": "z@example.invalid"}, {"email": "a@example.invalid"}])
        child = exception()
        initial, = self.collect([first, child]).resources
        first["attendees"].reverse()
        reordered, = self.collect([child, first]).resources
        self.assertEqual(initial, reordered)
        other = event("different", iCalUID=first["iCalUID"])
        a, b = self.collect([first, other]).resources
        self.assertNotEqual(str(Calendar.from_ical(a.data).walk("VEVENT")[0]["UID"]),
                            str(Calendar.from_ical(b.data).walk("VEVENT")[0]["UID"]))
        self.assertNotEqual(a.href, b.href)

    def test_event_change_changes_etag_but_preserves_href_and_uid(self):
        before, = self.collect([event()]).resources
        after, = self.collect([event(summary="Fictional changed")]).resources
        self.assertEqual(before.href, after.href)
        self.assertNotEqual(before.etag, after.etag)
        self.assertEqual(Calendar.from_ical(before.data).walk("VEVENT")[0]["UID"],
                         Calendar.from_ical(after.data).walk("VEVENT")[0]["UID"])

    def test_duplicate_event_ids_and_duplicate_recurrence_ids_abort(self):
        for items in ([event(), event()], [series(), exception("a"), exception("b")]):
            with self.subTest(items=items), self.assertRaises(CalendarConnectionError):
                self.collect(items)

    def test_page_failure_returns_no_partial_collection_and_hides_secrets(self):
        for failure in (Response({"error": TOKEN}, status=500),
                        RuntimeError(TOKEN + " private event data"),
                        Response(None, body=(TOKEN + " not-json").encode())):
            self.responses = [Response(METADATA), Response({"kind": "calendar#events", "items": [event()], "nextPageToken": "later"}), failure]
            with self.assertRaises(CalendarConnectionError) as error:
                google.collect_google(Credentials())
            for secret in (TOKEN, EMAIL, "private event data", "Fictional event"):
                self.assertNotIn(secret, str(error.exception))

    def test_redirect_is_not_followed_and_response_is_closed(self):
        response = Response({}, status=302, headers={"Location": "https://attacker.invalid/"})
        self.responses = [response]
        with self.assertRaises(CalendarConnectionError):
            google.collect_google(Credentials())
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(response.closed)

    def test_resource_page_byte_and_total_limits_abort(self):
        cases = (("MAX_EVENTS", 0), ("MAX_RESPONSE_BYTES", 1), ("MAX_TOTAL_BYTES", 1), ("MAX_PAGES", 0))
        for name, limit in cases:
            with self.subTest(limit=name), patch.object(google, name, limit), self.assertRaises(CalendarConnectionError):
                self.collect([event()])

    def test_streaming_size_limit_and_declared_size_limit(self):
        for response in (Response({}, body=b"x" * 101), Response({}, headers={"Content-Length": "101"})):
            self.responses = [response]
            with patch.object(google, "MAX_RESPONSE_BYTES", 100), self.assertRaises(CalendarConnectionError):
                google.collect_google(Credentials())
            self.assertTrue(response.closed)

    def test_collection_deadline_and_pagination_cycle_abort(self):
        with patch.object(google, "COLLECTION_TIMEOUT_SECONDS", 0), self.assertRaises(CalendarConnectionError):
            self.collect([])
        with self.assertRaises(CalendarConnectionError):
            self.collect([], pages=[{"nextPageToken": "same"}, {"nextPageToken": "same"}])

    def test_incomplete_json_envelope_cannot_be_treated_as_empty_calendar(self):
        for payload in ({}, {"kind": "calendar#events", "error": "private"}):
            self.responses = [Response(METADATA), Response(payload)]
            with self.subTest(payload=payload), self.assertRaises(CalendarConnectionError):
                google.collect_google(Credentials())
        self.assertEqual(self.collect([], pages=[{}]).resources, [])

    def test_incomplete_or_incompatible_event_data_aborts(self):
        invalid = [
            event(start={"date": "2026-10-20", "dateTime": "2026-10-20T00:00:00Z"}),
            event(end={"date": "2026-10-21"}),
            event(start={"dateTime": "2026-10-20T09:00:00"}),
            event(attendeesOmitted=True),
            event(attendees=[{"email": EMAIL, "responseStatus": "unknown"}]),
            event(status="unsupported"),
            event(originalStartTime={"dateTime": "2026-10-20T07:00:00Z"}),
            series(start={"dateTime": "2026-10-20T09:00:00+02:00"}),
            series(recurrence=["RRULE:FREQ=DAILY\r\nSUMMARY:Injected"]),
            series(recurrence=["DTSTART:20261020T090000Z"]),
            series(recurrence=["RRULE:FREQ=DAILY", "EXDATE;VALUE=DATE:20261021"]),
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(CalendarConnectionError):
                self.collect([raw])

    def test_ical_text_is_escaped_as_text_and_cannot_inject_component(self):
        title = "Fictional\nEND:VEVENT\nBEGIN:VEVENT\nUID:injected"
        resource, = self.collect([event(summary=title)]).resources
        components = Calendar.from_ical(resource.data).walk("VEVENT")
        self.assertEqual(len(components), 1)
        self.assertEqual(str(components[0]["SUMMARY"]), title)

    def test_cache_identity_does_not_accept_external_hosts_or_other_accounts(self):
        resource, = self.collect([event()]).resources
        for href in (resource.href.replace("www.googleapis.com", "attacker.invalid"),
                     resource.href + "?access_token=private", resource.href + "/../other",
                     resource.href.replace("/events/", "/calendars/")):
            self.assertFalse(google.valid_google_resource_identity(EMAIL, href))
        self.assertFalse(google.valid_google_resource_identity("other@example.invalid", resource.href))


if __name__ == "__main__":
    unittest.main()
