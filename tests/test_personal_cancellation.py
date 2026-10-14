"""Personal cancellation preserves recurrence and unrelated calendar content."""

from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace
import unittest

from icalendar import Calendar

from calendar_digest.event_actions import ActionConflict, EventTarget, SafeActionError
from calendar_digest.personal_cancellation import (
    prepare_personal_cancellation, verify_personal_cancellation,
)
from calendar_digest.selection import select_day


ACCOUNT = "owner@example.invalid"
DAY = date(2026, 10, 15)


def event(*fields, uid="personal"):
    return "\r\n".join([
        "BEGIN:VEVENT", f"UID:{uid}", "DTSTAMP:20261001T000000Z",
        "SUMMARY:Personal test", *fields, "END:VEVENT",
    ])


def calendar(*components):
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Cancellation Tests//EN",
        "X-WR-CALNAME:Synthetic", *components, "END:VCALENDAR", "",
    ])


def selected(data, day=DAY):
    resource = SimpleNamespace(
        source_id="mailru_1", calendar_id="personal", calendar_name="Calendar",
        account_email=ACCOUNT, data=data,
    )
    return select_day([resource], day)


def target_for(data, day=DAY):
    events = [item for item in selected(data, day) if item.uid == "personal"]
    if len(events) > 1:
        events = [item for item in events if item.start[:10] == day.isoformat()]
    if len(events) != 1:
        raise AssertionError("Fixture needs one chosen instance")
    item = events[0]
    return EventTarget(
        source_id=item.source_id, calendar_id=item.calendar_id, uid=item.uid,
        recurrence_id=item.recurrence_id, expected_start=item.start,
        expected_end=item.end, expected_event_status=item.event_status,
        expected_participation=item.participation,
    )


def simple(*fields):
    return calendar(event("DTSTART:20261015T120000Z", "DURATION:PT30M", *fields))


def recurring(*fields):
    return calendar(event(
        "DTSTART:20261014T120000Z", "DURATION:PT30M", "RRULE:FREQ=DAILY;COUNT=4",
        "SEQUENCE:4", *fields,
    ))


