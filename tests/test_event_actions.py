"""Real iCalendar parse/mutate/serialize cases without transport or accounts."""

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
import unittest

from icalendar import Calendar

from calendar_digest.event_actions import (
    ActionConflict, EventTarget, SafeActionError, prepare_change, verify_change,
)
from calendar_digest.selection import select_day


ACCOUNT = "self@example.invalid"
DAY = date(2026, 10, 14)


def event(uid="synthetic", *fields):
    return "\r\n".join([
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261001T000000Z",
        *fields, "END:VEVENT",
    ])


def calendar(*components):
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Action Tests//EN",
        "X-WR-CALNAME:Synthetic calendar", *components, "END:VCALENDAR", "",
    ])


def target_for(data, uid="synthetic", day=DAY):
    resource = SimpleNamespace(
        source_id="synthetic", calendar_id="example", calendar_name="Calendar",
        account_email=ACCOUNT, data=data,
    )
    selected = [item for item in select_day([resource], day) if item.uid == uid]
    if len(selected) > 1:
        selected = [item for item in selected if item.start[:10] == day.isoformat()]
    if len(selected) != 1:
        raise AssertionError("Fixture must contain one chosen instance")
    selected = selected[0]
    return EventTarget(
        source_id=selected.source_id, calendar_id=selected.calendar_id, uid=selected.uid,
        recurrence_id=selected.recurrence_id, expected_start=selected.start,
        expected_end=selected.end, expected_event_status=selected.event_status,
        expected_participation=selected.participation,
    )


def events(data):
    return Calendar.from_ical(data).walk("VEVENT")


def base_event(*fields):
    return event("synthetic", "DTSTART:20261014T090000Z", "DTEND:20261014T100000Z",
                 "SUMMARY:Synthetic title", "SEQUENCE:4", *fields)


