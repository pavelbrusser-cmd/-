"""Synthetic-only durable email-queue tests; no accounts or SMTP connections."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from calendar_digest import control_email_queue as queue
from calendar_digest.control_email_queue import ControlEmailQueueError, control_email_store


NOW = datetime(2026, 10, 19, 10, tzinfo=timezone.utc)
RESOURCE = {"source_id": "mailru_2", "calendar_id": "private-calendar",
            "calendar_name": "Контроль поручений", "href": "/private.ics", "etag": '"v1"',
            "data": "BEGIN:VCALENDAR\nprivate control events\nEND:VCALENDAR"}


def reminder(*, at=NOW, uid="private-uid", recipient="participant@example.com", rule="h1", **changes):
    item = {"source_id": "mailru_2", "calendar_id": "private-calendar", "calendar_name": "Контроль поручений",
            "account_email": "gpb.control@mail.ru", "uid": uid, "recurrence_id": None,
            "recipient": recipient, "title": "Личное поручение", "event_start": (at + timedelta(hours=1)).isoformat(),
            "event_end": (at + timedelta(hours=2)).isoformat(), "all_day": False, "location": "Личное место",
            "rule": rule, "notify_at": at.isoformat(), "category": "control_email", "recurring": False}
    item.update(changes)
    identity = [item[key] for key in ("source_id", "calendar_id", "uid", "recurrence_id", "rule", "recipient")]
    item["stable_id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return item


class ControlEmailQueueTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary) / "state"
        self.database = self.directory / "control-email" / "schedule.sqlite3"

    def store(self, now=NOW):
        return control_email_store(self.directory, now=now)

    def plan(self, store, entries=None, *, now=NOW, cache=(RESOURCE,), observed_at=None, **options):
        store.commit_plan(cache, [reminder()] if entries is None else entries,
                          now=now, horizon=now + timedelta(days=7),
                          observed_at=now if observed_at is None else observed_at,
                          calendar_count=1, sync_summary={"changed": 1}, **options)

    def raw(self):
        with sqlite3.connect(self.database) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute("SELECT * FROM reminders ORDER BY stable_id")]

    def test_preview_cache_and_plan_persist_without_automatic_activation(self):
        with self.store() as store:
            self.plan(store)
            self.assertEqual(store.load_cache(), [RESOURCE])
            self.assertEqual(store.due(NOW), [])
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW))
            self.assertIsNone(store.summary(NOW)["activated_at"])
        with self.store() as store:
            self.assertEqual(store.summary(NOW)["counts"]["pending"], 1)
            self.assertEqual(store.load_cache(), [RESOURCE])

    def test_first_activation_excludes_backlog_and_is_idempotent(self):
        old = reminder(at=NOW - timedelta(minutes=1), uid="old")
        current = reminder()
        with self.store() as store:
            self.plan(store, [old, current])
            store.activate(NOW)
            store.activate(NOW + timedelta(seconds=1))
            self.assertEqual(store.due(NOW), [current])
            self.assertEqual(store.summary(NOW)["counts"]["expired"], 1)
            self.assertEqual(store.summary(NOW)["activated_at"], NOW.isoformat())
            self.plan(store, [old, current])
            self.assertEqual(store.due(NOW), [current])

    def test_claim_checkpoint_is_durable_and_accepted_receipt_survives_move_removal(self):
        item = reminder()
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            self.assertTrue(store.claim(item["stable_id"], now=NOW))
            self.assertEqual(self.raw()[0]["state"], "sending")
            self.assertFalse(store.claim(item["stable_id"], now=NOW))
            store.complete(item["stable_id"], "accepted", now=NOW)
            accepted = self.raw()
            self.plan(store, [])
            self.plan(store, [reminder(at=NOW + timedelta(hours=1), title="Updated event")])
            self.assertEqual(self.raw(), accepted)
            self.assertEqual(store.due(NOW + timedelta(hours=1)), [])
        with self.store() as store:
            self.assertEqual(store.summary(NOW)["counts"]["accepted"], 1)

    def test_crash_after_data_checkpoint_becomes_uncertain_and_never_retries(self):
        item = reminder()
        with self.assertRaises(SystemExit):
            with self.store() as store:
                store.activate(NOW)
                self.plan(store)
                self.assertTrue(store.claim(item["stable_id"], now=NOW))
                raise SystemExit()
        with self.store() as store:
            self.assertEqual(store.summary(NOW)["counts"]["uncertain"], 1)
            self.plan(store, [reminder(at=NOW + timedelta(minutes=1))])
            self.assertEqual(store.due(NOW + timedelta(minutes=1)), [])
            with self.assertRaises(ControlEmailQueueError):
                store.complete(item["stable_id"], "failed", now=NOW, retryable=True)

    def test_unknown_transport_result_without_callback_is_also_consumed(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            store.complete(reminder()["stable_id"], "uncertain", now=NOW)
            self.plan(store)
            self.assertEqual(store.due(NOW), [])
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW))

    def test_pending_body_and_time_update_but_removed_event_is_cancelled(self):
        first = reminder(at=NOW + timedelta(hours=1))
        moved = reminder(at=NOW + timedelta(hours=2), title="Moved event", location="New place")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [first])
            self.plan(store, [moved])
            self.assertEqual(store.due(NOW + timedelta(hours=1)), [])
            self.assertEqual(store.due(NOW + timedelta(hours=2)), [moved])
            self.plan(store, [])
            self.assertEqual(store.due(NOW + timedelta(hours=2)), [])
            self.assertEqual(store.summary(NOW)["counts"]["cancelled"], 1)
            self.plan(store, [moved])
            self.assertEqual(store.due(NOW + timedelta(hours=2)), [moved])

    def test_recipient_removal_and_change_cancel_only_old_persons_pending_mail(self):
        old = reminder(recipient="former@example.com")
        new = reminder(recipient="replacement@example.com")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [old])
            self.plan(store, [new])
            self.assertEqual(store.due(NOW), [new])
            self.assertFalse(store.claim(old["stable_id"], now=NOW))
            self.assertEqual(store.summary(NOW)["counts"]["cancelled"], 1)
            self.assertTrue(store.claim(new["stable_id"], now=NOW))
            store.complete(new["stable_id"], "accepted", now=NOW)
            self.plan(store, [])
            self.plan(store, [new])
            self.assertEqual(store.due(NOW), [])

    def test_individual_recipients_have_independent_payloads_and_receipts(self):
        first = reminder(recipient="first@example.com")
        second = reminder(recipient="second@example.com")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [first, second])
            self.assertEqual(len(store.due(NOW)), 2)
            self.assertTrue(store.claim(first["stable_id"], now=NOW))
            store.complete(first["stable_id"], "accepted", now=NOW)
            self.assertEqual(store.due(NOW), [second])
            for item in (first, second):
                payload = next(row["payload"] for row in self.raw() if row["stable_id"] == item["stable_id"])
                other = second if item is first else first
                self.assertNotIn(other["recipient"], payload)

    def test_identical_transmission_times_coalesce_rules_using_planner_priority(self):
        entries = [reminder(rule=rule) for rule in ("m1", "d3", "d1", "d0", "h1")]
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, entries)
            self.assertEqual(store.due(NOW), [entries[-1]])
            self.assertEqual(store.summary(NOW)["counts"]["pending"], 1)
            # An equivalent offset representation is the same transmission slot.
            other = reminder(rule="d1", notify_at=NOW.astimezone(timezone(timedelta(hours=3))).isoformat())
            self.plan(store, [entries[-1], other])
            self.assertEqual(store.due(NOW), [entries[-1]])

    def test_different_rules_at_distinct_times_and_occurrences_are_not_coalesced(self):
        entries = [reminder(rule="d3"), reminder(rule="d1", at=NOW + timedelta(days=2)),
                   reminder(recurrence_id="2026-10-20T10:00:00+03:00", recurring=True)]
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, entries)
            self.assertEqual(store.summary(NOW)["counts"]["pending"], 3)
            self.assertEqual(len(store.due(NOW)), 2)

    def test_accepted_or_uncertain_slot_blocks_new_rule_after_configuration_change(self):
        for state in ("accepted", "uncertain"):
            with self.subTest(state=state):
                old = reminder(uid=state, rule="d0")
                new = reminder(uid=state, rule="h1")
                with self.store() as store:
                    store.activate(NOW)
                    self.plan(store, [old])
                    self.assertTrue(store.claim(old["stable_id"], now=NOW))
                    store.complete(old["stable_id"], state, now=NOW)
                    self.plan(store, [new])
                    self.assertEqual(store.due(NOW), [])
                    self.assertFalse(store.claim(new["stable_id"], now=NOW))
                    self.assertIsNone(store.summary(NOW)["next_due_at"])

    def test_claim_guards_same_slot_even_if_a_caller_bypasses_plan_coalescing(self):
        old, new = reminder(rule="d0"), reminder(rule="h1")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [old])
            # Synthetic corruption simulates an earlier writer that missed
            # coalescing; durable claim still permits only one SMTP DATA.
            stable_id, slot_id, due, payload = queue._reminder_row(new)
            with store.connection:
                store.connection.execute("INSERT INTO reminders VALUES (?,?,?,?,?,?,?,?,?)",
                                         (stable_id, slot_id, due, payload, "pending", 1, None,
                                          NOW.timestamp(), NOW.timestamp()))
            self.assertEqual(len(store.due(NOW)), 2)
            self.assertTrue(store.claim(old["stable_id"], now=NOW))
            self.assertEqual(store.due(NOW), [])
            self.assertFalse(store.claim(new["stable_id"], now=NOW))
        with self.store() as store:
            self.assertEqual(store.due(NOW), [])
            self.assertFalse(store.claim(new["stable_id"], now=NOW))

    def test_permanent_failure_is_not_revived_by_title_move_or_removal(self):
        item = reminder()
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            store.complete(item["stable_id"], "failed", now=NOW)
            stopped = self.raw()
            self.plan(store, [reminder(title="Corrected title")])
            self.plan(store, [])
            self.plan(store, [reminder(at=NOW + timedelta(hours=1))])
            self.assertEqual(self.raw(), stopped)
            self.assertEqual(store.due(NOW + timedelta(hours=1)), [])

    def test_retryable_failure_keeps_backoff_when_body_changes(self):
        item = reminder()
        edited = reminder(title="Corrected title")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            self.assertTrue(store.claim(item["stable_id"], now=NOW))
            store.complete(item["stable_id"], "failed", now=NOW, retryable=True)
            self.plan(store, [edited])
            self.assertEqual(store.due(NOW + timedelta(seconds=59)), [])
            self.assertEqual(store.due(NOW + timedelta(seconds=60)), [edited])
            self.assertTrue(store.claim(item["stable_id"], now=NOW + timedelta(seconds=60)))

    def test_cancelled_overdue_work_is_not_revived_by_replan(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            self.plan(store, [])
            self.plan(store, now=NOW + timedelta(seconds=1))
            self.assertEqual(store.due(NOW + timedelta(seconds=1)), [])
            self.assertEqual(store.summary(NOW)["counts"]["cancelled"], 1)

    def test_observation_age_not_sync_completion_controls_delivery(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, observed_at=NOW - timedelta(seconds=121))
            summary = store.summary(NOW)
            self.assertTrue(summary["sync_recent"])
            self.assertFalse(summary["sync_fresh_for_delivery"])
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW))
            store.mark_sync(NOW, 1, {}, observed_at=NOW)
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW + timedelta(seconds=121)))
            self.assertTrue(store.claim(reminder()["stable_id"], now=NOW + timedelta(seconds=120)))

    def test_exact_grace_boundary_expires_after_fifteen_minutes(self):
        first, second = reminder(uid="first"), reminder(uid="second")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [first, second])
            boundary = NOW + timedelta(minutes=15)
            store.mark_sync(boundary, 1, {})
            self.assertTrue(store.claim(first["stable_id"], now=boundary))
            later = boundary + timedelta(microseconds=1)
            self.assertFalse(store.claim(second["stable_id"], now=later))
            self.assertEqual(store.due(later), [])
            self.assertEqual(store.summary(later)["counts"]["expired"], 1)

    def test_no_night_sends_or_retries_when_clock_crosses_midnight(self):
        evening = NOW.replace(hour=20, minute=59)
        midnight = evening + timedelta(minutes=1)
        first, second = reminder(at=evening, uid="first"), reminder(at=evening, uid="second")
        with self.store(evening) as store:
            store.activate(evening)
            self.plan(store, [first, second], now=evening)
            self.assertEqual(len(store.due(evening)), 2)
            self.assertTrue(store.claim(first["stable_id"], now=evening))
            store.complete(first["stable_id"], "failed", now=evening, retryable=True)
            self.assertEqual(store.due(midnight), [])
            self.assertFalse(store.claim(first["stable_id"], now=midnight))
            self.assertFalse(store.claim(second["stable_id"], now=midnight))
        with self.store(midnight + timedelta(hours=7)) as store:
            self.assertEqual(store.summary(midnight + timedelta(hours=7))["counts"]["expired"], 2)
            self.assertEqual(store.due(midnight + timedelta(hours=7)), [])

    def test_seven_am_exactly_is_allowed_and_night_plans_fail_closed(self):
        seven = NOW.replace(hour=4)
        item = reminder(at=seven)
        with self.store(seven - timedelta(minutes=1)) as store:
            store.activate(seven - timedelta(minutes=1))
            self.plan(store, [item], now=seven - timedelta(minutes=1))
            self.assertEqual(store.due(seven - timedelta(microseconds=1)), [])
            self.assertFalse(store.claim(item["stable_id"], now=seven - timedelta(microseconds=1)))
            self.assertEqual(store.due(seven), [item])
            self.assertTrue(store.claim(item["stable_id"], now=seven))
            with self.assertRaises(ControlEmailQueueError):
                self.plan(store, [reminder(at=seven - timedelta(hours=1))], now=seven)

    def test_full_snapshot_plan_and_metadata_rollback_together(self):
        with self.store() as store:
            self.plan(store)
            before, summary = self.raw(), store.summary(NOW)
            with patch.object(store, "_sync_metadata", side_effect=sqlite3.OperationalError("secret error")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.plan(store, [reminder(uid="new")], cache=[dict(RESOURCE, etag='"v2"')])
            self.assertEqual(store.load_cache(), [RESOURCE])
            self.assertEqual(self.raw(), before)
            self.assertEqual(store.summary(NOW), summary)

    def test_replanning_does_not_load_historical_receipt_bodies_into_memory(self):
        with self.store() as store:
            self.plan(store)
            with store.connection:
                for index in range(256):
                    historical = reminder(uid=f"historical-{index}", title="x" * 64_000)
                    stable_id, slot_id, due, payload = queue._reminder_row(historical)
                    store.connection.execute("INSERT INTO reminders VALUES (?,?,?,?,?,?,?,?,?)",
                                             (stable_id, slot_id, due, payload, "accepted", 0, None,
                                              NOW.timestamp(), NOW.timestamp()))
            tracemalloc.start()
            try:
                self.plan(store, [reminder(title="Current plan")])
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            # Historical bodies total over 16 MB. They must remain on disk
            # while current-plan reconciliation performs indexed lookups.
            self.assertLess(peak, 4_000_000)
            self.assertEqual(store.summary(NOW)["counts"]["accepted"], 256)

    def test_optional_cache_and_unchanged_sync_preserve_complete_inventory(self):
        with self.store() as store:
            self.plan(store)
            self.plan(store, [reminder(title="Updated")], cache=None, policy_version="2")
            store.mark_sync(NOW + timedelta(seconds=1), 1, {"changed": 0}, observed_at=NOW)
            self.assertEqual(store.load_cache(), [RESOURCE])
            self.assertEqual(store.summary(NOW)["policy_version"], "2")
            self.assertEqual(store.summary(NOW)["last_observation_at"], NOW.isoformat())

    def test_invalid_identity_duplicates_headers_recipients_or_scope_do_not_mutate_plan(self):
        invalid = [dict(reminder(), stable_id="bad"), reminder(source_id="google"),
                   reminder(account_email="other@example.com"), reminder(title="Unsafe\r\nBcc: other@example.com"),
                   reminder(calendar_name="Unsafe\x00name"), reminder(recipient="User@example.com"),
                   reminder(recipient="Name <user@example.com>"), reminder(recipient="a@example.com,b@example.com"),
                   reminder(recipient="a@example.com\r\nBcc:b@example.com"), reminder(recipient=".a@example.com"),
                   reminder(recipient="a..b@example.com"), reminder(recipient="a@-example.com"),
                   reminder(recipient="gpb.control@mail.ru"), reminder(rule="unsupported"),
                   reminder(event_end="2020-01-01T00:00:00+00:00"), reminder(event_start="2026-10-19T10:00:00"),
                   reminder(notify_at="2026-10-19T10:00:00"), dict(reminder(), cc=["other@example.com"])]
        with self.store() as store:
            self.plan(store)
            before = self.raw()
            for item in invalid:
                with self.subTest(item=item["stable_id"]), self.assertRaises(ControlEmailQueueError):
                    self.plan(store, [item])
                self.assertEqual(self.raw(), before)
            with self.assertRaises(ControlEmailQueueError):
                self.plan(store, [reminder(), reminder()])
            with self.assertRaises(ControlEmailQueueError):
                self.plan(store, cache=[dict(RESOURCE, source_id="icloud")])
            with self.assertRaises(ControlEmailQueueError):
                self.plan(store, cache=[RESOURCE, RESOURCE])
            with self.assertRaises(ControlEmailQueueError):
                self.plan(store, [reminder(at=NOW + timedelta(days=7))])
            with self.assertRaises(ControlEmailQueueError):
                self.plan(store, observed_at=NOW + timedelta(seconds=1))
            self.assertEqual(self.raw(), before)

    def test_actual_planner_dataclass_identity_and_all_day_payload_are_compatible(self):
        from calendar_digest.control_email_rules import ControlEmailReminder
        item = reminder(all_day=True, event_start="2026-10-19", event_end="2026-10-20", rule="d0")
        typed = ControlEmailReminder(**{key: value for key, value in item.items() if key != "stable_id"})
        self.assertEqual(typed.stable_id, item["stable_id"])
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [dict(asdict(typed), stable_id=typed.stable_id)])
            self.assertEqual(store.due(NOW), [item])

    def test_summary_contains_only_counts_times_and_fixed_policy(self):
        with self.store() as store:
            self.plan(store)
            store.activate(NOW)
            summary = store.summary(NOW)
            self.assertEqual(summary["delivery_not_before"], "07:00")
            self.assertEqual(summary["delivery_timezone"], "Europe/Moscow")
            self.assertTrue(store.summary(NOW + timedelta(minutes=5))["sync_recent"])
            self.assertFalse(store.summary(NOW + timedelta(minutes=5))["sync_fresh_for_delivery"])
            self.assertFalse(store.summary(NOW + timedelta(minutes=10, seconds=1))["sync_recent"])
        rendered = json.dumps(summary, ensure_ascii=False)
        for secret in (*RESOURCE.values(), reminder()["uid"], reminder()["stable_id"], reminder()["title"],
                       reminder()["recipient"], reminder()["account_email"]):
            self.assertNotIn(secret, rendered)

    def test_managed_mount_permissions_and_other_delivery_files_are_untouched(self):
        self.directory.mkdir(mode=0o775)
        self.directory.chmod(0o2775)
        others = []
        for name in ("push-schedule", "push-delivery", "mail-delivery"):
            other = self.directory / name
            other.mkdir()
            file = other / "existing.sqlite3"
            file.write_bytes(b"unrelated receipt")
            others.append(file)
        root_inode = self.directory.stat().st_ino
        original = os.fchmod

        def forbid_root(descriptor, mode):
            if os.fstat(descriptor).st_ino == root_inode:
                raise PermissionError("managed mount")
            return original(descriptor, mode)

        with patch.object(queue.os, "fchmod", forbid_root), self.store() as store:
            self.plan(store)
            self.assertEqual(store.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o2775)
        self.assertEqual(stat.S_IMODE(self.database.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.directory / ".control-email.lock").stat().st_mode), 0o600)
        self.assertTrue(all(file.read_bytes() == b"unrelated receipt" for file in others))

    def test_competing_process_cannot_open_locked_store(self):
        code = ("from pathlib import Path\nfrom calendar_digest.control_email_queue import control_email_store,ControlEmailQueueError\n"
                "import sys\ntry:\n with control_email_store(Path(sys.argv[1])): pass\n"
                "except ControlEmailQueueError as e:\n assert e.retryable\n sys.exit(0)\nsys.exit(2)\n")
        with self.store():
            result = subprocess.run([sys.executable, "-c", code, str(self.directory)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_symlink_and_hardlink_lock_rejected_without_touching_target(self):
        self.directory.mkdir()
        target = self.directory / "private-target"
        target.write_text("unchanged")
        target.chmod(0o640)
        lock = self.directory / ".control-email.lock"
        for form in ("symlink", "hardlink"):
            if form == "symlink":
                lock.symlink_to(target)
            else:
                os.link(target, lock)
            with self.assertRaises(ControlEmailQueueError), self.store():
                pass
            self.assertEqual(target.read_text(), "unchanged")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            lock.unlink()

    def test_symlink_root_directory_database_and_sidecars_are_rejected(self):
        self.directory.mkdir()
        external = self.directory.parent / "external"
        external.mkdir()
        linked = self.directory.parent / "linked"
        linked.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ControlEmailQueueError), control_email_store(linked, now=NOW):
            pass
        self.database.parent.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ControlEmailQueueError), self.store():
            pass
        self.database.parent.unlink()
        self.database.parent.mkdir()
        target = external / "target"
        target.write_text("unchanged")
        for name in ("schedule.sqlite3", "schedule.sqlite3-journal", "schedule.sqlite3-wal", "schedule.sqlite3-shm"):
            entry = self.database.parent / name
            entry.symlink_to(target)
            with self.assertRaises(ControlEmailQueueError), self.store():
                pass
            entry.unlink()
            self.assertEqual(target.read_text(), "unchanged")

    def test_lock_replacement_is_rejected(self):
        original = queue.fcntl.flock

        def replace(descriptor, flags):
            result = original(descriptor, flags)
            lock = self.directory / ".control-email.lock"
            lock.rename(self.directory / "old-lock")
            lock.touch(mode=0o600)
            return result

        with patch.object(queue.fcntl, "flock", replace), self.assertRaises(ControlEmailQueueError), self.store():
            pass

    def test_safe_storage_failures_and_body_exceptions_preserve_their_meaning(self):
        with patch.object(queue.os, "open", side_effect=OSError("private secret path")):
            with self.assertRaises(ControlEmailQueueError) as failure, self.store():
                pass
            self.assertNotIn("private secret path", str(failure.exception))
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(self.directory / "other")}):
            with self.assertRaises(ControlEmailQueueError), self.store():
                pass
        with self.assertRaises(ControlEmailQueueError), self.store(NOW.replace(tzinfo=None)):
            pass
        sentinel = RuntimeError("caller handles this source failure")
        with self.assertRaises(RuntimeError) as failure, self.store():
            raise sentinel
        self.assertIs(failure.exception, sentinel)
        with self.assertRaises(ControlEmailQueueError) as failure, self.store():
            raise sqlite3.OperationalError("private SQL detail")
        self.assertNotIn("private SQL detail", str(failure.exception))

    def test_import_is_lightweight_and_does_not_load_existing_push_storage(self):
        code = ("import sys; import calendar_digest.control_email_queue; "
                "assert not any(name in sys.modules for name in "
                "('caldav','icalendar','calendar_digest.push_queue','calendar_digest.smtp_delivery',"
                "'calendar_digest.control_email_rules'))")
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
