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

from calendar_digest.calendar_collection import CalendarResource, Collection
from calendar_digest.calendar_operation_lock import CalendarBusy, calendar_operation_lock
from calendar_digest.contact_birthdays_source import BirthdayCollection, ContactBirthday
from calendar_digest import reminder_preview as preview
from calendar_digest.reminder_preview import (
    ReminderPreviewError, ReminderPreviewStage, create_reminder_preview,
)
from calendar_digest.readonly_caldav import CalendarConnectionError


def resource(uid, start, title, *, source="mailru_2", extra="", calendar="Контроль поручений"):
    data = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART{start}\r\nSUMMARY:{title}\r\n{extra}"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return CalendarResource(
        source_id=source, calendar_id="private-calendar-id", calendar_name=calendar,
        account_email="private-login@example.test", data=data,
    )


def collection():
    return Collection([
        resource("birthday-uid", ";VALUE=DATE:20261018", "День рождения — пример",
                 source="icloud", calendar="Личные даты", extra="CATEGORIES:Birthday\r\n"),
        resource("control-uid", ";TZID=Europe/Moscow:20261016T020000", "Ночное поручение"),
        resource("periodic-uid", ";TZID=Europe/Moscow:20261020T110000", "Weekly — отчёт"),
    ], 2)


class Elements(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ReminderPreviewTests(unittest.TestCase):
    def setUp(self):
        # Build-time Render variables must not reach live accounts or /var/data.
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "private-preview"

    def run_preview(self, resources=None, **kwargs):
        with patch.object(preview, "collect_all", return_value=resources or collection()) as collect:
            result = create_reminder_preview(date(2026, 10, 15), self.directory, **kwargs)
        collect.assert_called_once_with()
        return result

    def test_fresh_calendar_preview_contains_actual_schedule_and_safe_summary(self):
        result = self.run_preview()
        self.assertFalse(result["sent"])
        self.assertEqual(result["start_date"], "2026-10-15")
        self.assertEqual(result["end_date_exclusive"], "2026-10-22")
        self.assertEqual(result["timezone"], "Europe/Moscow")
        self.assertEqual(result["calendars"], 2)
        self.assertEqual(result["resources"], 3)
        self.assertEqual(result["reminders_total"], 7)
        self.assertEqual(result["by_category"], {
            "birthdays": 3, "control": 2, "periodic_control": 2, "google_events": 0,
        })
        payload = json.loads(Path(result["preview_json"]).read_text())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["reminders"]), 7)
        self.assertTrue(all(len(item["stable_id"]) == 64 for item in payload["reminders"]))
        self.assertTrue(all(datetime.fromisoformat(item["notify_at"]).utcoffset().total_seconds() == 10800
                            for item in payload["reminders"]))
        self.assertNotIn("private-login", json.dumps(payload))
        self.assertNotIn("BEGIN:VCALENDAR", json.dumps(payload))
        serialized = json.dumps(result, ensure_ascii=False)
        for private in ("birthday-uid", "private-calendar-id", "Ночное поручение", "Личные даты", "private-login"):
            self.assertNotIn(private, serialized)

    def test_optional_contact_birthdays_join_calendar_preview_without_exposing_names_in_summary(self):
        os.environ['ICLOUD_CONTACT_BIRTHDAYS_ENABLED'] = 'true'
        birthdays = BirthdayCollection([ContactBirthday('private-book', 'contact-uid', 'Private contact <x>', 10, 20)], 1)
        stages = []
        with patch.object(preview, 'collect_contact_birthdays', return_value=birthdays) as collect:
            result = self.run_preview(progress=stages.append)
        collect.assert_called_once_with()
        self.assertEqual(stages, list(ReminderPreviewStage))
        self.assertTrue(result['contacts_enabled'])
        self.assertEqual(result['birthday_contacts'], 1)
        self.assertEqual(result['contact_addressbooks'], 1)
        self.assertEqual(result['by_category']['birthdays'], 6)
        self.assertEqual(result['reminders_total'], 10)
        html = Path(result['preview_html']).read_text()
        self.assertIn('Private contact &lt;x&gt;', html)
        self.assertIn('Контакты iCloud', html)
        self.assertNotIn('Private contact', json.dumps(result))
        payload = json.loads(Path(result['preview_json']).read_text())
        notices = [item for item in payload['reminders'] if item['source_id'] == 'icloud_contacts']
        self.assertEqual([item['notify_at'] for item in notices], [
            '2026-10-17T09:00:00+03:00', '2026-10-19T09:00:00+03:00', '2026-10-20T09:00:00+03:00'])

    def test_contacts_are_not_read_unless_enabled_and_failure_never_overwrites_preview(self):
        with patch.object(preview, 'collect_contact_birthdays') as collect:
            result = self.run_preview()
        collect.assert_not_called()
        self.assertFalse(result['contacts_enabled'])
        original = Path(result['preview_json']).read_bytes()
        os.environ['ICLOUD_CONTACT_BIRTHDAYS_ENABLED'] = 'true'
        with patch.object(preview, 'collect_contact_birthdays', side_effect=CalendarConnectionError('safe failure')), \
                patch.object(preview, 'collect_all') as calendars:
            with self.assertRaises(CalendarConnectionError):
                create_reminder_preview(date(2026, 10, 15), self.directory)
        calendars.assert_not_called()
        self.assertEqual(Path(result['preview_json']).read_bytes(), original)

    def test_html_escapes_calendar_content_and_has_no_links_scripts_or_remote_assets(self):
        dangerous = '<img src="https://invalid.example/pixel" onerror="alert(1)"> & <script>oops</script>'
        item = resource("private-event", ";VALUE=DATE:20261018", dangerous,
                        source="icloud", calendar='Calendar </p><script>bad</script>',
                        extra="CATEGORIES:Birthday\r\n")
        result = self.run_preview(Collection([item], 1))
        html = Path(result["preview_html"]).read_text()
        self.assertIn("&lt;img", html)
        self.assertIn("&lt;script&gt;oops&lt;/script&gt;", html)
        self.assertIn("Calendar &lt;/p&gt;&lt;script&gt;bad&lt;/script&gt;", html)
        self.assertIn("Это предпросмотр.", html)
        self.assertIn("Уведомления не отправлялись", html)
        for tag, attrs in Elements(html).elements:
            self.assertNotIn(tag, {"script", "img", "iframe", "a", "link", "object", "form"})
            self.assertFalse(any(key.lower().startswith("on") for key in attrs))
            self.assertFalse(any(key in attrs for key in ("src", "href")))

    def test_midnight_notice_shows_both_notification_day_and_event_day(self):
        result = self.run_preview()
        html = Path(result["preview_html"]).read_text()
        self.assertIn('<h2>15.10.2026<span>четверг</span>', html)
        self.assertIn('23:00<span>МСК</span>', html)
        self.assertIn("Событие: 16.10.2026 в 02:00", html)
        self.assertIn("За 3 часа", html)
        self.assertIn("18.10.2026 · весь день", html)

    def test_deferred_hourly_notice_shows_seven_and_honest_reason(self):
        early = resource("early", ";TZID=Europe/Moscow:20261015T040000", "Early synthetic")
        result = self.run_preview(Collection([early], 1), days=1)
        payload = json.loads(Path(result["preview_json"]).read_text())
        reminder, = payload["reminders"]
        self.assertEqual(reminder["notify_at"], "2026-10-15T07:00:00+03:00")
        self.assertEqual(reminder["rule"], "h3")
        html = Path(result["preview_html"]).read_text()
        self.assertIn("07:00<span>МСК</span>", html)
        self.assertIn("Перенесено на 07:00 МСК", html)
        self.assertIn("Событие: 15.10.2026 в 04:00", html)
        self.assertNotIn("За 3 часа", html)

    def test_one_day_window_keeps_future_events_but_only_that_days_notifications(self):
        result = self.run_preview(days=1)
        payload = json.loads(Path(result["preview_json"]).read_text())
        self.assertEqual(result["end_date_exclusive"], "2026-10-16")
        self.assertEqual(result["reminders_total"], 3)
        self.assertEqual({item["notify_at"][:10] for item in payload["reminders"]}, {"2026-10-15"})
        self.assertTrue(any(item["event_start"] == "2026-10-18" for item in payload["reminders"]))

    def test_empty_schedule_still_explains_each_preview_day(self):
        unrelated = resource("unrelated", ";VALUE=DATE:20261018", "Обычное событие", source="icloud")
        result = self.run_preview(Collection([unrelated], 1), days=2)
        self.assertEqual(result["reminders_total"], 0)
        html = Path(result["preview_html"]).read_text()
        self.assertEqual(html.count("На этот день уведомлений нет."), 2)
        self.assertNotIn("Обычное событие", html)

    def test_preview_leaves_email_baseline_delivery_journal_and_actions_untouched(self):
        self.directory.mkdir()
        originals = {
            "calendar-state.json": b'{"unparsed-baseline":true}',
            "deliveries.sqlite3": b"existing-delivery-journal",
            "actions.sqlite3": b"existing-actions-and-cancellation-archive",
            "preview-2026-10-16.json": b"existing-email-preview",
            "email-actions-2026-10-16.html": b"existing-action-email",
        }
        for name, contents in originals.items():
            (self.directory / name).write_bytes(contents)
        with patch("calendar_digest.smtp_delivery.submit_message", side_effect=AssertionError("Must not send")):
            self.run_preview()
        for name, contents in originals.items():
            self.assertEqual((self.directory / name).read_bytes(), contents)
        self.assertEqual(
            {item.name for item in self.directory.iterdir()} - originals.keys(),
            {".calendar-operation.lock", "reminder-preview-2026-10-15.json", "reminder-preview-2026-10-15.html"},
        )

    def test_new_directory_and_files_are_private_and_existing_mount_permissions_stay(self):
        result = self.run_preview()
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        for name in ("preview_json", "preview_html"):
            self.assertEqual(Path(result[name]).stat().st_mode & 0o777, 0o600)
        self.directory.chmod(0o2775)
        self.run_preview()
        self.assertEqual(self.directory.stat().st_mode & 0o7777, 0o2775)

    def test_existing_output_symlink_is_replaced_without_writing_its_target(self):
        self.directory.mkdir()
        untouched = self.directory.parent / "unrelated-file"
        untouched.write_text("do not change")
        untouched.chmod(0o644)
        output = self.directory / "reminder-preview-2026-10-15.json"
        output.symlink_to(untouched)
        self.run_preview()
        self.assertFalse(output.is_symlink())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(untouched.read_text(), "do not change")
        self.assertEqual(untouched.stat().st_mode & 0o777, 0o644)

    def test_collection_failure_preserves_previous_preview_without_partial_outputs(self):
        result = self.run_preview()
        originals = {name: Path(result[name]).read_bytes() for name in ("preview_json", "preview_html")}
        stages = []
        with patch.object(preview, "collect_all", side_effect=CalendarConnectionError("Недоступен источник.")):
            with self.assertRaises(CalendarConnectionError):
                create_reminder_preview(date(2026, 10, 15), self.directory, progress=stages.append)
        self.assertEqual(stages, [ReminderPreviewStage.COLLECT])
        for name, contents in originals.items():
            self.assertEqual(Path(result[name]).read_bytes(), contents)

    def test_plan_failure_does_not_create_preview_files(self):
        with patch.object(preview, "collect_all", return_value=collection()), \
                patch.object(preview, "plan_reminders", side_effect=ValueError("synthetic invalid event")):
            with self.assertRaises(ValueError):
                create_reminder_preview(date(2026, 10, 15), self.directory)
        self.assertEqual(list(self.directory.glob("reminder-preview-*")), [])

    def test_request_validation_happens_before_lock_or_collection(self):
        cases = [
            ("2026-10-15", {}), (datetime(2026, 10, 15), {}),
            (date(2026, 10, 15), {"days": 0}), (date(2026, 10, 15), {"days": 32}),
            (date(2026, 10, 15), {"days": True}), (date(2026, 10, 15), {"days": 1.5}),
            (date(9999, 12, 31), {}),
            (date(2026, 10, 15), {"control_source": "icloud"}),
        ]
        with patch.object(preview, "collect_all") as collect:
            for start, kwargs in cases:
                with self.subTest(start=start, kwargs=kwargs), self.assertRaises(ReminderPreviewError):
                    create_reminder_preview(start, self.directory, **kwargs)
            collect.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_shared_calendar_gate_prevents_collection_even_with_separate_output_directory(self):
        state = Path(self.temporary.name) / "service-state"
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(state)}), \
                calendar_operation_lock(state), patch.object(preview, "collect_all") as collect:
            with self.assertRaises(CalendarBusy):
                create_reminder_preview(date(2026, 10, 15), self.directory)
            collect.assert_not_called()
        self.assertFalse(self.directory.exists())
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(state)}):
            self.run_preview()
        self.assertFalse((self.directory / ".calendar-operation.lock").exists())
        self.assertTrue((state / ".calendar-operation.lock").is_file())

    def test_safe_fixed_progress_finishes_only_after_both_files_exist(self):
        stages = []

        def observer(stage):
            stages.append(stage)
            if stage is ReminderPreviewStage.DONE:
                self.assertTrue((self.directory / "reminder-preview-2026-10-15.json").is_file())
                self.assertTrue((self.directory / "reminder-preview-2026-10-15.html").is_file())

        self.run_preview(progress=observer)
        self.assertEqual(stages, [stage for stage in ReminderPreviewStage if stage is not ReminderPreviewStage.CONTACTS])
        self.assertNotIn("private", " ".join(stage.value for stage in stages).lower())

    def test_observer_failure_is_ignored_without_printing_private_error(self):
        attempted = []

        def observer(stage):
            attempted.append(stage)
            raise RuntimeError("private observer information")

        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.run_preview(progress=observer)
        self.assertEqual(result["reminders_total"], 7)
        self.assertEqual(attempted, [stage for stage in ReminderPreviewStage if stage is not ReminderPreviewStage.CONTACTS])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_storage_failure_has_no_private_path_and_no_success_stage(self):
        stages = []
        with patch.object(preview, "collect_all", return_value=collection()), \
                patch.object(preview, "write_private", side_effect=PermissionError("/private/location secret")):
            with self.assertRaises(ReminderPreviewError) as error:
                create_reminder_preview(date(2026, 10, 15), self.directory, progress=stages.append)
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn(ReminderPreviewStage.DONE, stages)


if __name__ == "__main__":
    unittest.main()
