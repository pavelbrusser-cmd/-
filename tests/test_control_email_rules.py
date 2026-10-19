"""Pure external-recipient planning fixtures; never access calendars or send."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import unittest
from unittest.mock import patch

from calendar_digest.calendar_collection import CalendarResource
from calendar_digest import control_email_rules as rules


ATTENDEE = "ATTENDEE;PARTSTAT=ACCEPTED:mailto:person@example.invalid"


def event(uid="control", *properties):
    return "\r\n".join([
        "BEGIN:VEVENT", "UID:" + uid, "DTSTAMP:20261001T000000Z", *properties, "END:VEVENT",
    ])


def resource(*events, source="mailru_2", account="gpb.control@mail.ru", calendar="control"):
    return CalendarResource(source, calendar, "Контроль поручений", account, "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Control Email Test//EN", *events, "END:VCALENDAR", "",
    ]))


def ordinary(uid="control", *, title="Task", start="20261022T090000Z", end="20261022T093000Z", people=None, extra=()):
    return event(uid, "DTSTART:" + start, "DTEND:" + end, "SUMMARY:" + title,
                 *(people if people is not None else [ATTENDEE]), *extra)


def plan(resources, start="2026-10-19T00:00:00+03:00", days=7, own=()):
    begin = datetime.fromisoformat(start)
    return rules.plan_control_emails(resources, window_start=begin, window_end=begin + timedelta(days=days), own_accounts=own)


class ControlEmailRulesTests(unittest.TestCase):
    def test_ordinary_d3_d1_h1_and_single_recipient_payload(self):
        result = plan([resource(ordinary(extra=("LOCATION:Room A",)))])
        self.assertEqual([(item.rule, item.notify_at) for item in result.reminders], [
            ("d3", "2026-10-19T09:00:00+03:00"),
            ("d1", "2026-10-21T09:00:00+03:00"),
            ("h1", "2026-10-22T11:00:00+03:00"),
        ])
        first = result.reminders[0]
        self.assertEqual(first.account_email, "gpb.control@mail.ru")
        self.assertEqual(first.recipient, "person@example.invalid")
        self.assertEqual(first.location, "Room A")
        self.assertEqual(first.event_end, "2026-10-22T12:30:00+03:00")
        self.assertEqual(first.category, "control_email")
        self.assertEqual(result.summary["recipients_total"], 1)
        self.assertNotIn("attendees", asdict(first))
        self.assertNotIn("action_links", asdict(first))
        self.assertNotIn(first.recipient, repr(first))

    def test_each_external_attendee_gets_separate_items(self):
        people = [ATTENDEE, "ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:second@example.invalid",
                  "ATTENDEE;PARTSTAT=TENTATIVE:mailto:third@example.invalid"]
        result = plan([resource(ordinary(people=people))])
        self.assertEqual(len(result.reminders), 9)
        self.assertEqual(result.summary["recipients_total"], 3)
        self.assertEqual(len({item.stable_id for item in result.reminders}), 9)

    def test_source_and_account_are_both_required(self):
        resources = [
            resource(ordinary("wrong-source"), source="mailru_1"),
            resource(ordinary("google"), source="google"),
            resource(ordinary("eligible"), account="GPB.CONTROL@MAIL.RU"),
        ]
        result = plan(resources)
        self.assertEqual({item.uid for item in result.reminders}, {"eligible"})
        self.assertEqual(result.summary["series_total"], 1)
        for account in ("other@example.invalid", ""):
            with self.subTest(account=account), self.assertRaises(rules.ControlEmailError):
                plan([resource(ordinary(), account=account)])

    def test_inconsistent_cancelled_override_account_aborts_complete_plan(self):
        master = resource(ordinary(extra=("RRULE:FREQ=DAILY;COUNT=1",)))
        cancelled = event("control", "RECURRENCE-ID:20261022T090000Z", "STATUS:CANCELLED")
        self.assertEqual(plan([master, resource(cancelled)]).reminders, [])
        for account in ("", "other@example.invalid"):
            with self.subTest(account=account), self.assertRaises(rules.ControlEmailError):
                plan([master, resource(cancelled, account=account)])

    def test_unrelated_malformed_calendar_is_not_parsed(self):
        unrelated = CalendarResource("icloud", "control", "Контроль поручений", "gpb.control@mail.ru", "not ICS")
        self.assertEqual(plan([unrelated]).reminders, [])

    def test_self_duplicates_declined_rooms_resources_and_malformed_are_skipped(self):
        people = [
            ATTENDEE, "ATTENDEE;PARTSTAT=ACCEPTED:mailto:PERSON@EXAMPLE.INVALID",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:gpb.control@mail.ru",
            "ATTENDEE;PARTSTAT=ACCEPTED:mailto:myself@example.invalid",
            "ATTENDEE;PARTSTAT=DECLINED:mailto:declined@example.invalid",
            "ATTENDEE;CUTYPE=RESOURCE:mailto:resource@example.invalid",
            "ATTENDEE;CUTYPE=ROOM:mailto:room@example.invalid",
            "ATTENDEE;ROLE=NON-PARTICIPANT:mailto:observer@example.invalid",
            "ATTENDEE:urn:uuid:1234", "ATTENDEE:mailto:not-an-address",
            "ATTENDEE:mailto:first@example.invalid,second@example.invalid",
            "ATTENDEE:mailto:Person <person@example.invalid>",
            "ATTENDEE:mailto:query@example.invalid?subject=private",
            "ATTENDEE:mailto:user%2Btag@example.invalid",
        ]
        result = plan([resource(ordinary(people=people))], own=["MYSELF@EXAMPLE.INVALID"])
        self.assertEqual({item.recipient for item in result.reminders}, {"person@example.invalid"})
        self.assertEqual(len(result.reminders), 3)

    def test_explicit_group_and_delegated_attendees_are_included(self):
        people = [
            "ATTENDEE;CUTYPE=GROUP:mailto:list@example.invalid",
            "ATTENDEE;PARTSTAT=DELEGATED:mailto:delegated@example.invalid",
            "ATTENDEE;CUTYPE=ROOM:mailto:room@example.invalid",
            "ATTENDEE;CUTYPE=RESOURCE:mailto:resource@example.invalid",
            "ATTENDEE;ROLE=NON-PARTICIPANT:mailto:observer@example.invalid",
            "ATTENDEE;PARTSTAT=DECLINED:mailto:declined@example.invalid",
        ]
        result = plan([resource(ordinary(people=people))])
        self.assertEqual({item.recipient for item in result.reminders},
                         {"list@example.invalid", "delegated@example.invalid"})
        self.assertEqual(len(result.reminders), 6)

    def test_organizer_alone_does_not_become_recipient(self):
        raw = ordinary(people=[], extra=("ORGANIZER:mailto:organizer@example.invalid",))
        result = plan([resource(raw)])
        self.assertEqual(result.reminders, [])
        self.assertEqual(result.summary["series_without_external_recipients"], 1)

    def test_conflicting_duplicate_participant_is_excluded_conservatively(self):
        raw = ordinary(people=[ATTENDEE, "ATTENDEE;PARTSTAT=DECLINED:mailto:PERSON@EXAMPLE.INVALID"])
        self.assertEqual(plan([resource(raw)]).reminders, [])

    def test_keywords_add_every_agreed_long_lead(self):
        cases = [
            ("weekly", "20261026", "w1"), ("biweekly", "20261102", "w2"),
            ("monthly", "20261119", "m1"), ("bimonthly", "20261219", "m2"),
            ("quarterly", "20270119", "m3"), ("semiannually", "20270419", "m6"),
            ("yearly", "20271019", "y1"), ("annually", "20271019", "y1"),
            ("biennially", "20281019", "y2"),
        ]
        for keyword, day, rule in cases:
            with self.subTest(keyword=keyword):
                raw = ordinary(title="Task (" + keyword.upper() + ")", start=day + "T090000Z", end=day + "T093000Z")
                result = plan([resource(raw)])
                extra = [item for item in result.reminders if item.rule == rule]
                self.assertEqual(len(extra), 1)
                self.assertEqual(extra[0].notify_at, "2026-10-19T09:00:00+03:00")

    def test_daily_has_only_ordinary_rules_and_keyword_matching_is_not_substring(self):
        for title in ("Task daily", "Task notweekly", "Task monthlywood"):
            with self.subTest(title=title):
                result = plan([resource(ordinary(title=title))])
                self.assertEqual({item.rule for item in result.reminders}, {"d3", "d1", "h1"})

    def test_yearly_and_annually_are_one_rule_not_duplicate_emails(self):
        raw = ordinary(title="yearly annually", start="20271019T090000Z", end="20271019T093000Z")
        result = plan([resource(raw)])
        self.assertEqual(len(result.reminders), 1)
        self.assertEqual(result.reminders[0].rule, "y1")

    def test_month_end_inverse_includes_every_clamped_future_date(self):
        raw = [ordinary(str(day), title="monthly", start=f"202703{day}T090000Z", end=f"202703{day}T093000Z")
               for day in range(28, 32)]
        result = plan([resource(*raw)], start="2027-02-28T00:00:00+03:00", days=1)
        self.assertEqual(len(result.reminders), 4)
        self.assertEqual({item.rule for item in result.reminders}, {"m1"})
        self.assertEqual({item.notify_at for item in result.reminders}, {"2027-02-28T09:00:00+03:00"})

    def test_calendar_months_and_years_shift_directly_with_leap_clamping(self):
        cases = [
            ("bimonthly", "20270131", "2026-11-30", "m2"),
            ("quarterly", "20270531", "2027-02-28", "m3"),
            ("semiannually", "20270831", "2027-02-28", "m6"),
            ("yearly", "20280229", "2027-02-28", "y1"),
            ("biennially", "20280229", "2026-02-28", "y2"),
        ]
        for title, day, notify_day, rule in cases:
            with self.subTest(title=title):
                result = plan([resource(ordinary(title=title, start=day + "T090000Z", end=day + "T093000Z"))],
                              start=notify_day + "T00:00:00+03:00", days=1)
                self.assertEqual([(item.rule, item.notify_at) for item in result.reminders],
                                 [(rule, notify_day + "T09:00:00+03:00")])

    def test_candidate_window_across_month_boundary_and_exclusive_notification_end(self):
        raw = [ordinary("first", title="monthly", start="20270130T090000Z", end="20270130T093000Z"),
               ordinary("last", title="monthly", start="20270203T090000Z", end="20270203T093000Z"),
               ordinary("outside", title="monthly", start="20270205T090000Z", end="20270205T093000Z")]
        result = plan([resource(*raw)], start="2026-12-29T00:00:00+03:00", days=7)
        self.assertEqual({item.uid for item in result.reminders}, {"first", "last"})
        first_day = plan([resource(ordinary())], start="2026-10-19T09:00:00+03:00", days=1)
        self.assertEqual(len(first_day.reminders), 1)
        before = rules.plan_control_emails([resource(ordinary())],
                                          window_start=datetime.fromisoformat("2026-10-19T00:00:00+03:00"),
                                          window_end=datetime.fromisoformat("2026-10-19T09:00:00+03:00"), own_accounts=[])
        self.assertEqual(before.reminders, [])

    def test_early_hourly_reminder_moves_to_seven_even_after_event_start(self):
        raw = ordinary(start="20261019T010000Z", end="20261019T013000Z")
        result = plan([resource(raw)], days=1)
        self.assertEqual([(item.rule, item.notify_at) for item in result.reminders], [("h1", "2026-10-19T07:00:00+03:00")])
        self.assertEqual(result.reminders[0].event_start, "2026-10-19T04:00:00+03:00")

    def test_hourly_cross_midnight_keeps_previous_day(self):
        raw = ordinary(start="20261019T213000Z", end="20261019T220000Z")
        result = plan([resource(raw)], days=1)
        self.assertEqual({(item.rule, item.notify_at) for item in result.reminders}, {
            ("d1", "2026-10-19T09:00:00+03:00"), ("h1", "2026-10-19T23:30:00+03:00"),
        })

    def test_all_day_uses_d0_morning_and_preserves_multi_day_end(self):
        raw = event("all-day", "DTSTART;VALUE=DATE:20261022", "DTEND;VALUE=DATE:20261025", ATTENDEE)
        result = plan([resource(raw)])
        self.assertEqual({item.rule for item in result.reminders}, {"d3", "d1", "d0"})
        last = result.reminders[-1]
        self.assertTrue(last.all_day)
        self.assertEqual(last.event_end, "2026-10-25")
        self.assertEqual(last.notify_at, "2026-10-22T09:00:00+03:00")

    def test_cancellation_suppresses_occurrence_and_cancelled_master_suppresses_children(self):
        master = ordinary(extra=("RRULE:FREQ=DAILY;COUNT=3",))
        cancelled = event("control", "RECURRENCE-ID:20261022T090000Z", "STATUS:CANCELLED")
        result = plan([resource(master, cancelled)])
        self.assertNotIn("2026-10-22T12:00:00+03:00", {item.event_start for item in result.reminders})
        self.assertEqual(result.summary["cancelled_occurrences"], 1)
        cancelled_master = ordinary(extra=("RRULE:FREQ=DAILY;COUNT=3", "STATUS:CANCELLED"))
        active = ordinary(extra=("RECURRENCE-ID:20261022T090000Z",))
        self.assertEqual(plan([resource(cancelled_master, active)]).reminders, [])

    def test_moved_exception_suppresses_old_slot_and_uses_actual_start(self):
        master = ordinary(extra=("RRULE:FREQ=DAILY;COUNT=2",))
        moved = ordinary(start="20261220T090000Z", end="20261220T093000Z", extra=("RECURRENCE-ID:20261022T090000Z",))
        result = plan([resource(master, moved)])
        self.assertEqual({item.recurrence_id for item in result.reminders}, {"2026-10-23T12:00:00+03:00"})
        later = plan([resource(master, moved)], start="2026-12-17T00:00:00+03:00")
        self.assertEqual({item.recurrence_id for item in later.reminders}, {"2026-10-22T12:00:00+03:00"})
        self.assertEqual({item.event_start for item in later.reminders}, {"2026-12-20T12:00:00+03:00"})

    def test_override_replaces_attendees_but_absent_attendees_inherit(self):
        master = ordinary(extra=("RRULE:FREQ=DAILY;COUNT=2",))
        replacement = ordinary(people=["ATTENDEE:mailto:new@example.invalid"], extra=("RECURRENCE-ID:20261022T090000Z",))
        inherited = ordinary(start="20261023T090000Z", end="20261023T093000Z", people=[],
                             extra=("RECURRENCE-ID:20261023T090000Z",))
        result = plan([resource(master, replacement, inherited)])
        first = {item.recipient for item in result.reminders if item.recurrence_id == "2026-10-22T12:00:00+03:00"}
        second = {item.recipient for item in result.reminders if item.recurrence_id == "2026-10-23T12:00:00+03:00"}
        self.assertEqual(first, {"new@example.invalid"})
        self.assertEqual(second, {"person@example.invalid"})

    def test_empty_explicit_attendee_list_does_not_inherit_master_recipient(self):
        master = ordinary(extra=("RRULE:FREQ=DAILY;COUNT=1",))
        removed = ordinary(people=["ATTENDEE:"], extra=("RECURRENCE-ID:20261022T090000Z",))
        self.assertEqual(plan([resource(master, removed)]).reminders, [])

    def test_override_keyword_uses_its_own_future_window(self):
        master = ordinary(extra=("RRULE:FREQ=YEARLY;COUNT=4",))
        distant = ordinary(title="biennially", start="20281019T090000Z", end="20281019T093000Z",
                           extra=("RECURRENCE-ID:20281022T090000Z",))
        result = plan([resource(master, distant)])
        future = [item for item in result.reminders if item.rule == "y2"]
        self.assertEqual(len(future), 1)
        self.assertEqual(future[0].event_start, "2028-10-19T12:00:00+03:00")

    def test_future_dense_series_materializes_only_narrow_candidate_windows(self):
        raw = ordinary(title="biennially", start="20261019T090000Z", end="20261019T093000Z",
                       extra=("RRULE:FREQ=DAILY",))
        original = rules.selection._normalize
        with patch.object(rules.selection, "_normalize", wraps=original) as normalization:
            result = plan([resource(raw)])
        self.assertLess(normalization.call_count, 60, "Do not serialize every daily event across two years")
        self.assertEqual(sum(item.rule == "y2" for item in result.reminders), 7)
        self.assertGreater(result.summary["candidate_occurrences"], 7)

    def test_no_recipient_dense_master_cannot_exhaust_budget_for_eligible_event(self):
        dense = ordinary("irrelevant", title="biennially", start="20000101T090000Z", end="20000101T093000Z",
                         people=[], extra=("RRULE:FREQ=SECONDLY",))
        with patch.object(rules, "MAX_RECURRENCE_STEPS", 0):
            result = plan([resource(dense, ordinary("eligible"))])
        self.assertEqual({item.uid for item in result.reminders}, {"eligible"})
        self.assertEqual(result.summary["series_without_external_recipients"], 1)

    def test_eligible_override_of_no_recipient_dense_master_is_still_planned(self):
        master = ordinary(title="biennially", start="20000101T090000Z", end="20000101T093000Z",
                          people=[], extra=("RRULE:FREQ=SECONDLY",))
        override = ordinary(extra=("RECURRENCE-ID:20261022T090000Z",))
        with patch.object(rules, "MAX_RECURRENCE_STEPS", 0):
            result = plan([resource(master, override)])
        self.assertEqual(len(result.reminders), 3)
        self.assertEqual({item.recurrence_id for item in result.reminders}, {"2026-10-22T12:00:00+03:00"})

    def test_recipientless_distant_override_does_not_expand_master_two_years(self):
        master = ordinary(start="20261019T090000Z", end="20261019T093000Z", extra=("RRULE:FREQ=DAILY",))
        override = ordinary(title="biennially", start="20281019T090000Z", end="20281019T093000Z",
                            people=["ATTENDEE:"], extra=("RECURRENCE-ID:20281019T090000Z",))
        with patch.object(rules, "MAX_SERIES_RECURRENCE_STEPS", 30):
            result = plan([resource(master, override)])
        self.assertTrue(result.reminders)
        self.assertNotIn("y2", {item.rule for item in result.reminders})

    def test_recurrence_preserves_dst_and_actual_hour_before(self):
        raw = event("berlin", "DTSTART;TZID=Europe/Berlin:20261024T090000", "DTEND;TZID=Europe/Berlin:20261024T100000",
                    "RRULE:FREQ=DAILY;COUNT=3", ATTENDEE)
        result = plan([resource(raw)], start="2026-10-24T00:00:00+03:00", days=3)
        hourly = {item.event_start: item.notify_at for item in result.reminders if item.rule == "h1"}
        self.assertEqual(hourly["2026-10-24T10:00:00+03:00"], "2026-10-24T09:00:00+03:00")
        self.assertEqual(hourly["2026-10-25T11:00:00+03:00"], "2026-10-25T10:00:00+03:00")

    def test_stable_identity_survives_move_and_text_change_but_not_recipient_change(self):
        before = next(item for item in plan([resource(ordinary())]).reminders if item.rule == "d3")
        after = next(item for item in plan([resource(ordinary(title="Changed", start="20261023T090000Z", end="20261023T093000Z"))]).reminders if item.rule == "d3")
        self.assertEqual(before.stable_id, after.stable_id)
        self.assertNotEqual(before.notify_at, after.notify_at)
        self.assertNotEqual(before.stable_id, replace(before, recipient="new@example.invalid").stable_id)
        self.assertNotEqual(before.stable_id, replace(before, rule="d1").stable_id)

    def test_identical_notification_slots_coalesce_with_deterministic_rule_priority(self):
        moment = datetime.fromisoformat("2026-10-19T09:00:00+03:00")
        with patch.object(rules, "_schedule", return_value=[("w1", moment), ("d1", moment), ("h1", moment)]):
            result = plan([resource(ordinary())])
        self.assertEqual(len(result.reminders), 1)
        self.assertEqual(result.reminders[0].rule, "h1")

    def test_duplicate_resources_do_not_double_send(self):
        data = resource(ordinary())
        self.assertEqual(len(plan([data, data]).reminders), 3)

    def test_multiline_text_is_single_line_without_header_controls(self):
        data = resource(ordinary(title="First\\nSecond", extra=("LOCATION:Room\\nFloor",)))
        result = plan([replace(data, calendar_name="Calendar\r\nInjected")])
        item = result.reminders[0]
        self.assertEqual(item.title, "First Second")
        self.assertEqual(item.location, "Room Floor")
        self.assertEqual(item.calendar_name, "Calendar Injected")

    def test_limits_abort_without_partial_plan_or_private_data(self):
        for name, value in (("MAX_OCCURRENCES", 0), ("MAX_REMINDERS", 1),
                            ("MAX_RECURRENCE_STEPS", 0), ("MAX_SERIES_RECURRENCE_STEPS", 0)):
            with self.subTest(limit=name), patch.object(rules, name, value), self.assertRaises(rules.ControlEmailError) as error:
                plan([resource(ordinary(title="PRIVATE TITLE", extra=("RRULE:FREQ=DAILY;COUNT=2",)))])
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertNotIn("person@example.invalid", str(error.exception))

    def test_invalid_window_and_own_accounts_fail_closed(self):
        for own in ("self@example.invalid", ["not an address"], [None]):
            with self.subTest(own=own), self.assertRaises(rules.ControlEmailError):
                plan([resource(ordinary())], own=own)
        for start, end in ((datetime(2026, 10, 19), datetime(2026, 10, 20)),
                           (datetime.fromisoformat("2026-10-19T00:00:00+03:00"), datetime.fromisoformat("2026-12-01T00:00:00+03:00"))):
            with self.assertRaises(rules.ControlEmailError):
                rules.plan_control_emails([], window_start=start, window_end=end, own_accounts=[])

    def test_email_normalization_is_conservative(self):
        self.assertEqual(rules.normalize_email("Name+tag@Example.Invalid"), "name+tag@example.invalid")
        for raw in (" name@example.invalid", "name@example.invalid\n", "Display <name@example.invalid>",
                    "a..b@example.invalid", ".a@example.invalid", "a.@example.invalid", "a@-example.invalid",
                    "a@localhost", "a@example..invalid", "a@example.invalid,b@example.invalid", "имя@example.invalid"):
            with self.subTest(raw=raw):
                self.assertIsNone(rules.normalize_email(raw))


if __name__ == "__main__":
    unittest.main()
