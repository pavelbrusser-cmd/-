"""Synthetic iCalendar cases for actual digest selection; no network access."""

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
import json
import tracemalloc
import unittest
from unittest.mock import patch

import recurring_ical_events

from calendar_digest.selection import SelectionError, select_day


DAY = date(2026, 10, 14)


@dataclass(frozen=True)
class Resource:
    source_id: str
    calendar_id: str
    calendar_name: str
    account_email: str
    data: str


def event(uid="example", *properties):
    return "\r\n".join([
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261001T000000Z",
        *properties, "END:VEVENT",
    ])


def resource(*events, source="mailru-1", calendar="schedule", account="me@example.invalid"):
    return Resource(source, calendar, "Synthetic calendar", account, "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Digest Tests//EN",
        *events, "END:VCALENDAR", "",
    ]))


class SelectionTests(unittest.TestCase):
    def test_rrule_rdate_exdate_and_instance_deduplication(self):
        data = resource(event("series",
            "DTSTART:20261012T090000Z", "DTEND:20261012T100000Z",
            "RRULE:FREQ=DAILY;COUNT=5", "RDATE:20261014T090000Z,20261014T130000Z",
            "EXDATE:20261013T090000Z", "SUMMARY:Series",
        ))
        results = select_day([data, data], DAY)
        self.assertEqual([item.start for item in results], [
            "2026-10-14T12:00:00+03:00", "2026-10-14T16:00:00+03:00",
        ])
        self.assertTrue(all(item.recurring for item in results))
        self.assertEqual([item.recurrence_id for item in results], [item.start for item in results])
        self.assertEqual(select_day([data], date(2026, 10, 13)), [])
        json.dumps([asdict(item) for item in results])

    def test_explicit_cancelled_override_survives_exdate(self):
        data = resource(
            event("cancelled", "DTSTART:20261012T090000Z", "DTEND:20261012T100000Z",
                  "RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20261014T090000Z", "SUMMARY:Original"),
            event("cancelled", "RECURRENCE-ID:20261014T090000Z", "DTSTART:20261014T090000Z",
                  "DTEND:20261014T100000Z", "STATUS:CANCELLED", "SUMMARY:Cancelled instance"),
        )
        result, = select_day([data], DAY)
        self.assertEqual(result.event_status, "CANCELLED")
        self.assertEqual(result.title, "Cancelled instance")
        self.assertIsNone(result.move_basis)

    def test_long_rdate_period_overlapping_day_is_not_lost(self):
        data = resource(event("period", "DTSTART:20261001T090000Z", "DTEND:20261001T100000Z",
            "RDATE;VALUE=PERIOD:20261013T090000Z/20261015T100000Z"))
        result, = select_day([data], DAY)
        self.assertEqual(result.start, "2026-10-13T12:00:00+03:00")
        self.assertEqual(result.end, "2026-10-15T13:00:00+03:00")
        self.assertTrue(result.multi_day)
        self.assertTrue(result.recurring)
        excluded = resource(event("period", "DTSTART:20261001T090000Z", "DTEND:20261001T100000Z",
            "RDATE;VALUE=PERIOD:20261013T090000Z/20261015T100000Z", "EXDATE:20261013T090000Z"))
        self.assertEqual(select_day([excluded], DAY), [])

    def test_moved_rdate_period_preserves_original_duration(self):
        data = resource(
            event("period", "DTSTART:20261001T090000Z", "DTEND:20261001T100000Z",
                  "RDATE;VALUE=PERIOD:20261013T090000Z/P2D"),
            event("period", "RECURRENCE-ID:20261013T090000Z", "DTSTART:20261016T090000Z",
                  "DTEND:20261016T100000Z"),
        )
        result, = select_day([data], DAY)
        self.assertEqual(result.original_start, "2026-10-13T12:00:00+03:00")
        self.assertEqual(result.original_end, "2026-10-15T12:00:00+03:00")
        self.assertEqual(result.start, "2026-10-16T12:00:00+03:00")

    def test_original_end_is_independent_of_rid_timezone_across_dst(self):
        master = event("dst", "DTSTART;TZID=Europe/Berlin:20261018T013000",
            "DTEND;TZID=Europe/Berlin:20261018T033000", "RRULE:FREQ=WEEKLY;COUNT=3")
        results = []
        for rid in (
            "RECURRENCE-ID:20261024T233000Z",
            "RECURRENCE-ID;TZID=Europe/Berlin:20261025T013000",
        ):
            data = resource(master, event("dst", rid,
                "DTSTART;TZID=Europe/Berlin:20261026T013000",
                "DTEND;TZID=Europe/Berlin:20261026T033000"))
            result, = select_day([data], date(2026, 10, 25))
            results.append(result)
        self.assertEqual(results[0].original_start, results[1].original_start)
        self.assertEqual(results[0].original_end, "2026-10-25T04:30:00+03:00")
        self.assertEqual(results[0].original_end, results[1].original_end)
        normal, = select_day([resource(master)], date(2026, 10, 25))
        self.assertEqual(normal.end, results[0].original_end)

    def test_nominal_day_and_exact_24_hours_differ_across_dst(self):
        nominal = resource(event("nominal", "DTSTART;TZID=Europe/Berlin:20261024T120000",
            "DURATION:P1D"))
        exact = resource(event("exact", "DTSTART;TZID=Europe/Berlin:20261024T120000",
            "DURATION:PT24H"))
        results = {item.uid: item for item in select_day([nominal, exact], date(2026, 10, 25))}
        self.assertEqual(results["nominal"].end, "2026-10-25T14:00:00+03:00")
        self.assertEqual(results["exact"].end, "2026-10-25T13:00:00+03:00")

    def test_exdate_without_override_does_not_invent_cancellation(self):
        data = resource(event("excluded", "DTSTART:20261012T090000Z",
            "RRULE:FREQ=DAILY;COUNT=5", "EXDATE:20261014T090000Z"))
        self.assertEqual(select_day([data], DAY), [])

    def test_moved_out_and_moved_in_overrides_keep_original_slots(self):
        data = resource(
            event("series", "DTSTART:20261012T090000Z", "DTEND:20261012T100000Z",
                  "RRULE:FREQ=DAILY;COUNT=5", "SUMMARY:Master"),
            event("series", "RECURRENCE-ID:20261014T090000Z", "DTSTART:20261016T140000Z",
                  "DTEND:20261016T160000Z", "SUMMARY:Moved away"),
            event("series", "RECURRENCE-ID:20261013T090000Z", "DTSTART:20261014T140000Z",
                  "DTEND:20261014T160000Z", "SUMMARY:Moved here"),
        )
        results = {item.title: item for item in select_day([data], DAY)}
        self.assertEqual(set(results), {"Moved away", "Moved here"})
        moved = results["Moved away"]
        self.assertEqual(moved.original_start, "2026-10-14T12:00:00+03:00")
        self.assertEqual(moved.original_end, "2026-10-14T13:00:00+03:00")
        self.assertEqual(moved.start, "2026-10-16T17:00:00+03:00")
        self.assertEqual(moved.end, "2026-10-16T19:00:00+03:00")
        self.assertEqual(moved.move_basis, "recurrence_id")
        self.assertEqual(results["Moved here"].original_start, "2026-10-13T12:00:00+03:00")

    def test_master_and_override_can_arrive_in_separate_resources(self):
        master = resource(event("split", "DTSTART:20261012T090000Z",
            "RRULE:FREQ=DAILY;COUNT=5", "SUMMARY:Inherited title"))
        override = resource(event("split", "RECURRENCE-ID:20261014T090000Z",
            "DTSTART:20261015T090000Z", "STATUS:TENTATIVE"))
        result, = select_day([master, override], DAY)
        self.assertEqual(result.title, "Inherited title")
        self.assertEqual(result.event_status, "TENTATIVE")
        self.assertEqual(result.start, "2026-10-15T12:00:00+03:00")

    def test_orphan_override_retained_without_inventing_old_duration(self):
        data = resource(event("orphan", "RECURRENCE-ID:20261014T090000Z",
            "DTSTART:20261016T090000Z", "DTEND:20261016T100000Z"))
        result, = select_day([data], DAY)
        self.assertTrue(result.recurring)
        self.assertEqual(result.original_start, "2026-10-14T12:00:00+03:00")
        self.assertIsNone(result.original_end)

    def test_cancelled_override_without_dtstart_uses_rid_and_master_duration(self):
        data = resource(
            event("cancel", "DTSTART:20261012T090000Z", "DTEND:20261012T100000Z",
                  "RRULE:FREQ=DAILY;COUNT=5", "SUMMARY:Original"),
            event("cancel", "RECURRENCE-ID:20261014T090000Z", "STATUS:CANCELLED"),
        )
        result, = select_day([data], DAY)
        self.assertEqual(result.title, "Original")
        self.assertEqual(result.event_status, "CANCELLED")
        self.assertEqual(result.start, "2026-10-14T12:00:00+03:00")
        self.assertEqual(result.end, "2026-10-14T13:00:00+03:00")

    def test_all_day_birthdays_holidays_multiday_and_exclusive_end(self):
        data = resource(
            event("birthday", "DTSTART;VALUE=DATE:20261014", "CATEGORIES:Birthday",
                  "SUMMARY:Birthday", "RRULE:FREQ=YEARLY"),
            event("holiday", "DTSTART;VALUE=DATE:20261014", "CATEGORIES:Holiday"),
            event("several-days", "DTSTART;VALUE=DATE:20261013", "DTEND;VALUE=DATE:20261016"),
            event("already-ended", "DTSTART;VALUE=DATE:20261013", "DTEND;VALUE=DATE:20261014"),
        )
        results = {item.uid: item for item in select_day([data], DAY)}
        self.assertEqual(set(results), {"birthday", "holiday", "several-days"})
        self.assertTrue(all(item.all_day for item in results.values()))
        self.assertEqual(results["holiday"].end, "2026-10-15")
        self.assertFalse(results["birthday"].multi_day)
        self.assertTrue(results["several-days"].multi_day)

    def test_moscow_half_open_boundaries_and_point_events(self):
        data = resource(
            event("early", "DTSTART:20261013T213000Z", "DTEND:20261013T220000Z"),
            event("ends-at-start", "DTSTART:20261013T200000Z", "DTEND:20261013T210000Z"),
            event("starts-at-end", "DTSTART:20261014T210000Z"),
            event("point-at-start", "DTSTART:20261013T210000Z"),
            event("ends-at-end", "DTSTART:20261014T200000Z", "DTEND:20261014T210000Z"),
        )
        results = {item.uid: item for item in select_day([data], DAY)}
        self.assertEqual(set(results), {"early", "point-at-start", "ends-at-end"})
        self.assertFalse(results["ends-at-end"].multi_day)
        self.assertEqual(results["early"].start, "2026-10-14T00:30:00+03:00")

    def test_floating_assumes_moscow_and_named_zones_convert_after_expansion(self):
        data = resource(
            event("floating", "DTSTART:20261014T003000", "DTEND:20261014T013000"),
            event("berlin", "DTSTART;TZID=Europe/Berlin:20261013T120000",
                  "DTEND;TZID=Europe/Berlin:20261013T130000", "RRULE:FREQ=DAILY;COUNT=3"),
            event("overnight", "DTSTART:20261013T200000Z", "DTEND:20261014T010000Z"),
        )
        results = {item.uid: item for item in select_day([data], DAY)}
        self.assertEqual(results["floating"].start, "2026-10-14T00:30:00+03:00")
        self.assertTrue(results["floating"].timezone_assumed)
        self.assertEqual(results["berlin"].start, "2026-10-14T13:00:00+03:00")
        self.assertFalse(results["berlin"].timezone_assumed)
        self.assertTrue(results["overnight"].multi_day)

    def test_all_invitation_and_event_statuses_are_kept_per_account(self):
        statuses = ["ACCEPTED", "DECLINED", "TENTATIVE", "NEEDS-ACTION", "DELEGATED"]
        data = resource(*[
            event(participation, "DTSTART:20261014T090000Z", "STATUS:CANCELLED",
                  f"ATTENDEE;PARTSTAT={participation}:MAILTO:Me@Example.Invalid",
                  "ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.invalid")
            for participation in statuses
        ])
        results = select_day([data], DAY)
        self.assertEqual({item.participation for item in results}, set(statuses))
        self.assertTrue(all(item.event_status == "CANCELLED" for item in results))
        other = replace(data, source_id="mailru-2", account_email="absent@example.invalid")
        self.assertTrue(all(item.participation == "UNKNOWN" for item in select_day([other], DAY)))
        accepted = replace(data, source_id="mailru-3", account_email="other@example.invalid")
        self.assertTrue(all(item.participation == "ACCEPTED" for item in select_day([accepted], DAY)))

    def test_default_attendee_partstat_and_tentative_status(self):
        data = resource(event("pending", "DTSTART:20261014T090000Z", "STATUS:TENTATIVE",
            "ATTENDEE:mailto:me@example.invalid", "LOCATION:Synthetic room"))
        result, = select_day([data], DAY)
        self.assertEqual(result.participation, "NEEDS-ACTION")
        self.assertEqual(result.event_status, "TENTATIVE")
        self.assertEqual(result.location, "Synthetic room")

    def test_no_cross_source_or_calendar_deduplication(self):
        data = resource(event("shared", "DTSTART:20261014T090000Z"))
        results = select_day([
            data, data, replace(data, source_id="mailru-2"), replace(data, calendar_id="other"),
        ], DAY)
        self.assertEqual(len(results), 3)

    def test_snapshot_standalone_moves_in_and_out_use_updated_fields(self):
        old = resource(
            event("out", "DTSTART:20261014T090000Z", "DTEND:20261014T100000Z", "SUMMARY:Old"),
            event("in", "DTSTART:20261013T090000Z", "DTEND:20261013T100000Z"),
        )
        current = resource(
            event("out", "DTSTART:20261016T090000Z", "DTEND:20261016T110000Z",
                  "SUMMARY:Updated", "STATUS:TENTATIVE"),
            event("in", "DTSTART:20261014T090000Z", "DTEND:20261014T100000Z"),
        )
        results = {item.uid: item for item in select_day([current], DAY, [old])}
        self.assertEqual(set(results), {"in", "out"})
        self.assertEqual(results["out"].original_start, "2026-10-14T12:00:00+03:00")
        self.assertEqual(results["out"].original_end, "2026-10-14T13:00:00+03:00")
        self.assertEqual(results["out"].title, "Updated")
        self.assertEqual(results["out"].event_status, "TENTATIVE")
        self.assertTrue(all(item.move_basis == "previous_snapshot" for item in results.values()))
        self.assertFalse(any(item.recurring for item in results.values()))
        first_run, = select_day([current], DAY)
        self.assertEqual(first_run.uid, "in")
        self.assertIsNone(first_run.original_start)
        self.assertEqual(select_day([], DAY, [old]), [])

    def test_snapshot_identity_includes_source_and_calendar(self):
        old = resource(event("same", "DTSTART:20261014T090000Z"))
        current = resource(event("same", "DTSTART:20261016T090000Z"), source="mailru-2")
        self.assertEqual(select_day([current], DAY, [old]), [])
        current = replace(current, source_id=old.source_id, calendar_id="different")
        self.assertEqual(select_day([current], DAY, [old]), [])

    def test_higher_sequence_wins_and_equal_conflicting_versions_fail(self):
        low = resource(event("version", "DTSTART:20261014T090000Z", "SEQUENCE:1", "SUMMARY:Old"))
        high = resource(event("version", "DTSTART:20261014T100000Z", "SEQUENCE:2", "SUMMARY:Updated"))
        result, = select_day([low, high], DAY)
        self.assertEqual(result.title, "Updated")
        conflicting = resource(event("version", "DTSTART:20261014T110000Z", "SEQUENCE:2"))
        with self.assertRaises(SelectionError):
            select_day([high, conflicting], DAY)

    def test_streaming_groups_preserve_split_overrides_multiuid_and_escaped_uid(self):
        master = resource(
            event("escaped\\,uid", "DTSTART:20261012T090000Z", "RRULE:FREQ=DAILY;COUNT=4", "SUMMARY:Master"),
            event("other", "DTSTART:20261014T130000Z", "SUMMARY:Other"),
        )
        override = resource(event("escaped\\,uid", "RECURRENCE-ID:20261014T090000Z",
                                  "DTSTART:20261016T090000Z", "SUMMARY:Moved", "SEQUENCE:2"))
        results = select_day(iter([override, master]), DAY)
        self.assertEqual({item.uid for item in results}, {"escaped,uid", "other"})
        moved = next(item for item in results if item.uid == "escaped,uid")
        self.assertEqual(moved.title, "Moved")
        self.assertEqual(moved.original_start, "2026-10-14T12:00:00+03:00")
        self.assertEqual(moved.start, "2026-10-16T12:00:00+03:00")

    def test_compact_history_still_rejects_conflicting_previous_versions(self):
        old = resource(event("previous", "DTSTART:20261014T090000Z", "SUMMARY:First"))
        conflict = resource(event("previous", "DTSTART:20261014T090000Z", "SUMMARY:Conflict"))
        current = resource(event("previous", "DTSTART:20261016T090000Z"))
        with self.assertRaises(SelectionError):
            select_day([current], DAY, iter([old, conflict]))

    def test_current_custom_timezone_precedes_historical_definition(self):
        zone_id = "Custom/SelectionCurrentBeforeHistory"
        def with_zone(offset):
            timezone = "\r\n".join([
                "BEGIN:VTIMEZONE", "TZID:" + zone_id, "BEGIN:STANDARD",
                "DTSTART:19700101T000000", "TZOFFSETFROM:" + offset,
                "TZOFFSETTO:" + offset, "END:STANDARD", "END:VTIMEZONE",
            ])
            return resource(timezone, event("custom-zone", f"DTSTART;TZID={zone_id}:20261014T090000"))
        result, = select_day([with_zone("+0300")], DAY, [with_zone("+0100")])
        self.assertEqual(result.start, "2026-10-14T09:00:00+03:00")
        self.assertIsNone(result.move_basis)

    def test_large_history_does_not_retain_full_event_graphs(self):
        # Descriptions are deliberately large: the input already exists, while
        # selection should retain only one parsed series and compact old times.
        rows = [resource(event(str(index), "DTSTART:20200101T090000Z",
                               "DESCRIPTION:" + "synthetic " * 700)) for index in range(240)]
        tracemalloc.start()
        try:
            self.assertEqual(select_day(iter(rows), DAY, iter(rows)), [])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)

    def test_old_daily_recurrence_has_no_historical_datetime_cache(self):
        data = resource(event("old-daily", "DTSTART:20000101T090000Z", "RRULE:FREQ=DAILY"))
        queries = []
        original = recurring_ical_events.of
        def track(*args, **kwargs):
            query = original(*args, **kwargs)
            queries.append(query)
            return query
        with patch("calendar_digest.selection.recurring_ical_events.of", side_effect=track):
            result, = select_day([data], DAY)
        self.assertEqual(result.start, "2026-10-14T12:00:00+03:00")
        for query in queries:
            for series in query.series:
                for rule in series.recurrence.rrules[1:]:
                    self.assertIsNone(rule._cache)

    def test_unsupported_range_and_whole_series_move_fail_explicitly(self):
        ranged = resource(event("range", "DTSTART:20261014T090000Z",
            "RECURRENCE-ID;RANGE=THISANDFUTURE:20261013T090000Z"))
        with self.assertRaisesRegex(SelectionError, "RANGE"):
            select_day([ranged], DAY)
        old = resource(event("series", "DTSTART:20261012T090000Z", "RRULE:FREQ=DAILY;COUNT=5"))
        new = resource(event("series", "DTSTART:20261012T100000Z", "RRULE:FREQ=DAILY;COUNT=5"))
        with self.assertRaisesRegex(SelectionError, "всей повторяющейся серии"):
            select_day([new], DAY, [old])

    def test_malformed_critical_data_fail_safely(self):
        invalid = [
            resource(event("private-uid", "DTSTART:not-a-date", "SUMMARY:Private title")),
            resource(event("private-uid", "DTSTART:20261014T100000Z", "DTEND:20261014T090000Z")),
            resource(event("private-uid", "DTSTART:20261014T100000Z", "DTEND;VALUE=DATE:20261015")),
            resource(event("private-uid", "DTSTART:20261014T100000Z", "DURATION:-PT1H")),
            resource(event("private-uid", "DTSTART:20261014T100000Z", "RRULE:FREQ=INVALID")),
            resource(event("private-uid", "DTSTART;TZID=Not/ARealZone:20261014T100000")),
            resource(event("private-uid", "SUMMARY:Private title")),
        ]
        for data in invalid:
            with self.subTest(data=invalid.index(data)), self.assertRaises(SelectionError) as error:
                select_day([data], DAY)
            for private in ("private-uid", "Private title", "me@example.invalid"):
                self.assertNotIn(private, str(error.exception))
        with self.assertRaises(SelectionError):
            select_day([], datetime(2026, 10, 14))


if __name__ == "__main__":
    unittest.main()
