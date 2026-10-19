"""Read-only participant-preview tests use synthetic invitations and no network."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from html.parser import HTMLParser
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from calendar_digest import control_email_preview as preview
from calendar_digest.calendar_operation_lock import CalendarBusy, calendar_operation_lock
from calendar_digest.calendar_sync import CachedResource, SyncResult
from calendar_digest.control_email_automation import ControlEmailCycleError
from calendar_digest.control_email_preview import (
    ControlEmailPreviewError, ControlEmailPreviewStage, create_control_email_preview,
)
from calendar_digest.readonly_caldav import CalendarConnectionError


DAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 8, tzinfo=ZoneInfo("Europe/Moscow"))


def collection(*, title="Частное контрольное поручение", event_start="20261022T120000", extra=""):
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:private-event-uid\r\n"
        f"DTSTART;TZID=Europe/Moscow:{event_start}\r\nDURATION:PT1H\r\nSUMMARY:{title}\r\n"
        "ATTENDEE;PARTSTAT=ACCEPTED:mailto:executor@example.test\r\n"
        "ATTENDEE;PARTSTAT=DECLINED:mailto:declined@example.test\r\n"
        "ATTENDEE;CUTYPE=ROOM:mailto:room@example.test\r\n"
        "ATTENDEE:mailto:owner@example.test\r\n"
        "ATTENDEE:mailto:gpb.control@mail.ru\r\n"
        f"{extra}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    resource = CachedResource("mailru_2", "private-calendar-id", "Контроль <личный>",
                              "https://calendar.mail.ru/private/event.ics", '"etag"', data)
    return SyncResult([resource], 1, 1, 1, 0)


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ControlEmailPreviewTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "MAILRU_2_USERNAME": "gpb.control@mail.ru", "MAILRU_2_APP_PASSWORD": "synthetic-private-password",
            "MAILRU_1_USERNAME": "owner@example.test",
        }, clear=True))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "preview"

    def run_preview(self, result=None, **kwargs):
        with patch.object(preview, "sync_calendars", return_value=result or collection()) as synchronize:
            result = create_control_email_preview(DAY, self.directory, clock=lambda: NOW, **kwargs)
        synchronize.assert_called_once_with([], source_ids=("mailru_2",))
        return result

    def test_only_control_source_is_read_and_actual_recipients_are_private(self):
        result = self.run_preview()
        self.assertFalse(result["sent"])
        self.assertFalse(result["activated"])
        self.assertEqual(result["reminders_total"], 3)
        self.assertEqual(result["recipients_total"], 1)
        self.assertEqual(result["window_start"], NOW.isoformat())
        self.assertEqual(result["end_date_exclusive"], "2026-10-26")
        payload = json.loads(Path(result["preview_json"]).read_text())
        self.assertEqual({row["recipient"] for row in payload["reminders"]}, {"executor@example.test"})
        self.assertEqual([row["notify_at"] for row in payload["reminders"]], [
            "2026-10-19T09:00:00+03:00", "2026-10-21T09:00:00+03:00", "2026-10-22T11:00:00+03:00",
        ])
        self.assertEqual(payload["subject"], "ВНИМАНИЕ! Контроль и мониторинг поручений!")
        self.assertEqual(payload["importance"], "high")
        encoded = json.dumps(result, ensure_ascii=False)
        for private in ("executor@", "owner@", "private-calendar", "private-event", "Частное", "synthetic-private"):
            self.assertNotIn(private, encoded)
        self.assertNotIn("BEGIN:VCALENDAR", json.dumps(payload))
        self.assertNotIn("synthetic-private-password", json.dumps(payload))
        html = Path(result["preview_html"]).read_text()
        self.assertIn("executor@example.test", html)
        self.assertIn("22.10.2026 в 12:00 МСК", html)
        self.assertIn("Высокая важность", html)
        self.assertIn("Письма не отправлялись, расписание не включалось", html)

    def test_preview_never_sends_activates_or_changes_other_workflows(self):
        self.directory.mkdir()
        preserved = {
            "calendar-state.json": b"digest baseline", "deliveries.sqlite3": b"daily receipts",
            "actions.sqlite3": b"calendar actions", "push-schedule.sqlite3": b"push receipts",
            "control-email.sqlite3": b"participant receipts", ".control-email-scheduler.json": b"heartbeat",
        }
        for name, value in preserved.items():
            (self.directory / name).write_bytes(value)
        with patch("calendar_digest.smtp_delivery.submit_message", side_effect=AssertionError("SMTP forbidden")), \
                patch("calendar_digest.control_email_automation.run_control_email_cycle", side_effect=AssertionError("cycle forbidden")):
            self.run_preview()
        for name, value in preserved.items():
            self.assertEqual((self.directory / name).read_bytes(), value)
        self.assertEqual({item.name for item in self.directory.iterdir()} - preserved.keys(), {
            ".calendar-operation.lock", "control-email-preview-2026-10-19.json", "control-email-preview-2026-10-19.html",
        })

    def test_new_files_private_and_existing_mount_permissions_preserved(self):
        result = self.run_preview()
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        for key in ("preview_json", "preview_html"):
            self.assertEqual(Path(result[key]).stat().st_mode & 0o777, 0o600)
        self.directory.chmod(0o2775)
        self.run_preview()
        self.assertEqual(self.directory.stat().st_mode & 0o7777, 0o2775)

    def test_html_escapes_event_text_and_has_no_active_or_remote_content(self):
        danger = '<img src="https://example.test/pixel" onerror="alert(1)"><script>oops</script> &'
        result = self.run_preview(collection(title=danger))
        html = Path(result["preview_html"]).read_text()
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script&gt;oops&lt;/script&gt;", html)
        self.assertIn("Контроль &lt;личный&gt;", html)
        for tag, attributes in Elements(html).elements:
            self.assertNotIn(tag, {"script", "img", "iframe", "a", "link", "object", "form"})
            self.assertFalse(any(name.startswith("on") for name in attributes))
            self.assertFalse(any(name in attributes for name in ("href", "src")))

    def test_past_times_are_excluded_instead_of_proposing_catchup(self):
        later = NOW.replace(hour=10)
        with patch.object(preview, "sync_calendars", return_value=collection()):
            result = create_control_email_preview(DAY, self.directory, days=1, clock=lambda: later)
        self.assertEqual(result["reminders_total"], 0)
        self.assertEqual(result["window_start"], later.isoformat())
        self.assertIn("На этот день писем нет", Path(result["preview_html"]).read_text())

    def test_collection_delay_excludes_due_times_that_passed_during_fetch(self):
        clock = iter([NOW, NOW.replace(hour=10)])
        with patch.object(preview, "sync_calendars", return_value=collection()):
            result = create_control_email_preview(DAY, self.directory, days=1, clock=lambda: next(clock))
        self.assertEqual(result["reminders_total"], 0)
        self.assertEqual(result["window_start"], NOW.replace(hour=10).isoformat())

    def test_deferred_hourly_rule_is_honestly_shown_at_seven(self):
        now = NOW.replace(hour=0)
        with patch.object(preview, "sync_calendars", return_value=collection(event_start="20261019T040000")):
            result = create_control_email_preview(DAY, self.directory, days=1, clock=lambda: now)
        html = Path(result["preview_html"]).read_text()
        self.assertIn("Перенесено на 07:00 МСК", html)
        self.assertIn("07:00<span>МСК</span>", html)
        self.assertIn("19.10.2026 в 04:00 МСК", html)
        self.assertNotIn("За 1 час", html)

    def test_failed_collection_preserves_both_previous_files(self):
        result = self.run_preview()
        existing = {key: Path(result[key]).read_bytes() for key in ("preview_json", "preview_html")}
        stages = []
        with patch.object(preview, "sync_calendars", side_effect=CalendarConnectionError("safe failure")):
            with self.assertRaises(CalendarConnectionError):
                create_control_email_preview(DAY, self.directory, clock=lambda: NOW, progress=stages.append)
        self.assertEqual(stages, [ControlEmailPreviewStage.COLLECT])
        for key, value in existing.items():
            self.assertEqual(Path(result[key]).read_bytes(), value)

    def test_wrong_source_account_fails_before_fetch(self):
        os.environ["MAILRU_2_USERNAME"] = "different@example.test"
        with patch.object(preview, "sync_calendars") as synchronize:
            with self.assertRaises(ControlEmailCycleError):
                create_control_email_preview(DAY, self.directory, clock=lambda: NOW)
        synchronize.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_window_validation_precedes_directory_or_network(self):
        cases = [("2026-10-19", 7), (NOW, 7), (DAY, 0), (DAY, 32), (DAY, True),
                 (date(9999, 12, 31), 7), (date(2026, 10, 18), 1)]
        with patch.object(preview, "sync_calendars") as synchronize:
            for day, days in cases:
                with self.subTest(day=day, days=days), self.assertRaises(ControlEmailPreviewError):
                    create_control_email_preview(day, self.directory, days=days, clock=lambda: NOW)
        synchronize.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_shared_calendar_gate_prevents_parallel_collection(self):
        gate = Path(self.temporary.name) / "service"
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(gate)}), calendar_operation_lock(gate), \
                patch.object(preview, "sync_calendars") as synchronize:
            with self.assertRaises(CalendarBusy):
                create_control_email_preview(DAY, self.directory, clock=lambda: NOW)
        synchronize.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_fixed_progress_finishes_after_both_outputs_exist(self):
        stages = []
        def progress(stage):
            stages.append(stage)
            if stage is ControlEmailPreviewStage.DONE:
                self.assertTrue((self.directory / "control-email-preview-2026-10-19.json").is_file())
                self.assertTrue((self.directory / "control-email-preview-2026-10-19.html").is_file())
        self.run_preview(progress=progress)
        self.assertEqual(stages, list(ControlEmailPreviewStage))

    def test_storage_failure_and_observer_exception_do_not_leak_private_data(self):
        stages = []
        with patch.object(preview, "sync_calendars", return_value=collection()), \
                patch.object(preview, "write_private", side_effect=PermissionError("/private/account@example.test")):
            with self.assertRaises(ControlEmailPreviewError) as error:
                create_control_email_preview(DAY, self.directory, clock=lambda: NOW, progress=stages.append)
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("@", str(error.exception))
        self.assertNotIn(ControlEmailPreviewStage.DONE, stages)
        def broken(stage):
            raise RuntimeError("private observer content")
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.run_preview(progress=broken)
        self.assertEqual(output.getvalue() + errors.getvalue(), "")

    def test_console_summary_does_not_accept_unknown_planner_private_fields(self):
        actual = preview._plan
        def with_private(*args, **kwargs):
            plan = actual(*args, **kwargs)
            plan.summary["addresses"] = ["private@example.test"]
            return plan
        with patch.object(preview, "_plan", side_effect=with_private):
            result = self.run_preview()
        self.assertNotIn("addresses", result)
        self.assertNotIn("private@example.test", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