class PersonalCancellationTests(unittest.TestCase):
    def test_standalone_delete_archives_current_interval_without_status_put(self):
        data = simple("STATUS:CONFIRMED", "LOCATION:Personal room")
        plan = prepare_personal_cancellation(data, target_for(data), ACCOUNT)
        self.assertEqual(plan.method, "DELETE")
        self.assertEqual(plan.operation, "delete_personal_event")
        self.assertEqual(plan.data, "")
        self.assertEqual(plan.event.event_status, "CANCELLED")
        self.assertEqual(plan.event.location, "Personal room")
        self.assertEqual(plan.event.start, "2026-10-15T15:00:00+03:00")
        self.assertEqual(plan.event.end, "2026-10-15T15:30:00+03:00")
        self.assertEqual(plan.before_start, plan.after_start)
        self.assertFalse(verify_personal_cancellation(data, target_for(data), plan, ACCOUNT))

    def test_source_restriction_and_snapshot_conflict(self):
        data = simple()
        target = target_for(data)
        for source in ("icloud", "mailru_4", "mailru_1_extra", "synthetic"):
            with self.subTest(source=source), self.assertRaises(SafeActionError):
                prepare_personal_cancellation(data, replace(target, source_id=source), ACCOUNT)
        with self.assertRaises(ActionConflict):
            prepare_personal_cancellation(
                data, replace(target, expected_end="2026-10-15T17:00:00+03:00"), ACCOUNT,
            )
        cancelled = simple("STATUS:CANCELLED")
        with self.assertRaisesRegex(SafeActionError, "уже отменено"):
            prepare_personal_cancellation(cancelled, target_for(cancelled), ACCOUNT)

    def test_personal_organizer_requires_exact_identity_and_authority(self):
        for organizer, allowed in ((ACCOUNT, False), ("other@example.invalid", True)):
            data = simple(f"ORGANIZER:mailto:{organizer}")
            with self.subTest(organizer=organizer), self.assertRaises(SafeActionError):
                prepare_personal_cancellation(
                    data, target_for(data), ACCOUNT, allow_organizer_updates=allowed,
                )
        data = simple("ORGANIZER:MAILTO:Owner@Example.Invalid")
        self.assertEqual(prepare_personal_cancellation(
            data, target_for(data), ACCOUNT, allow_organizer_updates=True,
        ).method, "DELETE")
        duplicate = simple(f"ORGANIZER:mailto:{ACCOUNT}", f"ORGANIZER:mailto:{ACCOUNT}")
        with self.assertRaises(SafeActionError):
            prepare_personal_cancellation(
                duplicate, target_for(duplicate), ACCOUNT, allow_organizer_updates=True,
            )

    def test_any_participants_block_deletion_even_owner_organizer(self):
        for attendee in (ACCOUNT, "other@example.invalid"):
            data = simple(f"ORGANIZER:mailto:{ACCOUNT}", f"ATTENDEE:mailto:{attendee}")
            with self.subTest(attendee=attendee), self.assertRaisesRegex(SafeActionError, "без участников"):
                prepare_personal_cancellation(
                    data, target_for(data), ACCOUNT, allow_organizer_updates=True,
                )

    def test_delete_rejects_other_event_or_non_event_record(self):
        chosen = event("DTSTART:20261015T120000Z")
        extras = (
            event("DTSTART:20261016T120000Z", uid="unrelated"),
            "BEGIN:VTODO\r\nUID:task\r\nSUMMARY:Independent task\r\nEND:VTODO",
            "BEGIN:VJOURNAL\r\nUID:note\r\nSUMMARY:Independent note\r\nEND:VJOURNAL",
        )
        for extra in extras:
            data = calendar(chosen, extra)
            with self.subTest(extra=extra), self.assertRaisesRegex(SafeActionError, "другие записи"):
                prepare_personal_cancellation(data, target_for(data), ACCOUNT)

    def test_delete_permits_owned_alarm_and_timezone(self):
        zone = "\r\n".join([
            "BEGIN:VTIMEZONE", "TZID:Custom/Cancellation", "BEGIN:STANDARD",
            "DTSTART:19700101T000000", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300",
            "END:STANDARD", "END:VTIMEZONE",
        ])
        data = calendar(zone, event(
            "DTSTART;TZID=Custom/Cancellation:20261015T150000", "DURATION:PT30M",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M",
            "DESCRIPTION:Reminder", "END:VALARM",
        ))
        self.assertEqual(prepare_personal_cancellation(data, target_for(data), ACCOUNT).method, "DELETE")

    def test_repeat_excludes_one_slot_keeps_rules_alarm_and_siblings(self):
        data = recurring(
            "STATUS:CONFIRMED", "EXDATE:20261017T120000Z", "X-PERSONAL:Keep me",
            "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT24H",
            "DESCRIPTION:Reminder", "END:VALARM",
        )
        target = target_for(data)
        plan = prepare_personal_cancellation(data, target, ACCOUNT)
        self.assertEqual(plan.method, "PUT")
        self.assertEqual(plan.operation, "exclude_personal_occurrence")
        self.assertNotIn("CANCELLED", plan.data)
        self.assertEqual(plan.event.event_status, "CANCELLED")
        self.assertTrue(plan.event.recurring)
        master = Calendar.from_ical(plan.data).walk("VEVENT")[0]
        self.assertEqual(master.decoded("SEQUENCE"), 5)
        self.assertEqual(master["RRULE"].to_ical(), b"FREQ=DAILY;COUNT=4")
        self.assertIn("EXDATE:20261015T120000Z", plan.data)
        self.assertIn("EXDATE:20261017T120000Z", plan.data)
        self.assertIn("TRIGGER:-PT24H", plan.data)
        self.assertIn("X-PERSONAL:Keep me", plan.data)
        self.assertFalse(selected(plan.data))
        for day in (date(2026, 10, 14), date(2026, 10, 16)):
            self.assertEqual(selected(plan.data, day), selected(data, day))
        self.assertTrue(verify_personal_cancellation(plan.data, target, plan, ACCOUNT))

    def test_moved_override_archives_move_and_excludes_original_slot_only(self):
        data = calendar(
            event("DTSTART:20261010T120000Z", "DURATION:PT30M", "RRULE:FREQ=DAILY;COUNT=3"),
            event("RECURRENCE-ID:20261011T120000Z", "DTSTART:20261015T130000Z",
                  "DURATION:PT1H", "LOCATION:Moved room"),
            event("RECURRENCE-ID:20261012T120000Z", "DTSTART:20261016T140000Z", "DURATION:PT2H"),
        )
        target = target_for(data)
        plan = prepare_personal_cancellation(data, target, ACCOUNT)
        self.assertEqual(plan.event.start, "2026-10-15T16:00:00+03:00")
        self.assertEqual(plan.event.original_start, "2026-10-11T15:00:00+03:00")
        self.assertEqual(plan.event.original_end, "2026-10-11T15:30:00+03:00")
        self.assertIn("EXDATE:20261011T120000Z", plan.data)
        self.assertNotIn("EXDATE:20261015", plan.data)
        self.assertEqual(len(Calendar.from_ical(plan.data).walk("VEVENT")), 2)
        self.assertFalse(selected(plan.data, date(2026, 10, 11)))
        self.assertFalse(selected(plan.data, DAY))
        self.assertEqual(selected(plan.data, date(2026, 10, 16)), selected(data, date(2026, 10, 16)))
        self.assertTrue(verify_personal_cancellation(plan.data, target, plan, ACCOUNT))

    def test_missing_master_never_deletes_detached_occurrence(self):
        data = calendar(event("RECURRENCE-ID:20261011T120000Z", "DTSTART:20261015T130000Z"))
        with self.assertRaisesRegex(SafeActionError, "основная запись"):
            prepare_personal_cancellation(data, target_for(data), ACCOUNT)

    def test_sparse_and_other_overrides_with_attendees_block_personal_cancellation(self):
        master = event("DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=4")
        for participant_component in (
            event("RECURRENCE-ID:20261015T120000Z", "DTSTART:20261015T120000Z",
                  f"ATTENDEE:mailto:{ACCOUNT}"),
            event("RECURRENCE-ID:20261016T120000Z", "DTSTART:20261016T130000Z",
                  f"ATTENDEE:mailto:{ACCOUNT}"),
        ):
            data = calendar(master, participant_component)
            with self.subTest(component=participant_component), self.assertRaisesRegex(SafeActionError, "без участников"):
                prepare_personal_cancellation(data, target_for(data), ACCOUNT)
        data = calendar(
            event("DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=4", f"ATTENDEE:mailto:{ACCOUNT}"),
            event("RECURRENCE-ID:20261015T120000Z", "DTSTART:20261015T120000Z"),
        )
        with self.assertRaisesRegex(SafeActionError, "без участников"):
            prepare_personal_cancellation(data, target_for(data), ACCOUNT)

    def test_exdate_preserves_all_day_floating_and_named_timezone(self):
        for fields, expected in (
            (("DTSTART;VALUE=DATE:20261014", "DTEND;VALUE=DATE:20261015"), "EXDATE;VALUE=DATE:20261015"),
            (("DTSTART:20261014T150000", "DURATION:PT30M"), "EXDATE:20261015T150000"),
            (("DTSTART;TZID=Europe/Berlin:20261014T140000", "DURATION:PT30M"),
             "EXDATE;TZID=Europe/Berlin:20261015T140000"),
        ):
            data = calendar(event(*fields, "RRULE:FREQ=DAILY;COUNT=4"))
            with self.subTest(fields=fields):
                target = target_for(data)
                plan = prepare_personal_cancellation(data, target, ACCOUNT)
                self.assertIn(expected, plan.data)
                self.assertFalse(selected(plan.data))
                self.assertEqual(selected(plan.data, date(2026, 10, 16)), selected(data, date(2026, 10, 16)))
                self.assertTrue(verify_personal_cancellation(plan.data, target, plan, ACCOUNT))

    def test_rdate_periods_and_exact_duration_are_preserved(self):
        data = calendar(event(
            "DTSTART:20261014T120000Z", "DURATION:PT24H",
            "RDATE;VALUE=PERIOD:20261015T120000Z/PT24H,20261017T120000Z/PT2H",
        ))
        target = target_for(data)
        plan = prepare_personal_cancellation(data, target, ACCOUNT)
        self.assertIn("RDATE;VALUE=PERIOD:20261015T120000Z/PT24H,20261017T120000Z/PT2H", plan.data)
        self.assertIn("DURATION:PT24H", plan.data)
        self.assertEqual(selected(plan.data, date(2026, 10, 17)), selected(data, date(2026, 10, 17)))
        self.assertTrue(verify_personal_cancellation(plan.data, target, plan, ACCOUNT))

    def test_repeat_without_specific_rid_and_range_overrides_are_rejected(self):
        data = recurring()
        target = replace(target_for(data), recurrence_id=None)
        with self.assertRaises(SafeActionError):
            prepare_personal_cancellation(data, target, ACCOUNT)
        ranged = calendar(
            event("DTSTART:20261014T120000Z", "RRULE:FREQ=DAILY;COUNT=4"),
            event("RECURRENCE-ID;RANGE=THISANDFUTURE:20261015T120000Z", "DTSTART:20261015T130000Z"),
        )
        with self.assertRaisesRegex(SafeActionError, "RANGE"):
            prepare_personal_cancellation(ranged, target_for(data), ACCOUNT)

    def test_put_keeps_unrelated_event_timezone_and_calendar_fields(self):
        original = recurring()
        source = Calendar.from_ical(original)
        unrelated = Calendar.from_ical(calendar(event(
            "DTSTART:20261016T170000Z", "DESCRIPTION:Keep unrelated", uid="other",
        ))).walk("VEVENT")[0]
        source.add_component(unrelated)
        data = source.to_ical().decode()
        plan = prepare_personal_cancellation(data, target_for(data), ACCOUNT)
        self.assertEqual(Calendar.from_ical(plan.data).walk("VEVENT")[1].to_ical(), unrelated.to_ical())
        self.assertEqual(Calendar.from_ical(plan.data)["X-WR-CALNAME"], source["X-WR-CALNAME"])

    def test_custom_timezone_exclusion_retains_definition_and_alarm(self):
        zone = "\r\n".join([
            "BEGIN:VTIMEZONE", "TZID:Custom/RecurringCancellation", "BEGIN:STANDARD",
            "DTSTART:19700101T000000", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0300",
            "TZNAME:Custom", "END:STANDARD", "END:VTIMEZONE",
        ])
        data = calendar(zone, event(
            "DTSTART;TZID=Custom/RecurringCancellation:20261014T150000", "DURATION:PT30M",
            "RRULE:FREQ=DAILY;COUNT=4", "BEGIN:VALARM", "ACTION:DISPLAY",
            "TRIGGER:-PT15M", "DESCRIPTION:Reminder", "END:VALARM",
        ))
        target = target_for(data)
        plan = prepare_personal_cancellation(data, target, ACCOUNT)
        original, updated = Calendar.from_ical(data), Calendar.from_ical(plan.data)
        self.assertIn("EXDATE;TZID=Custom/RecurringCancellation:20261015T150000", plan.data)
        self.assertEqual(original.walk("VTIMEZONE")[0].to_ical(), updated.walk("VTIMEZONE")[0].to_ical())
        self.assertEqual(original.walk("VALARM")[0].to_ical(), updated.walk("VALARM")[0].to_ical())
        self.assertTrue(verify_personal_cancellation(plan.data, target, plan, ACCOUNT))
        for text in (
            plan.data.replace("TZOFFSETTO:+0300", "TZOFFSETTO:+0400"),
            plan.data.replace("TRIGGER:-PT15M", "TRIGGER:-PT30M"),
        ):
            self.assertFalse(verify_personal_cancellation(text, target, plan, ACCOUNT))

    def test_ambiguous_master_or_override_cannot_produce_deletion(self):
        data = recurring()
        target = target_for(data)
        master = Calendar.from_ical(data).walk("VEVENT")[0].to_ical().decode()
        duplicate_master = calendar(master, master)
        override = event("RECURRENCE-ID:20261015T120000Z", "DTSTART:20261015T130000Z")
        duplicate_override = calendar(master, override, override)
        for ambiguous in (duplicate_master, duplicate_override):
            with self.subTest(ambiguous=ambiguous), self.assertRaises(SafeActionError):
                prepare_personal_cancellation(ambiguous, target, ACCOUNT)

    def test_readback_rejects_resurrected_slot_changed_rules_and_content_loss(self):
        data = recurring("DESCRIPTION:Keep original", "LOCATION:Keep room")
        target = target_for(data)
        plan = prepare_personal_cancellation(data, target, ACCOUNT)
        self.assertTrue(verify_personal_cancellation(plan.data, target, plan, ACCOUNT))
        self.assertFalse(verify_personal_cancellation(data, target, plan, ACCOUNT))
        for altered in (
            plan.data.replace("COUNT=4", "COUNT=2"),
            plan.data.replace("EXDATE:20261015T120000Z", "EXDATE:20261016T120000Z"),
            plan.data.replace("DESCRIPTION:Keep original\r\n", ""),
            plan.data.replace("LOCATION:Keep room", "LOCATION:Wrong room"),
            plan.data.replace("PRODID:-//Cancellation Tests//EN", "PRODID:Unexpected"),
            "invalid ICS",
        ):
            with self.subTest(altered=altered):
                self.assertFalse(verify_personal_cancellation(altered, target, plan, ACCOUNT))

    def test_readback_allows_only_observed_timestamp_and_ack_changes(self):
        data = recurring()
        target = target_for(data)
        plan = prepare_personal_cancellation(data, target, ACCOUNT)
        actual = Calendar.from_ical(plan.data)
        master = actual.walk("VEVENT")[0]
        master.pop("DTSTAMP", None)
        master.add("DTSTAMP", datetime.fromisoformat("2026-10-15T17:00:00+00:00"))
        master.add("X-MOZ-LASTACK", "20261015T120000Z")
        self.assertTrue(verify_personal_cancellation(actual.to_ical().decode(), target, plan, ACCOUNT))
        master.pop("LAST-MODIFIED", None)
        self.assertFalse(verify_personal_cancellation(actual.to_ical().decode(), target, plan, ACCOUNT))


if __name__ == "__main__":
    unittest.main()