class EventActionsTests(unittest.TestCase):
    def test_personal_confirm_cancel_and_reschedule(self):
        data = calendar(base_event("STATUS:TENTATIVE"))
        target = target_for(data)
        confirmed = prepare_change(data, target, "confirm", ACCOUNT)
        self.assertEqual(confirmed.operation, "confirm_event")
        self.assertEqual(str(events(confirmed.data)[0]["STATUS"]), "CONFIRMED")
        self.assertTrue(verify_change(confirmed.data, target, confirmed, ACCOUNT))
        cancelled = prepare_change(data, target, "cancel", ACCOUNT)
        self.assertEqual(cancelled.operation, "cancel_event")
        self.assertEqual(str(events(cancelled.data)[0]["STATUS"]), "CANCELLED")
        moved = prepare_change(data, target, "reschedule", ACCOUNT,
                               "2026-10-15T15:00:00+03:00")
        self.assertEqual(moved.after_end, "2026-10-15T16:00:00+03:00")
        self.assertEqual(moved.title, "Synthetic title")
        self.assertEqual(moved.operation, "reschedule")
        self.assertEqual(events(moved.data)[0].decoded("SEQUENCE"), 5)
        self.assertTrue(verify_change(moved.data, target, moved, ACCOUNT))

    def test_participation_response_only_changes_matching_self_and_not_sequence(self):
        data = calendar(base_event(
            "STATUS:CONFIRMED", "ORGANIZER:mailto:organizer@example.invalid",
            "ATTENDEE;CN=Self;PARTSTAT=TENTATIVE;RSVP=TRUE;ROLE=REQ-PARTICIPANT:MAILTO:Self@Example.Invalid",
            "ATTENDEE;CN=Other;PARTSTAT=ACCEPTED;RSVP=TRUE:mailto:other@example.invalid",
        ))
        target = target_for(data)
        original = events(data)[0]
        for action, partstat, operation in (
            ("confirm", "ACCEPTED", "accept_invitation"),
            ("cancel", "DECLINED", "decline_participation"),
        ):
            with self.subTest(action=action):
                change = prepare_change(data, target, action, ACCOUNT)
                updated = events(change.data)[0]
                self.assertEqual(change.operation, operation)
                self.assertEqual(updated["ATTENDEE"][0].params["PARTSTAT"], partstat)
                self.assertEqual(updated["ATTENDEE"][0].params["ROLE"], "REQ-PARTICIPANT")
                self.assertEqual(updated["ATTENDEE"][0].params["RSVP"], "TRUE")
                self.assertEqual(updated["ATTENDEE"][1].to_ical(), original["ATTENDEE"][1].to_ical())
                self.assertEqual(updated["ATTENDEE"][1].params, original["ATTENDEE"][1].params)
                self.assertEqual(str(updated["STATUS"]), "CONFIRMED")
                self.assertEqual(updated.decoded("SEQUENCE"), 4)
                self.assertTrue(verify_change(change.data, target, change, ACCOUNT))

    def test_invitation_reschedule_requires_both_organizer_proof_and_exact_identity(self):
        for organizer, trusted in ((ACCOUNT, False), ("other@example.invalid", True)):
            data = calendar(base_event(
                f"ORGANIZER:mailto:{organizer}",
                f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{ACCOUNT}",
            ))
            with self.subTest(trusted=trusted), self.assertRaisesRegex(SafeActionError, "права организатора не подтверждены"):
                prepare_change(data, target_for(data), "reschedule", ACCOUNT,
                               "2026-10-15T15:00:00+03:00", allow_organizer_updates=trusted)
        data = calendar(base_event(
            f"ORGANIZER:mailto:{ACCOUNT}", "ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.invalid",
        ))
        target = target_for(data)
        moved = prepare_change(data, target, "reschedule", ACCOUNT,
                               "2026-10-15T15:00:00+03:00", allow_organizer_updates=True)
        self.assertEqual(moved.operation, "reschedule")
        cancelled = prepare_change(data, target, "cancel", ACCOUNT, allow_organizer_updates=True)
        self.assertEqual(cancelled.operation, "cancel_event")
        self.assertEqual(str(events(cancelled.data)[0]["STATUS"]), "CANCELLED")

    def test_missing_or_duplicate_self_cannot_accept_invitation(self):
        absent = calendar(base_event("ATTENDEE;PARTSTAT=TENTATIVE:mailto:other@example.invalid"))
        with self.assertRaisesRegex(SafeActionError, "не найден ваш адрес"):
            prepare_change(absent, target_for(absent), "confirm", ACCOUNT)
        duplicate = calendar(base_event(
            f"ATTENDEE;PARTSTAT=TENTATIVE:mailto:{ACCOUNT}",
            f"ATTENDEE;PARTSTAT=TENTATIVE:mailto:{ACCOUNT}",
        ))
        with self.assertRaisesRegex(SafeActionError, "несколько записей"):
            prepare_change(duplicate, target_for(duplicate), "confirm", ACCOUNT)

    def test_confirm_fifth_instance_creates_override_without_changing_master(self):
        data = calendar(event("synthetic",
            "DTSTART;TZID=Europe/Berlin:20261010T110000", "DTEND;TZID=Europe/Berlin:20261010T120000",
            "RRULE:FREQ=DAILY;COUNT=8", "EXDATE;TZID=Europe/Berlin:20261012T110000", "SEQUENCE:4",
            f"ATTENDEE;PARTSTAT=TENTATIVE:mailto:{ACCOUNT}",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "DESCRIPTION:Reminder", "END:VALARM",
        ))
        target = target_for(data)
        change = prepare_change(data, target, "confirm", ACCOUNT)
        original = events(data)[0]
        master, override = events(change.data)
        self.assertEqual(master.to_ical(), original.to_ical())
        self.assertEqual(override.decoded("DTSTART").date(), DAY)
        self.assertEqual(override["RECURRENCE-ID"].params["TZID"], "Europe/Berlin")
        self.assertEqual(override.decoded("RECURRENCE-ID").date(), DAY)
        for name in ("RRULE", "RDATE", "EXDATE", "EXRULE"):
            self.assertNotIn(name, override)
        self.assertEqual(override["ATTENDEE"].params["PARTSTAT"], "ACCEPTED")
        self.assertEqual(override.decoded("SEQUENCE"), 4)
        self.assertEqual(len(override.walk("VALARM")), 1)
        self.assertTrue(verify_change(change.data, target, change, ACCOUNT))

    def test_cancel_recurring_instance_preserves_series_and_siblings(self):
        data = calendar(event("synthetic", "DTSTART:20261010T090000Z", "DTEND:20261010T100000Z",
            "RRULE:FREQ=DAILY;COUNT=8", "SUMMARY:Recurring"))
        target = target_for(data)
        change = prepare_change(data, target, "cancel", ACCOUNT)
        master, override = events(change.data)
        self.assertNotIn("STATUS", master)
        self.assertEqual(str(override["STATUS"]), "CANCELLED")
        self.assertEqual(master.to_ical(), events(data)[0].to_ical())
        self.assertEqual(target_for(change.data, day=date(2026, 10, 15)).expected_event_status, "UNSPECIFIED")
        self.assertTrue(verify_change(change.data, target, change, ACCOUNT))

    def test_existing_moved_override_preserves_rid_and_custom_properties(self):
        master = event("synthetic", "DTSTART:20261010T090000Z", "DTEND:20261010T100000Z",
                       "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:Master")
        override = event("synthetic", "RECURRENCE-ID:20261011T090000Z", "DTSTART:20261014T130000Z",
                         "DURATION:PT2H", "LOCATION:Changed room", "X-PRIVATE-FIELD:Preserve")
        data = calendar(master, override)
        target = target_for(data)
        change = prepare_change(data, target, "reschedule", ACCOUNT, "2026-10-16T17:00:00+03:00")
        updated = events(change.data)[1]
        self.assertEqual(updated["RECURRENCE-ID"].to_ical(), events(data)[1]["RECURRENCE-ID"].to_ical())
        self.assertEqual(str(updated["LOCATION"]), "Changed room")
        self.assertEqual(str(updated["X-PRIVATE-FIELD"]), "Preserve")
        self.assertNotIn("DURATION", updated)
        self.assertEqual(change.after_end, "2026-10-16T19:00:00+03:00")
        self.assertTrue(verify_change(change.data, target, change, ACCOUNT))

    def test_sparse_override_inherits_attendees_for_authorization(self):
        data = calendar(
            event("synthetic", "DTSTART:20261010T090000Z", "RRULE:FREQ=DAILY;COUNT=3",
                  "ORGANIZER:mailto:other@example.invalid", f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{ACCOUNT}"),
            event("synthetic", "RECURRENCE-ID:20261011T090000Z", "DTSTART:20261014T090000Z"),
        )
        target = target_for(data)
        with self.assertRaisesRegex(SafeActionError, "права организатора не подтверждены"):
            prepare_change(data, target, "reschedule", ACCOUNT, "2026-10-15T15:00:00+03:00")
        declined = prepare_change(data, target, "cancel", ACCOUNT)
        self.assertEqual(declined.operation, "decline_participation")
        self.assertEqual(events(declined.data)[1]["ATTENDEE"].params["PARTSTAT"], "DECLINED")

    def test_timezone_alarm_and_unrelated_event_are_preserved(self):
        zone = "\r\n".join([
            "BEGIN:VTIMEZONE", "TZID:Custom/Test", "BEGIN:STANDARD", "DTSTART:19700101T000000",
            "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300", "TZNAME:TEST", "END:STANDARD", "END:VTIMEZONE",
        ])
        chosen = event("synthetic", "DTSTART;TZID=Custom/Test:20261014T120000",
                       "DTEND;TZID=Custom/Test:20261014T130000", "DESCRIPTION:Keep description",
                       "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "DESCRIPTION:Keep alarm", "END:VALARM")
        sibling = event("other", "DTSTART:20261016T090000Z", "SUMMARY:Unrelated")
        data = calendar(zone, chosen, sibling)
        original = Calendar.from_ical(data)
        change = prepare_change(data, target_for(data), "reschedule", ACCOUNT,
                                "2026-10-15T15:00:00+03:00", "2026-10-15T16:30:00+03:00")
        updated = Calendar.from_ical(change.data)
        self.assertEqual(updated.walk("VTIMEZONE")[0].to_ical(), original.walk("VTIMEZONE")[0].to_ical())
        self.assertEqual(updated.walk("VALARM")[0].to_ical(), original.walk("VALARM")[0].to_ical())
        self.assertEqual(updated.walk("VEVENT")[1].to_ical(), original.walk("VEVENT")[1].to_ical())
        self.assertEqual(str(updated.walk("VEVENT")[0]["DESCRIPTION"]), "Keep description")

    def test_untouched_duration_tokens_keep_exact_dst_semantics(self):
        data = calendar(event("synthetic", "DTSTART;TZID=Europe/Berlin:20261024T120000",
            "DURATION:PT24H", "RRULE:FREQ=DAILY;COUNT=3",
            f"ATTENDEE;PARTSTAT=TENTATIVE:mailto:{ACCOUNT}",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT24H", "DESCRIPTION:Reminder", "END:VALARM",
        ))
        before = target_for(data, day=date(2026, 10, 24))
        target = target_for(data, day=date(2026, 10, 26))
        change = prepare_change(data, target, "confirm", ACCOUNT)
        after = target_for(change.data, day=date(2026, 10, 24))
        self.assertEqual(before.expected_end, "2026-10-25T13:00:00+03:00")
        self.assertEqual(after.expected_end, before.expected_end)
        self.assertIn("DURATION:PT24H\r\n", change.data)
        self.assertEqual(change.data.count("TRIGGER:-PT24H\r\n"), 2)

    def test_dst_second_fold_preserves_requested_instant_and_verification_checks_it(self):
        data = calendar(event("synthetic", "DTSTART;TZID=Europe/Berlin:20261014T120000",
            "DTEND;TZID=Europe/Berlin:20261014T130000"))
        target = target_for(data)
        change = prepare_change(data, target, "reschedule", ACCOUNT, "2026-10-25T04:30:00+03:00")
        self.assertIn("DTSTART:20261025T013000Z", change.data)
        self.assertEqual(change.after_start, "2026-10-25T04:30:00+03:00")
        self.assertEqual(change.after_end, "2026-10-25T05:30:00+03:00")
        self.assertTrue(verify_change(change.data, target, change, ACCOUNT))
        claimed_wrong_time = replace(change, after_start="2026-10-25T03:30:00+03:00")
        self.assertFalse(verify_change(change.data, target, claimed_wrong_time, ACCOUNT))

    def test_recurring_point_event_override_has_no_equal_dtend(self):
        data = calendar(event("synthetic", "DTSTART:20261013T090000Z", "RRULE:FREQ=DAILY;COUNT=3",
            f"ATTENDEE;PARTSTAT=TENTATIVE:mailto:{ACCOUNT}"))
        target = target_for(data)
        change = prepare_change(data, target, "confirm", ACCOUNT)
        override = events(change.data)[1]
        self.assertNotIn("DTEND", override)
        self.assertNotIn("DURATION", override)
        self.assertEqual(change.after_start, change.after_end)
        self.assertTrue(verify_change(change.data, target, change, ACCOUNT))

    def test_floating_and_date_recurrence_ids_keep_their_source_type(self):
        floating = calendar(event("synthetic", "DTSTART:20261013T120000", "DURATION:PT1H",
            "RRULE:FREQ=DAILY;COUNT=3"))
        target = target_for(floating)
        moved = prepare_change(floating, target, "reschedule", ACCOUNT, "2026-10-16T15:00:00+03:00")
        override = events(moved.data)[1]
        self.assertIsNone(override.decoded("DTSTART").tzinfo)
        self.assertIsNone(override.decoded("RECURRENCE-ID").tzinfo)
        self.assertEqual(override.decoded("DTSTART").hour, 15)
        self.assertTrue(verify_change(moved.data, target, moved, ACCOUNT))
        whole_day = calendar(event("synthetic", "DTSTART;VALUE=DATE:20261013", "RRULE:FREQ=DAILY;COUNT=3"))
        target = target_for(whole_day)
        moved = prepare_change(whole_day, target, "reschedule", ACCOUNT, "2026-10-16")
        override = events(moved.data)[1]
        self.assertEqual(override["RECURRENCE-ID"].params["VALUE"], "DATE")
        self.assertEqual(override.decoded("RECURRENCE-ID"), DAY)
        self.assertEqual(moved.after_end, "2026-10-17")

    def test_all_day_multi_day_and_rdate_period_keep_effective_duration(self):
        data = calendar(event("synthetic", "DTSTART;VALUE=DATE:20261014", "DTEND;VALUE=DATE:20261017"))
        changed = prepare_change(data, target_for(data), "reschedule", ACCOUNT, "2026-10-20")
        self.assertEqual(changed.after_end, "2026-10-23")
        self.assertEqual(events(changed.data)[0]["DTSTART"].params["VALUE"], "DATE")
        period = calendar(event("synthetic", "DTSTART:20261001T090000Z", "DTEND:20261001T100000Z",
            "RDATE;VALUE=PERIOD:20261013T090000Z/20261015T100000Z"))
        moved = prepare_change(period, target_for(period), "reschedule", ACCOUNT, "2026-10-20T12:00:00+03:00")
        self.assertEqual(moved.after_end, "2026-10-22T13:00:00+03:00")
        self.assertTrue(verify_change(moved.data, target_for(period), moved, ACCOUNT))

    def test_stale_time_status_and_participation_each_conflict(self):
        data = calendar(base_event("STATUS:CONFIRMED", f"ATTENDEE;PARTSTAT=TENTATIVE:mailto:{ACCOUNT}"))
        target = target_for(data)
        variants = (
            data.replace("20261014T090000Z", "20261014T093000Z"),
            data.replace("STATUS:CONFIRMED", "STATUS:TENTATIVE"),
            data.replace("PARTSTAT=TENTATIVE", "PARTSTAT=ACCEPTED"),
        )
        for changed in variants:
            with self.subTest(variant=variants.index(changed)), self.assertRaises(ActionConflict):
                prepare_change(changed, target, "confirm", ACCOUNT)

    def test_current_attendee_set_is_rechecked_even_when_snapshot_fields_match(self):
        data = calendar(base_event("STATUS:CONFIRMED"))
        target = target_for(data)
        changed = data.replace("END:VEVENT", "ATTENDEE:mailto:other@example.invalid\r\nEND:VEVENT")
        with self.assertRaisesRegex(SafeActionError, "права организатора не подтверждены"):
            prepare_change(changed, target, "reschedule", ACCOUNT, "2026-10-15T15:00:00+03:00")

    def test_cancelled_cannot_reopen_and_range_or_whole_series_are_rejected(self):
        data = calendar(base_event("STATUS:CANCELLED"))
        for action in ("confirm", "cancel", "reschedule"):
            with self.subTest(action=action), self.assertRaisesRegex(SafeActionError, "уже отменено"):
                prepare_change(data, target_for(data), action, ACCOUNT)
        series = calendar(event("synthetic", "DTSTART:20261010T090000Z", "RRULE:FREQ=DAILY;COUNT=8"))
        target = replace(target_for(series), recurrence_id=None)
        with self.assertRaisesRegex(SafeActionError, "конкретный экземпляр"):
            prepare_change(series, target, "cancel", ACCOUNT)
        ranged = calendar(event("synthetic", "DTSTART:20261014T090000Z",
            "RECURRENCE-ID;RANGE=THISANDFUTURE:20261013T090000Z"))
        with self.assertRaisesRegex(SafeActionError, "RANGE"):
            prepare_change(ranged, target, "cancel", ACCOUNT)

    def test_invalid_intervals_and_timezone_fail_without_private_data(self):
        data = calendar(base_event())
        target = target_for(data)
        for start, end in (
            ("2026-10-15T15:00:00", None),
            ("2026-10-15T15:00:00+03:00", "2026-10-15T14:00:00+03:00"),
            ("2026-10-15", None),
            ("not-a-date", None),
            ("2026-10-15T15:00:00+03:00", "2026-10-16"),
        ):
            with self.subTest(start=start), self.assertRaises(SafeActionError) as error:
                prepare_change(data, target, "reschedule", ACCOUNT, start, end)
            for private in (ACCOUNT, "Synthetic title", "synthetic"):
                self.assertNotIn(private, str(error.exception))

    def test_verification_ignores_server_timestamps_but_detects_missing_change(self):
        data = calendar(base_event("STATUS:TENTATIVE"))
        target = target_for(data)
        change = prepare_change(data, target, "confirm", ACCOUNT)
        modified = Calendar.from_ical(change.data)
        for item in modified.walk("VEVENT"):
            item.pop("LAST-MODIFIED", None)
            item.pop("DTSTAMP", None)
        self.assertTrue(verify_change(modified.to_ical().decode(), target, change, ACCOUNT))
        self.assertFalse(verify_change(data, target, change, ACCOUNT))
        self.assertFalse(verify_change("not a calendar", target, change, ACCOUNT))

    def test_verification_rejects_unintended_changes_to_series_or_sibling(self):
        data = calendar(
            event("synthetic", "DTSTART:20261010T090000Z", "DTEND:20261010T100000Z",
                  "RRULE:FREQ=DAILY;COUNT=8", "STATUS:CONFIRMED"),
            event("other", "DTSTART:20261020T090000Z", "STATUS:CONFIRMED"),
        )
        target = target_for(data)
        change = prepare_change(data, target, "cancel", ACCOUNT)
        self.assertTrue(verify_change(change.data, target, change, ACCOUNT))
        for corruption in ("master_status", "master_rule", "sibling_status", "missing_sibling"):
            actual = Calendar.from_ical(change.data)
            master, sibling, _ = actual.walk("VEVENT")
            if corruption == "master_status":
                master["STATUS"] = "CANCELLED"
            elif corruption == "master_rule":
                master["RRULE"]["COUNT"] = [6]
            elif corruption == "sibling_status":
                sibling["STATUS"] = "CANCELLED"
            else:
                actual.subcomponents.remove(sibling)
            with self.subTest(corruption=corruption):
                self.assertFalse(verify_change(actual.to_ical().decode(), target, change, ACCOUNT))

    def test_verification_allows_equivalent_oneoff_timezone_and_end_encoding(self):
        data = calendar(base_event("STATUS:TENTATIVE"))
        target = target_for(data)
        change = prepare_change(data, target, "confirm", ACCOUNT)
        actual = change.data.replace("DTSTART:20261014T090000Z", "DTSTART;TZID=Europe/Moscow:20261014T120000")
        actual = actual.replace("DTEND:20261014T100000Z", "DURATION:PT1H")
        self.assertTrue(verify_change(actual, target, change, ACCOUNT))


if __name__ == "__main__":
    unittest.main()
