from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from calendar_digest.action_store import ActionStore, StoreError


TARGET = {
    "source_id": "mailru_1", "calendar_id": "private-calendar",
    "uid": "private-event-identity", "expected_start": "2026-10-14T12:00:00+03:00",
    "expected_end": "2026-10-14T13:00:00+03:00", "recurrence_id": None,
    "expected_event_status": "CONFIRMED", "expected_participation": "ACCEPTED",
}


class ActionStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "actions.sqlite3"
        self.key = secrets.token_urlsafe(48)
        self.store = ActionStore(self.path, self.key)

    def test_private_opaque_tokens_and_files(self):
        token = self.store.issue(TARGET, "cancel")
        self.assertEqual(len(token), 87)
        self.assertNotIn(self.key, token)
        for value in TARGET.values():
            if value:
                self.assertNotIn(value, token)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(self.key.encode(), self.path.read_bytes())

    def test_read_never_consumes_or_modifies_link(self):
        token = self.store.issue(TARGET, "reschedule")
        before = self.path.read_bytes()
        first = self.store.read(token)
        second = self.store.read(token)
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "ready")
        self.assertEqual(first["target"], TARGET)
        self.assertIsNone(first["result"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(first["expires_at"] - first["created_at"], 48 * 3600)

    def test_tampering_wrong_key_missing_and_malformed_are_safe(self):
        token = self.store.issue(TARGET, "cancel")
        replacement = "A" if token[-1] != "A" else "B"
        for value in (token[:-1] + replacement, "", "private-value", token + "/", None):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(StoreError) as error:
                    self.store.read(value)
                self.assertNotIn("private", str(error.exception))
                self.assertNotIn(str(self.path), str(error.exception))
        other = ActionStore(self.path, secrets.token_urlsafe(48))
        with self.assertRaises(StoreError):
            other.claim(token)
        with sqlite3.connect(self.path) as connection:
            connection.execute("DELETE FROM actions")
        with self.assertRaises(StoreError):
            self.store.read(token)

    def test_expiry_prevents_read_or_claim_without_consuming(self):
        with patch("calendar_digest.action_store.time.time", return_value=1000):
            token = self.store.issue(TARGET, "confirm", ttl_seconds=30)
        with patch("calendar_digest.action_store.time.time", return_value=1030):
            with self.assertRaises(StoreError):
                self.store.read(token)
            with self.assertRaises(StoreError):
                self.store.claim(token)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT state FROM actions").fetchone()[0], "ready")

    def test_concurrent_claim_has_only_one_winner(self):
        token = self.store.issue(TARGET, "cancel")
        barrier = threading.Barrier(8)

        def claim(_):
            store = ActionStore(self.path, self.key)
            barrier.wait(timeout=5)
            return store.claim(token)

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = list(executor.map(claim, range(8)))
        self.assertEqual(outcomes.count(True), 1)
        self.assertEqual(self.store.read(token)["state"], "running")

    def test_completion_is_durable_idempotent_and_cannot_be_reused(self):
        token = self.store.issue(TARGET, "confirm")
        with self.assertRaises(StoreError):
            self.store.complete(token, {"message": "not started"})
        self.assertTrue(self.store.claim(token))
        self.store.complete(token, {"message": "accepted"})
        self.store.complete(token, {"message": "must not replace original"})
        reopened = ActionStore(self.path, self.key)
        self.assertEqual(reopened.read(token)["result"], {"message": "accepted"})
        self.assertEqual(reopened.read(token)["state"], "done")
        self.assertFalse(reopened.claim(token))
        with self.assertRaises(StoreError):
            reopened.fail(token, {"message": "later failure"})

    def test_failed_and_uncertain_operations_are_terminal(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                token = self.store.issue(TARGET, "cancel")
                self.store.claim(token)
                self.store.fail(token, {"code": "provider_error"}, uncertain=uncertain)
                self.assertEqual(self.store.read(token)["state"], "uncertain" if uncertain else "failed")
                self.assertFalse(self.store.claim(token))
                with self.assertRaises(StoreError):
                    self.store.complete(token, {"message": "cannot retry"})

    def test_running_operation_can_record_outcome_after_link_expiry(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain):
                with patch("calendar_digest.action_store.time.time", return_value=1000):
                    token = self.store.issue(TARGET, "cancel", ttl_seconds=1)
                    self.store.claim(token)
                with patch("calendar_digest.action_store.time.time", return_value=1002):
                    if uncertain:
                        self.store.fail(token, {"code": "timeout"}, uncertain=True)
                    else:
                        self.store.complete(token, {"message": "completed"})
                    with self.assertRaises(StoreError):
                        self.store.read(token)
                with patch("calendar_digest.action_store.time.time", return_value=1000):
                    self.assertEqual(self.store.read(token)["state"], "uncertain" if uncertain else "done")

    def test_invalid_target_credentials_raw_ics_and_results_are_rejected(self):
        for target in (
            {**TARGET, "app_password": "never-persist-this"},
            {**TARGET, "uid": "BEGIN:VCALENDAR\nprivate\nEND:VCALENDAR"},
            {**TARGET, "expected_end": None},
            {"source_id": "mailru_1"},
        ):
            with self.assertRaises(StoreError):
                self.store.issue(target, "cancel")
        token = self.store.issue(TARGET, "cancel")
        self.store.claim(token)
        with self.assertRaises(StoreError):
            self.store.complete(token, {"nested": {"password": "never-persist-this"}})
        with self.assertRaises(StoreError):
            self.store.complete(token, {"raw": "BEGIN:VEVENT\nprivate\nEND:VEVENT"})
        self.assertNotIn(b"never-persist-this", self.path.read_bytes())
        self.assertEqual(self.store.read(token)["state"], "running")

    def test_invalid_configuration_and_symlink_fail_safely(self):
        with self.assertRaises(StoreError):
            ActionStore(self.path, "short")
        link = self.path.parent / "link.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(StoreError):
            ActionStore(link, self.key)
        for ttl in (0, -1, True, 2.5):
            with self.assertRaises(StoreError):
                self.store.issue(TARGET, "confirm", ttl_seconds=ttl)


if __name__ == "__main__":
    unittest.main()
