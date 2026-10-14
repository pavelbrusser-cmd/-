"""Durability tests with synthetic calendars and a fake SMTP boundary only."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from calendar_digest import delivery
from calendar_digest.delivery import (
    DeliveryError, DeliveryStore, delivery_status, prepare_delivery, submit_delivery,
)
from calendar_digest.selection import DigestEvent
from calendar_digest.smtp_delivery import MailSettings, SMTPDeliveryError, SMTPDeliveryUncertain


DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone.utc)
EVENT = DigestEvent(
    source_id="mailru_2", calendar_id="synthetic", calendar_name="Работа", uid="private-test-uid",
    recurrence_id=None, title="Частное тестовое событие", start="2026-10-15T17:00:00+03:00",
    end="2026-10-15T17:30:00+03:00", all_day=False, event_status="TENTATIVE",
    participation="NEEDS-ACTION", recurring=False, multi_day=False,
)
SETTINGS = MailSettings("sender@example.test", "private-password", "recipient@example.test")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.settings = SETTINGS
        self.preview_calls = 0
        self.smtp_calls = []
        self.payload = {"date": DAY.isoformat(), "timezone": "Europe/Moscow", "first_snapshot": False,
                        "events": [asdict(EVENT)]}
        self.start_patch(patch.dict(os.environ, {}, clear=True))
        self.start_patch(patch.object(delivery.MailSettings, "from_environment", side_effect=lambda: self.settings))
        self.start_patch(patch("calendar_digest.preview.create_preview", side_effect=self.preview))
        self.compose = self.start_patch(patch("calendar_digest.action_links.compose_action_email",
                                              side_effect=self.compose_message))
        self.transport = self.start_patch(patch.object(delivery, "submit_message", side_effect=self.submit))
        self.start_patch(patch.object(delivery, "_utc_now", return_value=NOW))

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def preview(self, day, directory, **kwargs):
        self.preview_calls += 1
        path = directory / f"preview-{day.isoformat()}.json"
        temporary = directory / ".complete-preview"
        temporary.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        return {"events": len(self.payload["events"])}

    def compose_message(self, day, events, **kwargs):
        self.assertEqual(day, DAY)
        self.assertEqual(kwargs["state_dir"], self.directory)
        self.assertEqual(events, [EVENT])
        return ('<html>Частное событие <a href="https://example.test/a/private-token">Действие</a></html>',
                {"date": day.isoformat(), "events": len(events), "action_links": 1})

    def submit(self, settings, message, *, before_data):
        before_data()
        # The exact checkpoint must be committed and observable before DATA.
        with sqlite3.connect(self.directory / "deliveries.sqlite3") as database:
            count = database.execute("SELECT count(*) FROM deliveries WHERE state='submitting'").fetchone()[0]
        self.assertEqual(count, 1)
        self.smtp_calls.append(message)

    def row(self, kind="test"):
        with sqlite3.connect(self.directory / "deliveries.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return dict(db.execute("SELECT * FROM deliveries WHERE kind=?", (kind,)).fetchone())

    def edit_row(self, **values):
        with sqlite3.connect(self.directory / "deliveries.sqlite3") as db:
            db.execute("UPDATE deliveries SET " + ",".join(key + "=?" for key in values), tuple(values.values()))

    def prepare(self, kind="test"):
        return prepare_delivery(DAY, self.directory, kind=kind)

    def send(self, kind="test"):
        return submit_delivery(DAY, self.directory, kind=kind)

    def state(self):
        return delivery_status(self.directory, day=DAY)["deliveries"][0]

    def test_complete_private_message_persisted_before_smtp_and_no_duplicate(self):
        prepared = self.prepare()
        self.assertEqual(prepared["state"], "ready")
        first = self.row()
        path = self.directory / "deliveries" / first["eml_name"]
        message = path.read_bytes()
        self.assertEqual(hashlib.sha256(message).hexdigest(), first["sha256"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "deliveries.sqlite3").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.prepare(), prepared)
        result = self.send()
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(self.send(), result)
        self.assertEqual(self.prepare(), result)
        self.assertEqual(self.smtp_calls, [message])
        self.assertEqual(self.preview_calls, 1)
        parsed = BytesParser(policy=policy.default).parsebytes(message)
        self.assertEqual(parsed["Message-ID"], first["message_id"])
        self.assertIn("17:00–17:30", parsed.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("Время московское", parsed.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("private-token", parsed.get_body(preferencelist=("html",)).get_content())

    def test_results_and_database_never_contain_password_or_private_event(self):
        self.prepare()
        result = json.dumps(delivery_status(self.directory), ensure_ascii=False)
        database = (self.directory / "deliveries.sqlite3").read_bytes()
        for private in (SETTINGS.username, SETTINGS.recipient, SETTINGS.password, EVENT.title,
                        EVENT.uid, "private-token"):
            self.assertNotIn(private, result)
        for private in (SETTINGS.password, EVENT.title, EVENT.uid, "private-token"):
            self.assertNotIn(private.encode(), database)

    def test_preparation_failure_cannot_send_previous_message(self):
        self.prepare()
        self.edit_row(state="failed", retryable=1)
        with patch("calendar_digest.preview.create_preview", side_effect=RuntimeError("private-password")):
            with self.assertRaises(DeliveryError) as failure:
                self.prepare()
        self.assertNotIn("private-password", str(failure.exception))
        self.assertEqual(self.state()["state"], "failed")
        with self.assertRaises(DeliveryError):
            self.send()
        self.transport.assert_not_called()
        self.assertIsNone(self.row()["eml_name"])

    def test_crashed_preparation_can_be_prepared_again(self):
        with patch("calendar_digest.preview.create_preview", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                self.prepare()
        self.assertEqual(self.state()["state"], "preparing")
        self.assertTrue(self.state()["retryable"])
        identity = self.row()["message_id"]
        self.assertEqual(self.prepare()["state"], "ready")
        self.assertEqual(self.row()["message_id"], identity)

    def test_crashed_data_is_uncertain_after_restart_and_never_replayed(self):
        self.prepare()
        def crash(settings, message, *, before_data):
            before_data()
            raise SystemExit
        self.transport.side_effect = crash
        with self.assertRaises(SystemExit):
            self.send()
        self.assertEqual(self.row()["state"], "submitting")
        self.assertEqual(self.state()["state"], "uncertain")
        self.assertFalse(self.state()["retryable"])
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(self.prepare()["state"], "uncertain")
        self.assertEqual(self.transport.call_count, 1)

    def test_interrupted_submitting_recovered_by_submit_without_status(self):
        self.prepare()
        self.edit_row(state="submitting")
        self.assertEqual(self.send()["state"], "uncertain")
        self.transport.assert_not_called()

    def test_lost_data_reply_is_uncertain_even_if_transport_fails_before_callback(self):
        self.prepare()
        self.transport.side_effect = SMTPDeliveryUncertain("private reply")
        with self.assertRaises(DeliveryError):
            self.send()
        self.assertEqual(self.state()["state"], "uncertain")
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(self.transport.call_count, 1)

    def test_authentication_failure_is_not_automatically_retryable_but_admin_can_reprepare(self):
        self.prepare()
        identity = self.row()["message_id"]
        self.transport.side_effect = SMTPDeliveryError("password rejected", retryable=False)
        with self.assertRaises(DeliveryError) as failure:
            self.send()
        self.assertFalse(failure.exception.retryable)
        self.assertEqual(self.state()["state"], "failed")
        self.assertFalse(self.state()["retryable"])
        self.assertEqual(self.prepare()["state"], "ready")
        self.assertEqual(self.row()["message_id"], identity)
        self.transport.side_effect = self.submit
        self.assertEqual(self.send()["state"], "accepted")

    def test_explicit_transient_data_rejection_allows_fresh_retry(self):
        self.prepare()
        original = self.row()
        def reject(settings, message, *, before_data):
            before_data()
            raise SMTPDeliveryError("451", retryable=True)
        self.transport.side_effect = reject
        with self.assertRaises(DeliveryError) as failure:
            self.send()
        self.assertTrue(failure.exception.retryable)
        self.assertTrue(self.state()["retryable"])
        self.assertEqual(self.prepare()["state"], "ready")
        self.assertNotEqual(self.row()["eml_name"], original["eml_name"])
        self.assertEqual(self.row()["message_id"], original["message_id"])
        self.transport.side_effect = self.submit
        self.assertEqual(self.send()["state"], "accepted")

    def test_failed_accepted_receipt_never_retries(self):
        self.prepare()
        original = DeliveryStore.transition
        def broken_receipt(store, day, kind, state, **kwargs):
            if state == "accepted":
                raise sqlite3.OperationalError("disk failure")
            return original(store, day, kind, state, **kwargs)
        with patch.object(DeliveryStore, "transition", broken_receipt):
            with self.assertRaises(DeliveryError):
                self.send()
        self.assertEqual(len(self.smtp_calls), 1)
        self.assertEqual(self.row()["state"], "submitting")
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(len(self.smtp_calls), 1)

    def test_failed_pre_data_checkpoint_sends_nothing(self):
        self.prepare()
        original = DeliveryStore.transition
        def broken_checkpoint(store, day, kind, state, **kwargs):
            if state == "submitting":
                raise sqlite3.OperationalError("disk failure")
            return original(store, day, kind, state, **kwargs)
        with patch.object(DeliveryStore, "transition", broken_checkpoint):
            with self.assertRaises(DeliveryError):
                self.send()
        self.assertEqual(self.smtp_calls, [])
        self.assertEqual(self.state()["state"], "failed")
        self.assertTrue(self.state()["retryable"])

    def test_checkpoint_committed_then_error_is_uncertain(self):
        self.prepare()
        original = DeliveryStore.transition
        def broken_checkpoint(store, day, kind, state, **kwargs):
            result = original(store, day, kind, state, **kwargs)
            if state == "submitting":
                raise sqlite3.OperationalError("lost local acknowledgement")
            return result
        with patch.object(DeliveryStore, "transition", broken_checkpoint):
            with self.assertRaises(DeliveryError):
                self.send()
        self.assertEqual(self.smtp_calls, [])
        self.assertEqual(self.state()["state"], "uncertain")

    def test_file_missing_modified_or_redirected_never_reaches_smtp(self):
        self.prepare()
        for alteration in ("missing", "changed", "symlink", "traversal"):
            with self.subTest(alteration=alteration):
                row = self.row()
                path = self.directory / "deliveries" / row["eml_name"]
                if alteration == "missing":
                    path.unlink()
                elif alteration == "changed":
                    path.write_bytes(b"another email")
                elif alteration == "symlink":
                    path.unlink()
                    path.symlink_to(self.directory / "preview-2026-10-15.json")
                else:
                    self.edit_row(eml_name="../preview-2026-10-15.json")
                with self.assertRaises(DeliveryError):
                    self.send()
                self.assertFalse(self.state()["retryable"])
                self.transport.assert_not_called()
                self.prepare()

    def test_changed_envelope_rejected_without_smtp(self):
        self.prepare()
        self.settings = replace(SETTINGS, recipient="different@example.test")
        with self.assertRaises(DeliveryError):
            self.send()
        self.transport.assert_not_called()
        self.settings = SETTINGS
        self.prepare()
        self.settings = replace(SETTINGS, username="different@example.test")
        with self.assertRaises(DeliveryError):
            self.send()
        self.transport.assert_not_called()

    def test_mime_destination_checked_even_when_hash_matches(self):
        self.prepare()
        row = self.row()
        path = self.directory / "deliveries" / row["eml_name"]
        content = path.read_bytes().replace(b"recipient@example.test", b"other@example.test")
        path.write_bytes(content)
        self.edit_row(sha256=hashlib.sha256(content).hexdigest())
        with self.assertRaises(DeliveryError):
            self.send()
        self.transport.assert_not_called()

    def test_stale_ready_requires_new_full_collection(self):
        self.prepare()
        self.edit_row(prepared_at=time.time() - delivery.MAX_READY_AGE - 1)
        with self.assertRaises(DeliveryError):
            self.send()
        self.transport.assert_not_called()
        self.assertTrue(self.state()["retryable"])
        self.prepare()
        self.assertEqual(self.preview_calls, 2)
        self.assertEqual(self.send()["state"], "accepted")

    def test_authentication_delay_cannot_send_stale_message(self):
        self.prepare()
        prepared = self.row()["prepared_at"]
        def late(settings, message, *, before_data):
            with patch.object(delivery.time, "time", return_value=prepared + delivery.MAX_READY_AGE + 1):
                before_data()
            self.fail("DATA must not start")
        self.transport.side_effect = late
        with self.assertRaises(DeliveryError):
            self.send()
        self.assertEqual(self.state()["state"], "failed")
        self.assertTrue(self.state()["retryable"])

    def test_daily_moscow_window_is_enforced_before_connect_and_again_before_data(self):
        self.prepare(kind="daily")
        for now in (NOW - timedelta(minutes=1), NOW + timedelta(minutes=30), NOW + timedelta(days=1)):
            with self.subTest(now=now), patch.object(delivery, "_utc_now", return_value=now):
                with self.assertRaises(DeliveryError):
                    self.send(kind="daily")
        self.transport.assert_not_called()
        def late(settings, message, *, before_data):
            with patch.object(delivery, "_utc_now", return_value=NOW + timedelta(minutes=30)):
                before_data()
            self.fail("DATA must not start")
        self.transport.side_effect = late
        with self.assertRaises(DeliveryError):
            self.send(kind="daily")
        self.assertFalse(delivery_status(self.directory)["deliveries"][0]["retryable"])

    def test_daily_and_test_are_separate_idempotent_jobs(self):
        self.prepare(kind="test")
        self.prepare(kind="daily")
        self.assertEqual(self.send(kind="test")["state"], "accepted")
        self.assertEqual(self.send(kind="daily")["state"], "accepted")
        self.assertEqual(len(self.smtp_calls), 2)
        self.assertEqual(len(delivery_status(self.directory)["deliveries"]), 2)

    def test_state_directory_mismatch_rejected_before_collection_or_smtp(self):
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(self.directory / "other")}):
            with self.assertRaises(DeliveryError):
                self.prepare()
            with self.assertRaises(DeliveryError):
                delivery_status(self.directory)
        self.assertEqual(self.preview_calls, 0)
        self.transport.assert_not_called()

    def test_shared_lock_blocks_other_preparations_submissions_and_recovery(self):
        self.prepare()
        with (self.directory / ".delivery.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for operation in (self.prepare, self.send, self.state):
                with self.assertRaises(DeliveryError) as failure:
                    operation()
                self.assertTrue(failure.exception.retryable)
        self.assertEqual(self.preview_calls, 1)
        self.transport.assert_not_called()

    def test_timezone_mismatch_or_incomplete_source_result_cannot_send(self):
        self.payload["timezone"] = "UTC"
        with self.assertRaises(DeliveryError):
            self.prepare()
        self.compose.assert_not_called()
        self.transport.assert_not_called()

    def test_composition_uses_captured_events_when_other_preview_is_replaced(self):
        original = self.compose_message
        def compose(day, events, **kwargs):
            path = self.directory / f"preview-{day.isoformat()}.json"
            path.write_text('{"events":[]}', encoding="utf-8")
            return original(day, events, **kwargs)
        self.compose.side_effect = compose
        self.assertEqual(self.prepare()["events"], 1)
        self.assertEqual(self.send()["state"], "accepted")

    def test_importing_delivery_does_not_import_calendar_stack(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import calendar_digest.delivery; "
             "assert not any(x in sys.modules for x in ('caldav','icalendar','calendar_digest.preview','calendar_digest.selection'))"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
