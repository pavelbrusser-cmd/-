"""Manual push receipt tests with a fake provider; no real notifications."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest import push_delivery
from calendar_digest.push_delivery import PushDeliveryError, push_status, send_push_test
from calendar_digest.push_transport import PushoverSettings, PushError, PushUncertain


NOW = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)
SETTINGS = PushoverSettings("A" * 30, "U" * 30, "private_iphone", "b7" * 32)


class PushDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary) / "state"
        self.database = self.directory / "push-delivery" / "push.sqlite3"
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.settings = self.enterContext(patch.object(
            push_delivery.PushoverSettings, "from_environment", return_value=SETTINGS,
        ))
        self.calls = []
        self.transport = self.enterContext(patch.object(
            push_delivery, "send_notification", side_effect=self.submit,
        ))

    def submit(self, settings, *, title, message, before_send):
        self.assertIs(settings, SETTINGS)
        before_send()
        # A separate connection must observe the checkpoint before submission.
        with sqlite3.connect(self.database) as database:
            states = database.execute("SELECT state FROM deliveries").fetchall()
        self.assertIn(("sending",), states)
        self.calls.append((title, message))
        return {"provider": "pushover", "state": "accepted", "encrypted": True}

    def send(self, now=NOW):
        return send_push_test(self.directory, now=now)

    def state(self):
        return push_status(self.directory)["deliveries"][0]

    def row(self):
        with sqlite3.connect(self.database) as database:
            database.row_factory = sqlite3.Row
            return dict(database.execute("SELECT * FROM deliveries").fetchone())

    def test_durable_checkpoint_and_moscow_date_prevent_duplicate(self):
        expected = {"date": "2026-10-16", "kind": "test", "state": "accepted", "retryable": False}
        self.assertEqual(self.send(), expected)
        self.assertEqual(self.send(), expected)
        self.assertEqual(self.state(), expected)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.settings.call_count, 1)
        self.assertIn("Ежедневные напоминания пока не включены", self.calls[0][1])
        self.assertEqual(self.send(NOW + timedelta(days=1))["date"], "2026-10-17")
        self.assertEqual(len(self.calls), 2)

    def test_private_files_and_no_credentials_in_database_or_results(self):
        result = json.dumps(self.send(), ensure_ascii=False)
        result += json.dumps(push_status(self.directory), ensure_ascii=False)
        data = self.database.read_bytes()
        for secret in (SETTINGS.app_token, SETTINGS.user_key, SETTINGS.device, SETTINGS.encryption_key):
            self.assertNotIn(secret, result)
            self.assertNotIn(secret.encode(), data)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.database.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / ".push-delivery.lock").stat().st_mode), 0o600)

    def test_provider_owned_parent_permissions_are_preserved(self):
        self.directory.mkdir(mode=0o775)
        self.directory.chmod(0o2775)
        parent_inode = self.directory.stat().st_ino
        original = os.fchmod

        def forbid_parent_chmod(descriptor, mode):
            if os.fstat(descriptor).st_ino == parent_inode:
                raise PermissionError("provider-owned mount")
            return original(descriptor, mode)

        with patch.object(push_delivery.os, "fchmod", side_effect=forbid_parent_chmod):
            self.assertEqual(self.send()["state"], "accepted")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o2775)

    def test_lost_provider_reply_is_uncertain_and_not_repeated(self):
        def lost_reply(settings, **kwargs):
            self.submit(settings, **kwargs)
            raise PushUncertain("private provider response")

        self.transport.side_effect = lost_reply
        with self.assertRaises(PushDeliveryError) as failure:
            self.send()
        self.assertNotIn("private provider response", str(failure.exception))
        self.assertFalse(failure.exception.retryable)
        self.assertEqual(self.state()["state"], "uncertain")
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(len(self.calls), 1)

    def test_uncertain_transport_without_callback_is_also_never_repeated(self):
        self.transport.side_effect = PushUncertain("lost reply")
        with self.assertRaises(PushDeliveryError):
            self.send()
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(self.transport.call_count, 1)

    def test_process_interruption_after_checkpoint_is_recovered(self):
        def interrupted(settings, **kwargs):
            kwargs["before_send"]()
            raise SystemExit

        self.transport.side_effect = interrupted
        with self.assertRaises(SystemExit):
            self.send()
        self.assertEqual(self.row()["state"], "sending")
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(self.transport.call_count, 1)

    def test_interrupted_preparation_can_be_retried(self):
        self.transport.side_effect = SystemExit
        with self.assertRaises(SystemExit):
            self.send()
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.state()["state"], "failed")
        self.assertTrue(self.state()["retryable"])
        self.transport.side_effect = self.submit
        self.assertEqual(self.send()["state"], "accepted")

    def test_known_failure_can_be_manually_retried_after_correction(self):
        for after_checkpoint in (False, True):
            with self.subTest(after_checkpoint=after_checkpoint):
                current = NOW + timedelta(days=int(after_checkpoint))

                def rejected(settings, **kwargs):
                    if after_checkpoint:
                        kwargs["before_send"]()
                    raise PushError("private API details", retryable=False)

                self.transport.side_effect = rejected
                with self.assertRaises(PushDeliveryError) as failure:
                    self.send(current)
                self.assertFalse(failure.exception.retryable)
                self.assertNotIn("private API details", str(failure.exception))
                self.assertEqual(self.state()["state"], "failed")
                self.assertFalse(self.state()["retryable"])
                self.transport.side_effect = self.submit
                self.assertEqual(self.send(current)["state"], "accepted")

    def test_transient_rejection_preserves_retryable_marker(self):
        self.transport.side_effect = PushError("connection unavailable", retryable=True)
        with self.assertRaises(PushDeliveryError) as failure:
            self.send()
        self.assertTrue(failure.exception.retryable)
        self.assertTrue(self.state()["retryable"])
        self.transport.side_effect = self.submit
        self.assertEqual(self.send()["state"], "accepted")

    def test_failed_accepted_receipt_preserves_checkpoint_and_blocks_repeat(self):
        original = push_delivery._PushStore.transition

        def broken(store, day, state, **kwargs):
            if state == "accepted":
                raise sqlite3.OperationalError("private disk path")
            return original(store, day, state, **kwargs)

        with patch.object(push_delivery._PushStore, "transition", broken):
            with self.assertRaises(PushDeliveryError) as failure:
                self.send()
        self.assertNotIn("private disk path", str(failure.exception))
        self.assertFalse(failure.exception.retryable)
        self.assertEqual(self.row()["state"], "sending")
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(len(self.calls), 1)

    def test_failed_checkpoint_never_reaches_submission(self):
        original = push_delivery._PushStore.transition

        def broken(store, day, state, **kwargs):
            if state == "sending":
                raise sqlite3.OperationalError("private disk path")
            return original(store, day, state, **kwargs)

        with patch.object(push_delivery._PushStore, "transition", broken):
            with self.assertRaises(PushDeliveryError):
                self.send()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.state()["state"], "failed")
        self.assertEqual(self.send()["state"], "accepted")

    def test_checkpoint_commit_with_lost_acknowledgement_is_uncertain(self):
        original = push_delivery._PushStore.transition

        def broken(store, day, state, **kwargs):
            result = original(store, day, state, **kwargs)
            if state == "sending":
                raise sqlite3.OperationalError("lost checkpoint acknowledgement")
            return result

        with patch.object(push_delivery._PushStore, "transition", broken):
            with self.assertRaises(PushDeliveryError):
                self.send()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.send()["state"], "uncertain")

    def test_unexpected_failure_after_checkpoint_is_uncertain(self):
        def broken(settings, **kwargs):
            self.submit(settings, **kwargs)
            raise RuntimeError("private traceback")

        self.transport.side_effect = broken
        with self.assertRaises(PushDeliveryError) as failure:
            self.send()
        self.assertNotIn("private traceback", str(failure.exception))
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(len(self.calls), 1)

    def test_transport_must_call_checkpoint(self):
        self.transport.side_effect = None
        self.transport.return_value = {"state": "accepted"}
        with self.assertRaises(PushDeliveryError):
            self.send()
        self.assertEqual(self.send()["state"], "uncertain")
        self.assertEqual(self.transport.call_count, 1)

    def test_missing_settings_are_safe_and_do_not_create_pending_receipt(self):
        self.settings.side_effect = PushError("private unexpected config")
        with self.assertRaises(PushDeliveryError) as failure:
            self.send()
        self.assertNotIn("private unexpected config", str(failure.exception))
        self.assertEqual(push_status(self.directory), {"deliveries": []})
        self.transport.assert_not_called()

    def test_timezone_and_configured_directory_are_checked_before_send(self):
        with self.assertRaises(PushDeliveryError):
            self.send(NOW.replace(tzinfo=None))
        self.assertFalse(self.directory.exists())
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(self.directory / "other")}):
            for operation in (self.send, self.state):
                with self.assertRaises(PushDeliveryError):
                    operation()
        self.assertFalse(self.directory.exists())
        self.transport.assert_not_called()

    def test_symlink_and_hardlink_lock_are_rejected_without_touching_target(self):
        self.directory.mkdir()
        target = self.directory / "target"
        target.write_text("unchanged", encoding="utf-8")
        target.chmod(0o640)
        lock = self.directory / ".push-delivery.lock"
        for form in ("symlink", "hardlink"):
            with self.subTest(form=form):
                if form == "symlink":
                    lock.symlink_to(target)
                else:
                    os.link(target, lock)
                with self.assertRaises(PushDeliveryError):
                    self.send()
                self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
                lock.unlink()
        self.transport.assert_not_called()

    def test_symlink_parent_private_directory_database_and_sidecar_are_rejected(self):
        self.directory.mkdir()
        external = self.directory.parent / "external"
        external.mkdir()
        original = self.directory
        link = self.directory.parent / "state-link"
        link.symlink_to(self.directory, target_is_directory=True)
        self.directory = link
        with self.assertRaises(PushDeliveryError):
            self.send()
        self.directory = original
        private = self.directory / "push-delivery"
        private.symlink_to(external, target_is_directory=True)
        with self.assertRaises(PushDeliveryError):
            self.send()
        private.unlink()
        private.mkdir()
        target = external / "target"
        target.write_text("unchanged", encoding="utf-8")
        for name in ("push.sqlite3", "push.sqlite3-journal"):
            with self.subTest(name=name):
                path = private / name
                path.symlink_to(target)
                with self.assertRaises(PushDeliveryError):
                    self.send()
                path.unlink()
                self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
        self.transport.assert_not_called()

    def test_process_lock_blocks_send_and_recovery(self):
        push_status(self.directory)
        code = (
            "import fcntl, pathlib, sys; "
            "lock = open(pathlib.Path(sys.argv[1]) / '.push-delivery.lock', 'r+'); "
            "fcntl.flock(lock, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read()"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(self.directory)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            for operation in (self.send, self.state):
                with self.assertRaises(PushDeliveryError) as failure:
                    operation()
                self.assertTrue(failure.exception.retryable)
            self.transport.assert_not_called()
        finally:
            process.communicate(timeout=5)
        self.assertEqual(self.send()["state"], "accepted")

    def test_module_import_does_not_load_calendar_stack(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import calendar_digest.push_delivery; "
             "assert not any(x in sys.modules for x in "
             "('caldav', 'icalendar', 'calendar_digest.preview', 'calendar_digest.selection'))"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
