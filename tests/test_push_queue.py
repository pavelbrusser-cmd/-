"""Private automatic queue tests: no CalDAV reads or provider submissions."""

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
import unittest
from unittest.mock import patch

from calendar_digest import push_queue
from calendar_digest.push_queue import QueueError, schedule_store


NOW = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
RESOURCE = {"source_id": "mailru_2", "calendar_id": "private-calendar",
            "calendar_name": "Частный календарь", "href": "/private-resource.ics", "etag": '"v1"',
            "data": "BEGIN:VCALENDAR\nprivate calendar content\nEND:VCALENDAR"}
CONTACT = {"book_id": "private-book", "uid": "private-contact", "name": "Личное имя",
           "month": 10, "day": 18}


def reminder(*, at=NOW, uid="private-uid", rule="h3", title="Личное поручение"):
    result = {"source_id": "mailru_2", "calendar_id": "private-calendar",
              "calendar_name": "Частный календарь", "uid": uid, "recurrence_id": None,
              "title": title, "event_start": (at + timedelta(hours=3)).isoformat(),
              "all_day": False, "category": "control", "rule": rule,
              "notify_at": at.isoformat(), "classification_evidence": []}
    identity = [result[key] for key in ("source_id", "calendar_id", "uid", "recurrence_id", "rule")]
    result["stable_id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                                                   separators=(",", ":")).encode()).hexdigest()
    return result


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary) / "state"
        self.database = self.directory / "push-schedule" / "schedule.sqlite3"

    def store(self, now=NOW):
        return schedule_store(self.directory, now=now)

    def plan(self, store, entries=None, *, now=NOW, cache=None):
        store.commit_plan([RESOURCE] if cache is None else cache,
                          [reminder()] if entries is None else entries,
                          now=now, horizon=now + timedelta(days=7), calendar_count=11,
                          sync_summary={"changed": 1})

    def counts(self, now=NOW):
        with self.store(now) as store:
            return store.summary(now)["counts"]

    def raw(self):
        with sqlite3.connect(self.database) as database:
            database.row_factory = sqlite3.Row
            return [dict(row) for row in database.execute("SELECT * FROM reminders ORDER BY notify_at")]

    def test_private_files_managed_root_and_manual_test_database_untouched(self):
        self.directory.mkdir(mode=0o775)
        self.directory.chmod(0o2775)
        manual = self.directory / "push-delivery"
        manual.mkdir()
        manual_db = manual / "push.sqlite3"
        manual_db.write_bytes(b"existing manual test receipt")
        root_inode = self.directory.stat().st_ino
        original = os.fchmod

        def forbid_root(descriptor, mode):
            if os.fstat(descriptor).st_ino == root_inode:
                raise PermissionError("managed provider-owned mount")
            return original(descriptor, mode)

        with patch.object(push_queue.os, "fchmod", side_effect=forbid_root), self.store() as store:
            self.plan(store)
            self.assertEqual(store.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(store.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o2775)
        self.assertEqual(stat.S_IMODE(self.database.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.database.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.directory / ".push-schedule.lock").stat().st_mode), 0o600)
        self.assertEqual(manual_db.read_bytes(), b"existing manual test receipt")

    def test_import_does_not_import_calendar_parsers_or_transport(self):
        code = "import sys; import calendar_digest.push_queue; assert not any(k in sys.modules for k in ('caldav','icalendar','calendar_digest.push_transport','calendar_digest.reminders'))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inactive_plan_preview_is_durable_without_delivery(self):
        with self.store() as store:
            self.plan(store)
            self.assertEqual(store.due(NOW), [])
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW))
            self.assertEqual(store.load_cache(), [RESOURCE])
            summary = store.summary(NOW)
            self.assertIsNone(summary["activated_at"])
            self.assertIsNone(summary["next_due_at"])
            self.assertEqual(summary["resources"], 1)
            self.assertEqual(summary["calendars"], 11)
        self.assertEqual(self.counts()["pending"], 1)

    def contact_plan(self, store, *, contacts=None, enabled=True):
        store.commit_plan([RESOURCE], [reminder()], now=NOW, horizon=NOW + timedelta(days=7),
                          calendar_count=11, sync_summary={},
                          contacts=[CONTACT] if contacts is None else contacts,
                          contacts_enabled=enabled, addressbook_count=1 if enabled else 0)

    def test_minimal_contact_snapshot_persists_only_five_fields(self):
        with self.store() as store:
            self.contact_plan(store)
            self.assertEqual(store.load_contacts(), [CONTACT])
            columns = store.connection.execute("PRAGMA table_info(birthday_contacts)").fetchall()
            self.assertEqual([row["name"] for row in columns], ["book_id", "uid", "name", "month", "day"])
            summary = store.summary(NOW)
            self.assertTrue(summary["contacts_enabled"])
            self.assertEqual(summary["contacts"], 1)
            self.assertEqual(summary["addressbooks"], 1)
            for private in (CONTACT["uid"], CONTACT["name"], CONTACT["book_id"]):
                self.assertNotIn(private, json.dumps(summary, ensure_ascii=False))
        with self.store() as store:
            self.assertEqual(store.load_contacts(), [CONTACT])

    def test_contact_snapshot_and_calendar_queue_rollback_together(self):
        with self.store() as store:
            self.contact_plan(store)
            before = store.summary(NOW)
            with patch.object(store, "_sync_metadata", side_effect=sqlite3.OperationalError("private detail")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.contact_plan(store, contacts=[dict(CONTACT, day=19, name="Другое имя")])
            self.assertEqual(store.load_contacts(), [CONTACT])
            self.assertEqual(store.load_cache(), [RESOURCE])
            self.assertEqual(store.summary(NOW), before)

    def test_contacts_extraneous_fields_incomplete_or_duplicate_snapshot_rejected(self):
        with self.store() as store:
            self.contact_plan(store)
            for contacts in ([dict(CONTACT, phone="unsaved-private-phone")], [CONTACT, CONTACT],
                             [dict(CONTACT, month=2, day=30)], [dict(CONTACT, day=True)]):
                with self.assertRaises(QueueError):
                    self.contact_plan(store, contacts=contacts)
                self.assertEqual(store.load_contacts(), [CONTACT])
            with self.assertRaises(QueueError):
                store.commit_plan([RESOURCE], [], now=NOW, horizon=NOW + timedelta(days=7),
                                  calendar_count=11, sync_summary={}, contacts_enabled=True)
            self.assertNotIn(b"unsaved-private-phone", self.database.read_bytes())

    def test_disabled_contact_source_clears_snapshot_and_metadata(self):
        with self.store() as store:
            self.contact_plan(store)
            self.plan(store)
            self.assertEqual(store.load_contacts(), [])
            summary = store.summary(NOW)
            self.assertFalse(summary["contacts_enabled"])
            self.assertEqual(summary["contacts"], 0)
            self.assertEqual(summary["addressbooks"], 0)

    def test_unchanged_sync_preserves_contacts_and_cannot_switch_source_without_plan(self):
        with self.store() as store:
            self.contact_plan(store)
            store.mark_sync(NOW, 11, {}, contacts_enabled=True, addressbook_count=1)
            store.mark_sync(NOW, 11, {})
            self.assertEqual(store.load_contacts(), [CONTACT])
            with self.assertRaises(QueueError):
                store.mark_sync(NOW, 11, {}, contacts_enabled=False, addressbook_count=0)
            with self.assertRaises(QueueError):
                store.mark_sync(NOW, 11, {}, addressbook_count=0)
            self.assertTrue(store.summary(NOW)["contacts_enabled"])

    def test_first_activation_expires_backlog_and_is_idempotent(self):
        earlier = reminder(at=NOW - timedelta(minutes=1), uid="earlier")
        current = reminder()
        with self.store() as store:
            self.plan(store, [earlier, current])
            store.activate(NOW)
            store.activate(NOW + timedelta(seconds=1))
            self.assertEqual(store.due(NOW), [current])
            self.assertEqual(store.summary(NOW)["activated_at"], NOW.isoformat())
            self.assertEqual(store.summary(NOW)["counts"]["expired"], 1)
        with self.store() as store:
            self.plan(store, [earlier, current])
            self.assertEqual(store.due(NOW), [current])

    def test_checkpoint_is_durable_before_submission_and_acceptance_is_permanent(self):
        item = reminder()
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            self.assertTrue(store.claim(item["stable_id"], now=NOW))
            self.assertEqual(self.raw()[0]["state"], "sending")
            self.assertFalse(store.claim(item["stable_id"], now=NOW))
            store.complete(item["stable_id"], "accepted", now=NOW)
            self.plan(store, [])
            moved = reminder(at=NOW + timedelta(hours=2))
            self.plan(store, [moved])
            self.assertEqual(store.summary(NOW)["counts"]["accepted"], 1)
            self.assertEqual(store.due(NOW + timedelta(hours=2)), [])
        self.assertEqual(self.counts()["accepted"], 1)

    def test_crash_after_checkpoint_recovers_to_uncertain_without_repeat(self):
        with self.assertRaises(SystemExit):
            with self.store() as store:
                self.plan(store)
                store.activate(NOW)
                self.assertTrue(store.claim(reminder()["stable_id"], now=NOW))
                raise SystemExit()
        self.assertEqual(self.raw()[0]["state"], "sending")
        with self.store() as store:
            self.assertEqual(store.summary(NOW)["counts"]["uncertain"], 1)
            self.plan(store)
            self.assertEqual(store.due(NOW), [])
            with self.assertRaises(QueueError):
                store.complete(reminder()["stable_id"], "failed", now=NOW, retryable=True)

    def test_uncertain_without_checkpoint_also_blocks_repeat(self):
        with self.store() as store:
            self.plan(store)
            store.activate(NOW)
            store.complete(reminder()["stable_id"], "uncertain", now=NOW)
            self.plan(store)
            self.assertEqual(store.due(NOW), [])
            self.assertEqual(store.summary(NOW)["counts"]["uncertain"], 1)

    def test_policy_version_commits_atomically_and_survives_unchanged_sync(self):
        with self.store() as store:
            self.assertIsNone(store.summary(NOW)["policy_version"])
            store.commit_plan([RESOURCE], [reminder()], now=NOW,
                              horizon=NOW + timedelta(days=7), calendar_count=11,
                              sync_summary={}, policy_version="2")
            store.mark_sync(NOW + timedelta(seconds=1), 11, {})
            self.assertEqual(store.summary(NOW)["policy_version"], "2")

    def test_move_cancel_and_future_return_reconcile_pending_only(self):
        future = reminder(at=NOW + timedelta(hours=1))
        moved = reminder(at=NOW + timedelta(hours=2), title="Изменённое поручение")
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [future])
            self.plan(store, [moved])
            self.assertEqual(store.summary(NOW)["next_due_at"], (NOW + timedelta(hours=2)).isoformat())
            self.assertEqual(store.due(NOW + timedelta(hours=1)), [])
            self.plan(store, [])
            self.assertEqual(store.summary(NOW)["counts"]["cancelled"], 1)
            self.plan(store, [moved])
            self.assertEqual(store.due(NOW + timedelta(hours=2)), [moved])

    def test_cancelled_overdue_is_not_revived_by_return(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            self.plan(store, [])
            self.plan(store, now=NOW + timedelta(seconds=1))
            self.assertEqual(store.due(NOW + timedelta(seconds=1)), [])
            self.assertEqual(store.summary(NOW)["counts"]["cancelled"], 1)

    def test_expired_reminder_can_be_moved_into_future(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            later = NOW + timedelta(minutes=16)
            self.assertEqual(store.summary(later)["counts"]["expired"], 1)
            moved = reminder(at=later + timedelta(hours=1))
            self.plan(store, [moved], now=later)
            self.assertEqual(store.summary(later)["counts"]["pending"], 1)

    def test_missing_or_stale_sync_prevents_claim_even_if_due(self):
        with self.store() as store:
            store.activate(NOW)
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW))
            self.plan(store)
            later = NOW + timedelta(seconds=121)
            self.assertFalse(store.claim(reminder()["stable_id"], now=later))
            self.assertEqual(len(store.due(later)), 1)
            store.mark_sync(later, 11, {"changed": 0})
            self.assertTrue(store.claim(reminder()["stable_id"], now=later))

    def test_idle_sync_health_is_distinct_from_strict_delivery_freshness(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            healthy_idle = store.summary(NOW + timedelta(minutes=5))
            self.assertTrue(healthy_idle["sync_recent"])
            self.assertFalse(healthy_idle["sync_fresh_for_delivery"])
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW + timedelta(minutes=5)))
            overdue_sync = store.summary(NOW + timedelta(minutes=10, seconds=1))
            self.assertFalse(overdue_sync["sync_recent"])
            self.assertFalse(overdue_sync["sync_fresh_for_delivery"])

    def test_long_inventory_completion_does_not_refresh_old_observations(self):
        observed = NOW - timedelta(seconds=121)
        with self.store() as store:
            store.activate(NOW)
            store.commit_plan([RESOURCE], [reminder()], now=NOW,
                              horizon=NOW + timedelta(days=7), calendar_count=11,
                              sync_summary={}, observed_at=observed)
            summary = store.summary(NOW)
            self.assertTrue(summary["sync_recent"])
            self.assertFalse(summary["sync_fresh_for_delivery"])
            self.assertEqual(summary["last_sync_at"], NOW.isoformat())
            self.assertEqual(summary["last_observation_at"], observed.isoformat())
            self.assertFalse(store.claim(reminder()["stable_id"], now=NOW))
            # A subsequent fast unchanged scan can safely release the due item.
            later = NOW + timedelta(seconds=1)
            store.mark_sync(later, 11, {}, observed_at=NOW)
            self.assertTrue(store.summary(later)["sync_fresh_for_delivery"])
            self.assertTrue(store.claim(reminder()["stable_id"], now=later))

    def test_old_metadata_without_observation_uses_sync_timestamp(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            with store.connection:
                store.connection.execute("DELETE FROM metadata WHERE key='last_observation_at'")
            self.assertEqual(store.summary(NOW)["last_observation_at"], NOW.isoformat())
            self.assertTrue(store.claim(reminder()["stable_id"], now=NOW))

    def test_exact_freshness_and_grace_boundaries_and_no_clock_reversal(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [reminder(uid="first"), reminder(uid="second"), reminder(uid="third")])
            self.assertFalse(store.claim(reminder(uid="first")["stable_id"], now=NOW - timedelta(seconds=1)))
            self.assertTrue(store.claim(reminder(uid="first")["stable_id"], now=NOW + timedelta(seconds=120)))
            late = NOW + timedelta(minutes=15)
            store.mark_sync(late, 11, {})
            self.assertTrue(store.claim(reminder(uid="second")["stable_id"], now=late))
            self.assertFalse(store.claim(reminder(uid="third")["stable_id"], now=late + timedelta(microseconds=1)))
            self.assertEqual(store.due(late + timedelta(microseconds=1)), [])

    def test_known_retry_waits_one_minute_and_refresh_does_not_reset_it(self):
        item = reminder()
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            self.assertTrue(store.claim(item["stable_id"], now=NOW))
            store.complete(item["stable_id"], "failed", now=NOW, retryable=True)
            self.plan(store)
            self.assertEqual(store.due(NOW + timedelta(seconds=59)), [])
            self.assertEqual(store.summary(NOW)["next_due_at"], (NOW + timedelta(minutes=1)).isoformat())
            self.assertEqual(store.due(NOW + timedelta(minutes=1)), [item])
            self.assertTrue(store.claim(item["stable_id"], now=NOW + timedelta(minutes=1)))

    def test_nonretryable_failure_stays_stopped_unless_event_changes(self):
        with self.store() as store:
            store.activate(NOW)
            self.plan(store)
            # Provider validation can reject a notification before checkpoint.
            store.complete(reminder()["stable_id"], "failed", now=NOW)
            self.plan(store)
            self.assertEqual(store.due(NOW), [])
            moved = reminder(at=NOW + timedelta(minutes=1))
            self.plan(store, [moved])
            self.assertEqual(store.due(NOW + timedelta(minutes=1)), [moved])

    def test_payload_dictionary_order_does_not_revive_failure(self):
        item = reminder()
        with self.store() as store:
            store.activate(NOW)
            self.plan(store, [item])
            store.complete(item["stable_id"], "failed", now=NOW)
            self.plan(store, [dict(reversed(list(item.items())))])
            self.assertEqual(store.due(NOW), [])

    def test_atomic_plan_rollback_keeps_previous_inventory_and_queue(self):
        with self.store() as store:
            self.plan(store)
            old = store.summary(NOW)
            with patch.object(store, "_sync_metadata", side_effect=sqlite3.OperationalError("private detail")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.plan(store, [reminder(uid="new")], cache=[dict(RESOURCE, etag='"v2"')])
            self.assertEqual(store.load_cache(), [RESOURCE])
            self.assertEqual(store.summary(NOW), old)
            self.assertEqual(len(self.raw()), 1)
            self.assertEqual(self.raw()[0]["stable_id"], reminder()["stable_id"])

    def test_rejected_plan_does_not_mutate_previous_state(self):
        with self.store() as store:
            self.plan(store)
            for entries in ([reminder(), reminder()], [dict(reminder(), stable_id="incorrect")],
                            [reminder(at=NOW + timedelta(days=7))]):
                with self.subTest(entries=len(entries)), self.assertRaises(QueueError):
                    self.plan(store, entries)
            with self.assertRaises(QueueError):
                self.plan(store, cache=[RESOURCE, RESOURCE])
            self.assertEqual(store.load_cache(), [RESOURCE])
            self.assertEqual(store.summary(NOW)["counts"]["pending"], 1)

    def test_summary_contains_only_counts_and_times(self):
        with self.store() as store:
            self.plan(store)
            store.activate(NOW)
            serialized = json.dumps(store.summary(NOW), ensure_ascii=False)
        for private in (*RESOURCE.values(), reminder()["title"], reminder()["uid"], reminder()["stable_id"]):
            self.assertNotIn(private, serialized)

    def test_restart_retains_cache_sync_and_activation(self):
        with self.store() as store:
            self.plan(store)
            store.activate(NOW)
        with self.store(NOW + timedelta(seconds=1)) as store:
            self.assertEqual(store.load_cache(), [RESOURCE])
            summary = store.summary(NOW + timedelta(seconds=1))
            self.assertEqual(summary["activated_at"], NOW.isoformat())
            self.assertEqual(summary["last_sync_at"], NOW.isoformat())
            self.assertTrue(summary["sync_recent"])
            self.assertEqual(store.due(NOW + timedelta(seconds=1)), [reminder()])

    def test_competing_process_cannot_open_locked_store(self):
        code = ("from pathlib import Path\nfrom calendar_digest.push_queue import schedule_store,QueueError\n"
                "import sys\ntry:\n with schedule_store(Path(sys.argv[1])): pass\n"
                "except QueueError as e:\n assert e.retryable\n sys.exit(0)\nsys.exit(2)\n")
        with self.store():
            result = subprocess.run([sys.executable, "-c", code, str(self.directory)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_symlink_and_hardlink_lock_targets_untouched(self):
        self.directory.mkdir()
        target = self.directory / "target"
        target.write_text("unchanged", encoding="utf-8")
        target.chmod(0o640)
        lock = self.directory / ".push-schedule.lock"
        for form in ("symlink", "hardlink"):
            if form == "symlink":
                lock.symlink_to(target)
            else:
                os.link(target, lock)
            with self.assertRaises(QueueError), self.store():
                pass
            self.assertEqual(target.read_text(), "unchanged")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
            lock.unlink()

    def test_symlink_root_private_directory_database_and_sidecar_rejected(self):
        self.directory.mkdir()
        external = self.directory.parent / "external"
        external.mkdir()
        link = self.directory.parent / "linked-root"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(QueueError), schedule_store(link, now=NOW):
            pass
        private = self.database.parent
        private.symlink_to(external, target_is_directory=True)
        with self.assertRaises(QueueError), self.store():
            pass
        private.unlink()
        private.mkdir()
        target = external / "target"
        target.write_text("unchanged", encoding="utf-8")
        for name in ("schedule.sqlite3", "schedule.sqlite3-journal", "schedule.sqlite3-wal", "schedule.sqlite3-shm"):
            entry = private / name
            entry.symlink_to(target)
            with self.assertRaises(QueueError), self.store():
                pass
            entry.unlink()
            self.assertEqual(target.read_text(), "unchanged")

    def test_lock_replacement_during_acquisition_is_rejected(self):
        original = push_queue.fcntl.flock

        def replace(descriptor, flags):
            result = original(descriptor, flags)
            lock = self.directory / ".push-schedule.lock"
            lock.rename(self.directory / "old-lock")
            lock.touch(mode=0o600)
            return result

        with patch.object(push_queue.fcntl, "flock", side_effect=replace):
            with self.assertRaises(QueueError), self.store():
                pass

    def test_safe_errors_no_private_details_and_environment_directory_guard(self):
        with patch.object(push_queue.os, "open", side_effect=OSError("private-secret-path")):
            with self.assertRaises(QueueError) as failure, self.store():
                pass
            self.assertNotIn("private-secret-path", str(failure.exception))
        with patch.dict(os.environ, {"DIGEST_STATE_DIR": str(self.directory / "different")}):
            with self.assertRaises(QueueError), self.store():
                pass
        with self.assertRaises(QueueError), self.store(NOW.replace(tzinfo=None)):
            pass


if __name__ == "__main__":
    unittest.main()
