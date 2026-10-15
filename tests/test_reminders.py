"""Reminder rules against synthetic calendars; no accounts or network access."""

from dataclasses import asdict, replace
from datetime import datetime, time, timedelta
import json
import unittest
from unittest.mock import patch

from calendar_digest.calendar_collection import CalendarResource
from calendar_digest import reminders
from calendar_digest.reminders import ReminderError, plan_reminders


def dt(value):
    return datetime.fromisoformat(value)


def event(uid="sample", *properties):
    return "\r\n".join([
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261001T000000Z",
        *properties, "END:VEVENT",
    ])


def resource(*events, source="mailru_2", calendar="synthetic", name="Synthetic calendar"):
    return CalendarResource(source, calendar, name, "self@example.invalid", "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Reminder Tests//EN",
        *events, "END:VCALENDAR", "",
    ]))


def plan(resources, start="2026-10-10T00:00:00+03:00", end="2026-10-20T00:00:00+03:00", **kwargs):
    return plan_reminders(resources, window_start=dt(start), window_end=dt(end), **kwargs)


class ReminderTests(unittest.TestCase):
    def test_control_uses_calendar_days_and_exact_hours(self):
        result = plan([resource(event("one", "DTSTART:20261015T120000Z", "SUMMARY:Review"))])
        self.assertEqual([(item.rule, item.notify_at) for item in result.reminders], [
            ("d3", "2026-10-12T09:00:00+03:00"),
            ("d1", "2026-10-14T09:00:00+03:00"),
            ("h3", "2026-10-15T12:00:00+03:00"),
        ])
        self.assertTrue(all(item.event_start == "2026-10-15T15:00:00+03:00" for item in result.reminders))
        self.assertEqual(result.summary["by_category"]["control"], 3)
        json.dumps(asdict(result))

    def test_periodic_words_override_control_only_and_match_whole_words(self):
        for word in ("weekly", "MONTHLY", "Quarterly", "yearly", "daily", "biweekly"):
            with self.subTest(word=word):
                result = plan([resource(event("marked", "DTSTART:20261015T120000Z", f"SUMMARY:Review ({word})"))])
                self.assertEqual([item.rule for item in result.reminders], ["d1", "h1"])
                self.assertTrue(all(item.category == "periodic_control" for item in result.reminders))
        for title in ("nonweeklyish", "weekly_report", "dailyish", "еженедельно"):
            with self.subTest(title=title):
                result = plan([resource(event("plain", "DTSTART:20261015T120000Z", f"SUMMARY:{title}"))])
                self.assertEqual([item.rule for item in result.reminders], ["d3", "d1", "h3"])
        other = resource(event("other", "DTSTART:20261015T120000Z", "SUMMARY:weekly"), source="mailru_1")
        self.assertEqual(plan([other]).reminders, [])

    def test_rrule_frequency_and_description_do_not_make_periodic_markers(self):
        result = plan([resource(event("ordinary", "DTSTART:20261015T120000Z",
            "RRULE:FREQ=YEARLY;COUNT=1", "DESCRIPTION:weekly", "SUMMARY:Review"))])
        self.assertEqual([item.rule for item in result.reminders], ["d3", "d1", "h3"])

    def test_explicit_birthday_evidence_and_precedence(self):
        cases = [
            ("Private label", "CATEGORIES:PERSONAL,Birthday", "Synthetic calendar", "categories"),
            ("Private label", None, "Дни рождения", "calendar_name"),
            ("День рождения Synthetic", None, "Synthetic calendar", "title"),
            ("Birthday Synthetic", None, "Synthetic calendar", "title"),
            ("Synthetic's birthday", None, "Synthetic calendar", "title"),
            ("ДР Synthetic", None, "Synthetic calendar", "title"),
            ("Д.Р. Synthetic", None, "Synthetic calendar", "title"),
            ("Synthetic (ДР)", None, "Synthetic calendar", "title"),
        ]
        for title, category, name, evidence in cases:
            with self.subTest(title=title, category=category, name=name):
                properties = ["DTSTART;VALUE=DATE:20261015", f"SUMMARY:{title}"]
                if category:
                    properties.append(category)
                result = plan([resource(event("birthday", *properties), name=name)])
                self.assertEqual([item.rule for item in result.reminders], ["d3", "d1", "d0"])
                self.assertTrue(all(item.category == "birthdays" for item in result.reminders))
                self.assertTrue(all(evidence in item.classification_evidence for item in result.reminders))
        birthday = resource(event("priority", "DTSTART:20261015T120000Z",
            "SUMMARY:Birthday weekly", "RRULE:FREQ=YEARLY;COUNT=1"))
        self.assertEqual([item.rule for item in plan([birthday]).reminders], ["d3", "d1", "d0"])

    def test_no_birthday_from_names_all_day_annual_or_partial_words(self):
        for title in ("Synthetic Person", "ДРЕЛЬ", "birthdayish", "Подготовка к дню рождения"):
            with self.subTest(title=title):
                item = resource(event("neutral", "DTSTART;VALUE=DATE:20261015",
                    "RRULE:FREQ=YEARLY;COUNT=1", f"SUMMARY:{title}"), source="icloud")
                result = plan([item])
                self.assertEqual(result.reminders, [])
                self.assertEqual(result.summary["series_without_policy"], 1)

    def test_every_invitation_participation_is_retained(self):
        resources = [resource(event(status, "DTSTART:20261015T120000Z", "STATUS:TENTATIVE",
            f"ATTENDEE;PARTSTAT={status}:mailto:self@example.invalid"))
            for status in ("ACCEPTED", "DECLINED", "NEEDS-ACTION", "TENTATIVE")]
        resources.append(resource(event("unknown", "DTSTART:20261015T120000Z")))
        result = plan(resources)
        self.assertEqual(len(result.reminders), 15)
        self.assertEqual({item.uid for item in result.reminders}, {"ACCEPTED", "DECLINED", "NEEDS-ACTION", "TENTATIVE", "unknown"})

    def test_cancelled_override_suppresses_original_even_with_exdate(self):
        data = resource(
            event("series", "DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=3", "EXDATE:20261015T120000Z"),
            event("series", "RECURRENCE-ID:20261014T120000Z", "STATUS:CANCELLED"),
            event("series", "RECURRENCE-ID:20261015T120000Z", "DTSTART:20261015T120000Z", "STATUS:CANCELLED"),
        )
        result = plan([data])
        self.assertEqual({item.event_start for item in result.reminders}, {"2026-10-16T15:00:00+03:00"})
        self.assertEqual(result.summary["cancelled_occurrences"], 2)

    def test_cancelled_master_and_inherited_cancellation_are_excluded(self):
        data = resource(
            event("series", "DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=3", "STATUS:CANCELLED"),
            event("series", "RECURRENCE-ID:20261015T120000Z", "DTSTART:20261015T140000Z"),
        )
        self.assertEqual(plan([data]).reminders, [])
        # Explicit current STATUS is respected, just as in digest selection.
        confirmed = resource(event("series", "RECURRENCE-ID:20261016T120000Z",
            "DTSTART:20261016T140000Z", "STATUS:CONFIRMED"))
        self.assertEqual({item.event_start for item in plan([data, confirmed]).reminders}, {"2026-10-16T17:00:00+03:00"})

    def test_moves_use_actual_start_and_keep_original_instance_identity(self):
        master = resource(event("moved", "DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=1", "SUMMARY:Original"))
        moved = resource(event("moved", "RECURRENCE-ID:20261014T120000Z",
            "DTSTART:20261020T120000Z", "SUMMARY:Moved"))
        self.assertEqual(plan([master, moved], start="2026-10-10T00:00:00+03:00", end="2026-10-15T00:00:00+03:00").reminders, [])
        result = plan([master, moved], end="2026-10-21T00:00:00+03:00")
        self.assertEqual([item.notify_at for item in result.reminders], [
            "2026-10-17T09:00:00+03:00", "2026-10-19T09:00:00+03:00", "2026-10-20T12:00:00+03:00",
        ])
        self.assertTrue(all(item.recurrence_id == "2026-10-14T15:00:00+03:00" for item in result.reminders))
        original = plan([master])
        self.assertEqual({item.stable_id for item in original.reminders}, {item.stable_id for item in result.reminders})

    def test_detached_override_moved_from_outside_range_and_inherited_categories(self):
        master = resource(event("old", "DTSTART;VALUE=DATE:20200101",
            "RRULE:FREQ=YEARLY;COUNT=1", "CATEGORIES:Birthday", "SUMMARY:Inherited"), source="icloud")
        override = resource(event("old", "RECURRENCE-ID;VALUE=DATE:20200101",
            "DTSTART;VALUE=DATE:20261015"), source="icloud")
        result = plan([master, override])
        self.assertEqual(len(result.reminders), 3)
        self.assertTrue(all(item.category == "birthdays" and item.title == "Inherited" for item in result.reminders))
        standalone = resource(event("orphan", "RECURRENCE-ID:20200101T120000Z", "DTSTART:20261015T120000Z"))
        orphan_plan = plan([standalone])
        self.assertEqual(len(orphan_plan.reminders), 3)
        self.assertEqual(orphan_plan.summary["series_without_policy"], 0)

    def test_rrule_rdate_exdate_and_duplicate_resources(self):
        data = resource(event("series", "DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=3",
            "RDATE:20261014T120000Z,20261017T120000Z", "EXDATE:20261015T120000Z"))
        result = plan([data, data])
        self.assertEqual({item.event_start for item in result.reminders}, {
            "2026-10-14T15:00:00+03:00", "2026-10-16T15:00:00+03:00", "2026-10-17T15:00:00+03:00",
        })
        self.assertEqual(len(result.reminders), 9)
        self.assertEqual(len({item.stable_id for item in result.reminders}), 9)

    def test_night_offsets_cross_midnight(self):
        resources = [
            resource(event("normal", "DTSTART;TZID=Europe/Moscow:20261015T010000")),
            resource(event("periodic", "DTSTART;TZID=Europe/Moscow:20261015T003000", "SUMMARY:daily")),
        ]
        result = plan(resources, start="2026-10-14T21:00:00+03:00", end="2026-10-15T00:00:00+03:00")
        self.assertEqual([(item.rule, item.notify_at) for item in result.reminders], [
            ("h3", "2026-10-14T22:00:00+03:00"), ("h1", "2026-10-14T23:30:00+03:00"),
        ])

    def test_all_day_multiday_and_missing_dtend_use_start_only(self):
        for summary, expected in (("Review", ["d3", "d1", "d0"]), ("monthly", ["d1", "d0"])):
            with self.subTest(summary=summary):
                data = resource(event("all-day", "DTSTART;VALUE=DATE:20261015",
                    "DTEND;VALUE=DATE:20261018", f"SUMMARY:{summary}"))
                result = plan([data])
                self.assertEqual([item.rule for item in result.reminders], expected)
                self.assertTrue(all(item.all_day and item.event_start == "2026-10-15" for item in result.reminders))
                self.assertEqual(result.reminders[-1].notify_at, "2026-10-15T09:00:00+03:00")
                self.assertEqual(plan([data], start="2026-10-16T00:00:00+03:00").reminders, [])
        missing_end = resource(event("default", "DTSTART;VALUE=DATE:20261015"))
        self.assertEqual(len(plan([missing_end]).reminders), 3)

    def test_half_open_notification_window_and_utc_inputs(self):
        data = resource(event("birthday", "DTSTART;VALUE=DATE:20261015", "SUMMARY:Birthday"))
        result = plan([data], start="2026-10-12T06:00:00+00:00", end="2026-10-14T06:00:00+00:00")
        self.assertEqual([(item.rule, item.notify_at) for item in result.reminders], [("d3", "2026-10-12T09:00:00+03:00")])
        narrow = plan([data], start="2026-10-15T09:00:00+03:00", end="2026-10-15T09:00:00.000001+03:00")
        self.assertEqual([item.rule for item in narrow.reminders], ["d0"])

    def test_timezone_conversion_and_floating_assumption(self):
        berlin = resource(event("dst", "DTSTART;TZID=Europe/Berlin:20261018T100000", "RRULE:FREQ=WEEKLY;COUNT=2"))
        result = plan([berlin], start="2026-10-25T00:00:00+03:00", end="2026-10-26T00:00:00+03:00")
        self.assertEqual([(item.event_start, item.notify_at) for item in result.reminders], [
            ("2026-10-25T12:00:00+03:00", "2026-10-25T09:00:00+03:00"),
        ])
        floating = plan([resource(event("floating", "DTSTART:20261015T120000"))])
        self.assertEqual(floating.reminders[-1].notify_at, "2026-10-15T09:00:00+03:00")
        self.assertEqual(floating.summary["timezone_assumed_occurrences"], 1)

    def test_yearly_birthday_and_timed_birthday_use_moscow_calendar_date(self):
        yearly = resource(event("anniversary", "DTSTART;VALUE=DATE:20001015",
            "RRULE:FREQ=YEARLY", "SUMMARY:Birthday"), source="icloud")
        timed = resource(event("timed", "DTSTART;TZID=Asia/Tokyo:20261016T010000",
            "SUMMARY:День рождения Synthetic"), source="mailru_1")
        result = plan([yearly, timed])
        self.assertEqual(len(result.reminders), 6)
        by_uid = {uid: [item for item in result.reminders if item.uid == uid] for uid in ("anniversary", "timed")}
        self.assertEqual([item.rule for item in by_uid["anniversary"]], ["d3", "d1", "d0"])
        self.assertTrue(all(item.event_start == "2026-10-15" and item.recurrence_id == "2026-10-15"
                            for item in by_uid["anniversary"]))
        self.assertTrue(all(item.event_start == "2026-10-15T19:00:00+03:00" for item in by_uid["timed"]))
        self.assertEqual(by_uid["timed"][-1].notify_at, "2026-10-15T09:00:00+03:00")

    def test_rdate_period_does_not_schedule_again_on_overlap(self):
        data = resource(event("period", "DTSTART:20261001T120000Z",
            "RDATE;VALUE=PERIOD:20261013T120000Z/20261018T120000Z"))
        self.assertEqual(plan([data], start="2026-10-14T00:00:00+03:00").reminders, [])
        result = plan([data])
        self.assertEqual(len(result.reminders), 3)
        self.assertTrue(all(item.event_start == "2026-10-13T15:00:00+03:00" for item in result.reminders))

    def test_stable_identity_changes_only_for_source_calendar_uid_rid_or_rule(self):
        initial = plan([resource(event("stable", "DTSTART:20261015T120000Z"))]).reminders[0]
        edited = replace(initial, title="Edited", event_start="2026-10-18T15:00:00+03:00",
            notify_at="2026-10-15T09:00:00+03:00", calendar_name="Renamed")
        self.assertEqual(initial.stable_id, edited.stable_id)
        for field, value in (("source_id", "other"), ("calendar_id", "other"), ("uid", "other"),
                             ("recurrence_id", "2026-10-15"), ("rule", "d1")):
            self.assertNotEqual(initial.stable_id, replace(initial, **{field: value}).stable_id)

    def test_each_uid_parsed_once_for_multiday_window(self):
        resources = [resource(event(str(i), "DTSTART:20261015T120000Z", "RRULE:FREQ=DAILY;COUNT=4")) for i in range(3)]
        with patch.object(reminders.selection, "_read_series", wraps=reminders.selection._read_series) as parser:
            plan(resources)
        self.assertEqual(parser.call_count, 3)

    def test_limits_fail_without_truncating_or_allocating_between_list(self):
        with patch("dateutil.rrule.rrulebase.between", side_effect=AssertionError("No intermediate list")):
            small = plan([resource(event("small", "DTSTART:20261015T120000Z", "RRULE:FREQ=DAILY;COUNT=2"))])
            self.assertEqual(len(small.reminders), 6)
        dense = resource(event("dense", "DTSTART:20261015T000000Z", "RRULE:FREQ=SECONDLY;COUNT=100000"))
        with patch.object(reminders, "MAX_SERIES_RECURRENCE_STEPS", 20):
            with self.assertRaisesRegex(ReminderError, "серия"):
                plan([dense])
        with patch.object(reminders, "MAX_OCCURRENCES", 1):
            with self.assertRaisesRegex(ReminderError, "экземпляров"):
                plan([resource(event("two", "DTSTART:20261015T120000Z", "RRULE:FREQ=DAILY;COUNT=2"))])
        with patch.object(reminders, "MAX_REMINDERS", 1):
            with self.assertRaisesRegex(ReminderError, "напоминаний"):
                plan([resource(event("one", "DTSTART:20261015T120000Z"))])

    def test_invalid_windows_rejected_before_reading_resources(self):
        def unread():
            raise AssertionError("Resources must not be read")
            yield
        start = dt("2026-10-15T00:00:00+03:00")
        for begin, end in ((start.replace(tzinfo=None), start), (start, start),
                           (start, start - timedelta(seconds=1)), (start, start + timedelta(days=32))):
            with self.subTest(begin=begin, end=end), self.assertRaises(ReminderError):
                plan_reminders(unread(), window_start=begin, window_end=end)
        with self.assertRaises(ReminderError):
            plan_reminders(unread(), window_start=start, window_end=start + timedelta(days=1), morning_time=time(9, tzinfo=start.tzinfo))

    def test_malformed_conflicting_and_range_events_fail_safely(self):
        malformed = resource(event("private-marker", "DTSTART:not-a-date"))
        with self.assertRaises(ReminderError) as caught:
            plan([malformed])
        self.assertNotIn("private-marker", str(caught.exception))
        conflicting = [resource(event("same", "DTSTART:20261015T120000Z", f"SUMMARY:{title}")) for title in ("First", "Other")]
        with self.assertRaises(ReminderError):
            plan(conflicting)
        ranged = resource(event("range", "DTSTART:20261015T120000Z",
            "RECURRENCE-ID;RANGE=THISANDFUTURE:20261014T120000Z"))
        with self.assertRaisesRegex(ReminderError, "RANGE"):
            plan([ranged])


if __name__ == "__main__":
    unittest.main()
