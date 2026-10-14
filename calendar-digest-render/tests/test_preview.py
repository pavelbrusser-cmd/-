import json
from dataclasses import replace
from datetime import date
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest.calendar_collection import CalendarResource, Collection
from calendar_digest.local_state import (
    Snapshot, StateError, advance_snapshot, read_snapshot, snapshot_json, write_private,
)
from calendar_digest.preview import PreviewError, create_preview
from calendar_digest.readonly_caldav import CalendarConnectionError
from calendar_digest.selection import select_day


def resource(start="20261014T090000Z", title="Встреча", email="account@example.test"):
    data = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:one\r\n"
            f"DTSTART:{start}\r\nDURATION:PT1H\r\nSUMMARY:{title}\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n")
    return CalendarResource(source_id="icloud", calendar_id="c", calendar_name="Дом",
                            account_email=email, data=data)


class SnapshotTests(unittest.TestCase):
    def test_master_and_detached_override_share_uid_without_snapshot_collision(self):
        master = resource()
        master = replace(master, data=master.data.replace("DURATION:PT1H", "DURATION:PT1H\r\nRRULE:FREQ=DAILY;COUNT=2"))
        override = resource("20261015T120000Z")
        override = replace(override, data=override.data.replace("DURATION:PT1H", "DURATION:PT1H\r\nRECURRENCE-ID:20261014T090000Z"))
        snapshot = advance_snapshot([master, override], Snapshot([], []))
        again = advance_snapshot([master, override], snapshot)
        selected = select_day(again.current, date(2026, 10, 14), previous_resources=again.previous)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].move_basis, "recurrence_id")
        self.assertTrue(selected[0].start.startswith("2026-10-15T15:00"))

    def test_remembers_last_start_across_reads_and_unrelated_edits(self):
        first = resource()
        second = resource("20261015T110000Z")
        baseline = advance_snapshot([first], Snapshot([], []))
        moved = advance_snapshot([second], baseline)
        again = advance_snapshot([resource("20261015T110000Z", "Новое название")], moved)
        selected = select_day(again.current, date(2026, 10, 14), previous_resources=again.previous)
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0].original_start.startswith("2026-10-14T12:00"))
        self.assertTrue(selected[0].start.startswith("2026-10-15T14:00"))
        self.assertEqual(selected[0].title, "Новое название")
        third = advance_snapshot([resource("20261016T110000Z")], again)
        selected = select_day(third.current, date(2026, 10, 15), previous_resources=third.previous)
        self.assertTrue(selected[0].original_start.startswith("2026-10-15T14:00"))

    def test_state_does_not_copy_account_login_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            value = resource()
            write_private(path, snapshot_json(Snapshot([value], [])))
            self.assertNotIn(value.account_email, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            restored = read_snapshot(path)
            self.assertEqual(restored.current[0].data, value.data)
            self.assertEqual(restored.current[0].account_email, "")

    def test_corrupt_state_is_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("broken")
            with self.assertRaises(StateError):
                read_snapshot(path)
            self.assertEqual(path.read_text(), "broken")


class PreviewTests(unittest.TestCase):
    def test_preview_escapes_event_markup_and_returns_only_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            collection = Collection(resources=[resource(title="<script>alert(1)</script> [link](https://example.test)")],
                                    calendar_count=1)
            with patch("calendar_digest.preview.collect_all", return_value=collection):
                summary = create_preview(date(2026, 10, 14), path)
            self.assertEqual(summary["events"], 1)
            self.assertNotIn("alert", json.dumps(summary))
            output = Path(summary["preview_markdown"]).read_text()
            self.assertNotIn("<script>", output)
            self.assertIn("&lt;script&gt;", output)
            self.assertIn(r"\[link\]", output)
            self.assertTrue(Path(summary["preview_json"]).exists())
            self.assertEqual((path / "calendar-state.json").stat().st_mode & 0o777, 0o600)

    def test_source_failure_preserves_baseline_and_previous_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch("calendar_digest.preview.collect_all", return_value=Collection([resource()], 1)):
                create_preview(date(2026, 10, 14), path)
            files = {item: item.read_bytes() for item in path.iterdir() if item.suffix in (".json", ".md")}
            with patch("calendar_digest.preview.collect_all", side_effect=CalendarConnectionError("Недоступен источник")):
                with self.assertRaises(CalendarConnectionError):
                    create_preview(date(2026, 10, 14), path)
            self.assertEqual(files, {item: item.read_bytes() for item in files})

    def test_simultaneous_collection_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with (path / ".preview.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("calendar_digest.preview.collect_all") as collect:
                    with self.assertRaises(PreviewError):
                        create_preview(date(2026, 10, 14), path)
                    collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
