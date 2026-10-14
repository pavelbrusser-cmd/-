from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest.action_store import ActionStore, StoreError
from calendar_digest.calendar_collection import CalendarResource, Collection
from calendar_digest.cancellation_archive import merge_cancellations
from calendar_digest.preview import create_preview
from calendar_digest.selection import select_day


DAY = date(2026, 10, 15)


def resource(*, source="mailru_1", calendar="calendar", start="20261015T120000Z", extra="", override=""):
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:fixture\r\n"
        f"DTSTART:{start}\r\nDURATION:PT30M\r\nSUMMARY:Private fixture\r\n"
        f"STATUS:CONFIRMED\r\n{extra}END:VEVENT\r\n{override}END:VCALENDAR\r\n"
    )
    return CalendarResource(source, calendar, "Calendar", "owner@example.test", data)


def target(event):
    return {
        "source_id": event.source_id, "calendar_id": event.calendar_id, "uid": event.uid,
        "recurrence_id": event.recurrence_id, "expected_start": event.start,
        "expected_end": event.end, "expected_event_status": "CONFIRMED",
        "expected_participation": event.participation,
    }


class CancellationArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.key = secrets.token_urlsafe(48)
        self.store = ActionStore(self.path / "actions.sqlite3", self.key)
        self.resource = resource()
        self.event = replace(select_day([self.resource], DAY)[0], event_status="CANCELLED")

    def stage(self, event=None, original=None, state="done"):
        event = event or self.event
        original = original or self.resource
        token = self.store.issue(target(event), "cancel")
        self.store.claim(token)
        self.store.stage_cancellation(token, event, original.data)
        if state == "done":
            self.store.complete(token, {"operation": "cancelled"})
        elif state in {"failed", "uncertain"}:
            self.store.fail(token, {"operation": "not_verified"}, uncertain=state == "uncertain")
        return token

    def test_confirmed_archive_survives_restart_and_preserves_exact_private_backup(self):
        token = self.stage()
        reopened = ActionStore(self.store.path, self.key)
        self.assertEqual(reopened.read(token)["state"], "done")
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [self.event])
        with sqlite3.connect(self.store.path) as connection:
            saved = connection.execute("SELECT original_data FROM cancellation_archive").fetchone()[0]
        self.assertEqual(saved, self.resource.data)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.resource.data, json.dumps(reopened.read(token)))

    def test_pending_failed_and_uncertain_backups_are_durable_but_invisible(self):
        for state in ("running", "failed", "uncertain"):
            self.stage(state=state)
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [])
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cancellation_archive").fetchone()[0], 3)

    def test_archive_cannot_replace_backup_or_stage_other_target_action_or_state(self):
        token = self.stage(state="running")
        self.store.stage_cancellation(token, self.event, self.resource.data)
        with self.assertRaises(StoreError):
            self.store.stage_cancellation(token, replace(self.event, title="Changed"), self.resource.data)
        for action, claim in (("cancel", False), ("reschedule", True)):
            other = self.store.issue(target(self.event), action)
            if claim:
                self.store.claim(other)
            with self.assertRaises(StoreError):
                self.store.stage_cancellation(other, self.event, self.resource.data)
        for changes in ({"uid": "other"}, {"source_id": "mailru_2"}, {"expected_event_status": "TENTATIVE"}):
            other = self.store.issue({**target(self.event), **changes}, "cancel")
            self.store.claim(other)
            with self.assertRaises(StoreError):
                self.store.stage_cancellation(other, self.event, self.resource.data)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cancellation_archive").fetchone()[0], 1)

    def test_failed_insert_and_failed_receipt_never_activate_archive(self):
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("CREATE TRIGGER deny_backup BEFORE INSERT ON cancellation_archive BEGIN SELECT RAISE(ABORT, 'private SQL message'); END")
        token = self.store.issue(target(self.event), "cancel")
        self.store.claim(token)
        with self.assertRaises(StoreError) as error:
            self.store.stage_cancellation(token, self.event, self.resource.data)
        self.assertNotIn("private", str(error.exception))
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cancellation_archive").fetchone()[0], 0)
            connection.execute("DROP TRIGGER deny_backup")
        self.store.stage_cancellation(token, self.event, self.resource.data)
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("CREATE TRIGGER deny_receipt BEFORE UPDATE ON actions WHEN NEW.state='done' BEGIN SELECT RAISE(ABORT, 'private SQL message'); END")
        with self.assertRaises(StoreError):
            self.store.complete(token, {"message": "done"})
        self.assertEqual(self.store.read(token)["state"], "running")
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [])
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cancellation_archive").fetchone()[0], 1)

    def test_absent_archive_does_not_create_files_or_require_key_and_old_database_works(self):
        absent = self.path / "not-created"
        self.assertEqual(merge_cancellations([], [], DAY, absent), [])
        self.assertFalse(absent.exists())
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("DROP TABLE cancellation_archive")
        before = self.store.path.read_bytes()
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [])
        self.assertEqual(before, self.store.path.read_bytes())

    def test_restored_event_wins_even_when_moved_outside_digest_day(self):
        self.stage()
        moved = resource(start="20261017T120000Z")
        self.assertEqual(merge_cancellations([], [moved], DAY, self.path), [])
        live = select_day([self.resource], DAY)
        self.assertEqual(merge_cancellations(live, [self.resource], DAY, self.path), live)

    def test_same_uid_in_other_source_or_calendar_does_not_hide_cancellation(self):
        self.stage()
        others = [resource(source="mailru_2"), resource(calendar="other")]
        merged = merge_cancellations(select_day(others, DAY), others, DAY, self.path)
        self.assertEqual(len(merged), 3)
        self.assertEqual(sum(event.event_status == "CANCELLED" for event in merged), 1)

    def test_only_excluded_occurrence_is_archived_and_restored_override_wins(self):
        original = resource(extra="RRULE:FREQ=DAILY;COUNT=3\r\n")
        day = date(2026, 10, 16)
        event = replace(select_day([original], day)[0], event_status="CANCELLED")
        self.stage(event, original)
        excluded = replace(original, data=original.data.replace("RRULE:", "EXDATE:20261016T120000Z\r\nRRULE:"))
        self.assertEqual(merge_cancellations(select_day([excluded], day), [excluded], day, self.path), [event])
        next_day = date(2026, 10, 17)
        following = select_day([excluded], next_day)
        self.assertEqual(merge_cancellations(following, [excluded], next_day, self.path), following)
        self.assertEqual(merge_cancellations([], [original], day, self.path), [])
        override = (
            "BEGIN:VEVENT\r\nUID:fixture\r\nRECURRENCE-ID:20261016T120000Z\r\n"
            "DTSTART:20261020T120000Z\r\nDURATION:PT30M\r\nSTATUS:CONFIRMED\r\nEND:VEVENT\r\n"
        )
        moved = replace(excluded, data=excluded.data.replace("END:VCALENDAR", override + "END:VCALENDAR"))
        self.assertEqual(merge_cancellations([], [moved], day, self.path), [])

    def test_duplicate_successful_actions_render_once_with_newest_data(self):
        self.stage()
        second = replace(self.event, title="Latest title")
        self.stage(second)
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [second])

    def test_moved_recurring_event_appears_on_original_and_new_days(self):
        override = (
            "BEGIN:VEVENT\r\nUID:fixture\r\nRECURRENCE-ID:20261015T120000Z\r\n"
            "DTSTART:20261018T120000Z\r\nDURATION:PT30M\r\nSTATUS:CONFIRMED\r\nEND:VEVENT\r\n"
        )
        original = resource(extra="RRULE:FREQ=DAILY;COUNT=2\r\n", override=override)
        event = replace(select_day([original], DAY)[0], event_status="CANCELLED")
        self.stage(event, original)
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [event])
        self.assertEqual(merge_cancellations([], [], date(2026, 10, 18), self.path), [event])
        self.assertEqual(merge_cancellations([], [], date(2026, 10, 17), self.path), [])

    def test_signed_context_preserves_standalone_move_history_after_restart(self):
        context = replace(
            self.event, event_status="CONFIRMED", original_start="2026-10-14T15:00:00+03:00",
            original_end="2026-10-14T15:30:00+03:00", move_basis="previous_snapshot",
            timezone_assumed=True,
        )
        token = self.store.issue(target(context), "cancel", digest_event=context)
        # A later process only has the current raw calendar resource, which no
        # longer carries the previous start of an ordinary nonrecurring event.
        reopened = ActionStore(self.store.path, self.key)
        reopened.claim(token)
        reopened.stage_cancellation(token, self.event, self.resource.data)
        reopened.complete(token, {"operation": "cancelled"})
        cancelled = replace(context, event_status="CANCELLED")
        for day in (date(2026, 10, 14), DAY):
            self.assertEqual(merge_cancellations([], [], day, self.path), [cancelled])

    def test_existing_recurrence_move_metadata_takes_precedence_over_context(self):
        context = replace(
            self.event, event_status="CONFIRMED", original_start="2026-10-13T15:00:00+03:00",
            original_end="2026-10-13T15:30:00+03:00", move_basis="previous_snapshot",
        )
        actual = replace(
            self.event, original_start="2026-10-14T15:00:00+03:00",
            original_end="2026-10-14T15:30:00+03:00", move_basis="recurrence_id",
        )
        token = self.store.issue(target(context), "cancel", digest_event=context)
        self.store.claim(token)
        self.store.stage_cancellation(token, actual, self.resource.data)
        self.store.complete(token, {"operation": "cancelled"})
        self.assertEqual(merge_cancellations([], [], DAY, self.path), [actual])

    def test_stale_invalid_context_rejected_before_archive_and_issue_is_atomic(self):
        context = replace(self.event, event_status="CONFIRMED")
        for wrong in (replace(context, uid="other"), replace(context, start="2026-10-15T14:00:00+03:00")):
            with self.assertRaises(StoreError):
                self.store.issue(target(context), "cancel", digest_event=wrong)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
            connection.execute("CREATE TRIGGER deny_context BEFORE INSERT ON action_contexts BEGIN SELECT RAISE(ABORT, 'private SQL message'); END")
        with self.assertRaises(StoreError):
            self.store.issue(target(context), "cancel", digest_event=context)
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
            connection.execute("DROP TRIGGER deny_context")
        for invalid in ("private malformed JSON", json.dumps({**asdict(context), "uid": "other"}),
                        json.dumps({**asdict(context), "password": "private value"}),
                        '{"uid":"fixture",' + json.dumps(asdict(context))[1:]):
            token = self.store.issue(target(context), "cancel", digest_event=context)
            self.store.claim(token)
            with sqlite3.connect(self.store.path) as connection:
                connection.execute("UPDATE action_contexts SET event_json=? WHERE action_id=?", (invalid, token.split(".")[0]))
            with self.assertRaises(StoreError) as error:
                self.store.stage_cancellation(token, self.event, self.resource.data)
            self.assertNotIn("private", str(error.exception))
        with sqlite3.connect(self.store.path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM cancellation_archive").fetchone()[0], 0)

    def test_all_day_multiday_and_midnight_exclusive_boundaries(self):
        original = replace(self.resource, data=self.resource.data.replace(
            "DTSTART:20261015T120000Z\r\nDURATION:PT30M", "DTSTART;VALUE=DATE:20261015\r\nDTEND;VALUE=DATE:20261017"
        ))
        event = replace(select_day([original], DAY)[0], event_status="CANCELLED")
        self.stage(event, original)
        for day, expected in ((date(2026, 10, 14), []), (DAY, [event]),
                              (date(2026, 10, 16), [event]), (date(2026, 10, 17), [])):
            self.assertEqual(merge_cancellations([], [], day, self.path), expected)
        timed = resource(source="mailru_2", start="20261015T203000Z")
        timed_event = replace(select_day([timed], DAY)[0], event_status="CANCELLED")
        self.stage(timed_event, timed)
        self.assertIn(timed_event, merge_cancellations([], [], DAY, self.path))
        self.assertNotIn(timed_event, merge_cancellations([], [], date(2026, 10, 16), self.path))

    def test_invalid_schema_timezone_dates_backup_and_status_fail_safely(self):
        token = self.store.issue(target(self.event), "cancel")
        self.store.claim(token)
        for changes in (
            {"event_status": "CONFIRMED"}, {"all_day": 1}, {"all_day": True},
            {"start": "2026-10-15T15:00:00"}, {"end": "2026-10-14T15:00:00+03:00"},
            {"recurrence_id": "2026-10-15"}, {"original_end": "2026-10-15"},
        ):
            with self.assertRaises(StoreError):
                self.store.stage_cancellation(token, replace(self.event, **changes), self.resource.data)
        for raw in ("private value", self.resource.data.replace("UID:fixture", "UID:other"),
                    "x" * (4 * 1024 * 1024 + 1), self.resource.data.replace("STATUS:", "ATTENDEE:mailto:guest@example.test\r\nSTATUS:")):
            with self.assertRaises(StoreError):
                self.store.stage_cancellation(token, self.event, raw)
        self.stage()
        with sqlite3.connect(self.store.path) as connection:
            connection.execute("UPDATE cancellation_archive SET event_json=?", (json.dumps({**asdict(self.event), "password": "private value"}),))
        with self.assertRaises(StoreError) as error:
            merge_cancellations([], [], DAY, self.path)
        self.assertNotIn("private", str(error.exception))

    def test_preview_uses_state_directory_when_output_is_elsewhere(self):
        self.stage()
        output = self.path / "output"
        with patch.dict("os.environ", {"DIGEST_STATE_DIR": str(self.path)}), patch(
            "calendar_digest.preview.collect_all", return_value=Collection([], 11)
        ):
            result = create_preview(DAY, output)
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["event_statuses"], {"CANCELLED": 1})
        self.assertFalse((output / "actions.sqlite3").exists())
        payload = json.loads(Path(result["preview_json"]).read_text())
        self.assertEqual(payload["events"], [asdict(self.event)])


if __name__ == "__main__":
    unittest.main()
