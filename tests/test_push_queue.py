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
CONTACT_BOOK_A = "https://contacts.icloud.com/123456/carddavhome/card/"
CONTACT_BOOK_B = "https://p12-contacts.icloud.com/123456/carddavhome/%63ard"


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


def contact_reminder(book, *, at=NOW, day=18, title="Личное имя", uid="private-contact"):
    result = dict(reminder(at=at, uid=uid, rule="d3", title=title),
                  source_id="icloud_contacts", calendar_id=book,
                  recurrence_id="birthday:2026", event_start=f"2026-10-{day:02}",
                  all_day=True, category="birthdays", classification_evidence=["contacts"])
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

    def legacy_contacts(self, contacts, attempts, *, now=NOW, version=1):
        """Insert a historical store exactly as the previous release wrote it."""
        with self.store(now) as store:
            store.activate(now)
            store.mark_sync(now, 11, {})
        with sqlite3.connect(self.database) as database:
            database.execute(f"PRAGMA user_version={version}")
            database.executemany("INSERT INTO birthday_contacts VALUES (?,?,?,?,?)",
                                 [tuple(contact[field] for field in ("book_id", "uid", "name", "month", "day"))
                                  for contact in contacts])
            for payload, state, updated, retryable, retry_at in attempts:
                database.execute("""INSERT INTO reminders VALUES (?,?,?,?,?,?,?,?)""", (
                    payload["stable_id"], datetime.fromisoformat(payload["notify_at"]).timestamp(),
                    push_queue._json(payload), state, int(retryable),
                    None if retry_at is None else retry_at.timestamp(), now.timestamp(), updated.timestamp(),
                ))

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

    def test_contact_alias_pending_beats_cancelled_and_snapshot_coalesces(self):
        from calendar_digest.contact_identity import canonical_contact_book_id

        old, active = contact_reminder(CONTACT_BOOK_A), contact_reminder(CONTACT_BOOK_B)
        # Cancelled can be newer: the currently active alias still takes priority.
        self.legacy_contacts([dict(CONTACT, book_id=CONTACT_BOOK_A), dict(CONTACT, book_id=CONTACT_BOOK_B)], [
            (old, "cancelled", NOW + timedelta(seconds=1), False, None),
            (active, "pending", NOW, True, None),
        ])
        canonical = canonical_contact_book_id(CONTACT_BOOK_A)
        with self.store() as store:
            self.assertEqual(store.load_contacts(), [dict(CONTACT, book_id=canonical)])
            due = store.due(NOW)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0]["calendar_id"], canonical)
            self.assertEqual(store.summary(NOW)["counts"]["cancelled"], 0)
            self.assertTrue(store.claim(due[0]["stable_id"], now=NOW))

    def test_contact_alias_accepted_receipt_dominates_new_pending_date_correction(self):
        from calendar_digest.contact_identity import canonical_contact_book_id

        old = contact_reminder(CONTACT_BOOK_A, title="Имя при отправке")
        pending = contact_reminder(CONTACT_BOOK_B, at=NOW + timedelta(minutes=2), day=19, title="Новое имя")
        self.legacy_contacts([], [
            (old, "accepted", NOW, False, None),
            (pending, "pending", NOW + timedelta(seconds=1), True, None),
        ])
        with self.store() as store:
            self.assertEqual(store.due(NOW + timedelta(minutes=2)), [])
            self.assertEqual(store.summary(NOW)["counts"]["accepted"], 1)
            self.assertEqual(store.summary(NOW)["counts"]["pending"], 0)
        saved = self.raw()[0]
        payload = json.loads(saved["payload"])
        self.assertEqual(payload["calendar_id"], canonical_contact_book_id(CONTACT_BOOK_A))
        self.assertEqual(payload["title"], old["title"])
        self.assertEqual(payload["event_start"], old["event_start"])
        self.assertEqual(saved["notify_at"], NOW.timestamp())
        # Replanning the corrected canonical event must not create a second send.
        canonical_pending = contact_reminder(canonical_contact_book_id(CONTACT_BOOK_B),
                                             at=NOW + timedelta(minutes=2), day=19, title="Новое имя")
        with self.store() as store:
            self.plan(store, [canonical_pending])
            self.assertEqual(store.due(NOW + timedelta(minutes=2)), [])
            self.assertEqual(store.summary(NOW)["counts"]["accepted"], 1)

    def test_contact_alias_uncertain_and_interrupted_sending_dominate_pending(self):
        for index, state in enumerate(("uncertain", "sending")):
            with self.subTest(state=state):
                uid = f"contact-{index}"
                old, active = contact_reminder(CONTACT_BOOK_A, uid=uid), contact_reminder(CONTACT_BOOK_B, uid=uid)
                self.legacy_contacts([], [
                    (old, state, NOW, False, None),
                    (active, "pending", NOW + timedelta(seconds=1), True, None),
                ])
                with self.store() as store:
                    self.assertEqual(store.due(NOW), [])
                    self.assertEqual(store.summary(NOW)["counts"]["uncertain"], index + 1)
                    self.assertEqual(store.summary(NOW)["counts"]["pending"], 0)

    def test_contact_alias_latest_active_failure_preserves_retry_delay(self):
        old, failed = contact_reminder(CONTACT_BOOK_A), contact_reminder(CONTACT_BOOK_B)
        retry_at = NOW + timedelta(minutes=1)
        self.legacy_contacts([], [
            (old, "pending", NOW, True, None),
            (failed, "failed", NOW + timedelta(seconds=1), True, retry_at),
        ])
        with self.store() as store:
            self.assertEqual(store.due(NOW), [])
            self.assertEqual(len(store.due(retry_at)), 1)
        self.assertEqual(self.raw()[0]["state"], "failed")
        self.assertEqual(self.raw()[0]["retry_at"], retry_at.timestamp())
        self.assertEqual(self.raw()[0]["retryable"], 1)

    def test_contact_alias_nonretryable_failure_remains_stopped(self):
        old, failed = contact_reminder(CONTACT_BOOK_A), contact_reminder(CONTACT_BOOK_B)
        self.legacy_contacts([], [
            (old, "pending", NOW, True, None),
            (failed, "failed", NOW + timedelta(seconds=1), False, None),
        ])
        with self.store() as store:
            self.assertEqual(store.due(NOW), [])
        self.assertEqual(self.raw()[0]["state"], "failed")
        self.assertEqual(self.raw()[0]["retryable"], 0)

    def test_contact_migration_keeps_other_accounts_books_and_calendar_payloads_distinct(self):
        books = [CONTACT_BOOK_A,
                 "https://contacts.icloud.com/123456/carddavhome/other/",
                 "https://contacts.icloud.com/654321/carddavhome/card/"]
        calendar = reminder()
        self.legacy_contacts([dict(CONTACT, book_id=book) for book in books],
            [(contact_reminder(book), "pending", NOW, True, None) for book in books]
            + [(calendar, "pending", NOW, True, None)])
        calendar_before = next(row for row in self.raw() if row["stable_id"] == calendar["stable_id"])
        with self.store() as store:
            self.assertEqual(len(store.load_contacts()), 3)
            self.assertEqual(len(store.due(NOW)), 4)
            self.assertEqual(len({item["calendar_id"] for item in store.due(NOW)
                                  if item["source_id"] == "icloud_contacts"}), 3)
        calendar_after = next(row for row in self.raw() if row["stable_id"] == calendar["stable_id"])
        self.assertEqual(calendar_before, calendar_after)

    def test_contact_identity_migration_is_idempotent(self):
        self.legacy_contacts([dict(CONTACT, book_id=CONTACT_BOOK_A)], [
            (contact_reminder(CONTACT_BOOK_A), "pending", NOW, True, None),
        ])
        with self.store() as store:
            contacts = store.load_contacts()
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        first = self.raw()
        with self.store() as store:
            self.assertEqual(store.load_contacts(), contacts)
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.raw(), first)

    def test_new_store_marks_version_three_only_after_successful_recovery(self):
        original = push_queue.QueueStore._migrate_contact_identities
        versions = []

        def migration(store, now):
            versions.append(store.connection.execute("PRAGMA user_version").fetchone()[0])
            return original(store, now)

        with patch.object(push_queue.QueueStore, "_migrate_contact_identities", migration):
            with self.store() as store:
                self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(versions, [1])

    def test_failed_initial_recovery_does_not_mark_database_migrated(self):
        with patch.object(push_queue.QueueStore, "_migrate_contact_identities", side_effect=QueueError("migration stopped")):
            with self.assertRaises(QueueError), self.store():
                pass
        with sqlite3.connect(self.database) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 1)
        with self.store() as store:
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 3)

    def test_conflicting_contact_alias_snapshot_rolls_back_every_identity(self):
        initial_contacts = [dict(CONTACT, book_id=CONTACT_BOOK_A), dict(CONTACT, book_id=CONTACT_BOOK_B, day=19)]
        self.legacy_contacts(initial_contacts, [
            (contact_reminder(CONTACT_BOOK_A), "accepted", NOW, False, None),
            (contact_reminder(CONTACT_BOOK_B), "pending", NOW, True, None),
        ])
        before = self.raw()
        with self.assertRaises(QueueError) as failure, self.store():
            pass
        self.assertNotIn(CONTACT["name"], str(failure.exception))
        self.assertNotIn(CONTACT_BOOK_A, str(failure.exception))
        self.assertEqual(self.raw(), before)
        with sqlite3.connect(self.database) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 1)
            database.row_factory = sqlite3.Row
            after_contacts = [dict(row) for row in database.execute("SELECT * FROM birthday_contacts ORDER BY book_id")]
        self.assertEqual(after_contacts, sorted(initial_contacts, key=lambda row: row["book_id"]))

    def test_database_failure_after_contact_write_rolls_back_contacts_and_receipts(self):
        contact = dict(CONTACT, book_id=CONTACT_BOOK_A)
        self.legacy_contacts([contact], [(contact_reminder(CONTACT_BOOK_A), "accepted", NOW, False, None)])
        before = self.raw()
        with sqlite3.connect(self.database) as database:
            database.execute("""CREATE TRIGGER reject_migration BEFORE INSERT ON reminders
                BEGIN SELECT RAISE(ABORT,'private storage failure'); END""")
        with self.assertRaises(QueueError) as failure, self.store():
            pass
        self.assertNotIn("private storage failure", str(failure.exception))
        self.assertEqual(self.raw(), before)
        with sqlite3.connect(self.database) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 1)
            database.row_factory = sqlite3.Row
            self.assertEqual(dict(database.execute("SELECT * FROM birthday_contacts").fetchone()), contact)

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

    def test_night_migration_runs_before_expiry_when_restart_is_after_seven(self):
        seven = NOW.replace(hour=4)  # 07:00 Moscow, independently of server TZ.
        original = reminder(at=seven - timedelta(hours=4))
        restarted = seven + timedelta(minutes=5)
        self.legacy_contacts([], [(original, "pending", seven, True, None)],
                             now=seven - timedelta(hours=8), version=2)
        with self.store(restarted) as store:
            migrated = store.due(restarted)
            self.assertEqual(len(migrated), 1)
            self.assertEqual(migrated[0], dict(original, notify_at="2026-10-15T07:00:00+03:00"))
            summary = store.summary(restarted)
            self.assertEqual(summary["delivery_not_before"], "07:00")
            self.assertEqual(summary["delivery_timezone"], "Europe/Moscow")
            self.assertEqual(summary["counts"]["expired"], 0)
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertFalse(store.claim(original["stable_id"], now=restarted))  # Still needs a fresh scan.
            store.mark_sync(restarted, 11, {})
            self.assertTrue(store.claim(original["stable_id"], now=restarted))
            store.complete(original["stable_id"], "accepted", now=restarted)
        with self.store(restarted) as store:
            self.plan(store, migrated, now=restarted)
            self.assertEqual(store.due(restarted), [])
            self.assertEqual(store.summary(restarted)["counts"]["accepted"], 1)

    def test_migration_before_seven_waits_and_is_idempotent_on_restart(self):
        seven = NOW.replace(hour=4)
        original = reminder(at=seven - timedelta(hours=1))
        earlier = seven - timedelta(minutes=30)
        self.legacy_contacts([], [(original, "pending", earlier, True, None)],
                             now=seven - timedelta(hours=8), version=2)
        with self.store(earlier) as store:
            self.assertEqual(store.due(earlier), [])
            self.assertEqual(store.summary(earlier)["next_due_at"], seven.isoformat())
            self.assertFalse(store.claim(original["stable_id"], now=earlier))
        first = self.raw()
        with self.store(seven) as store:
            self.assertEqual(len(store.due(seven)), 1)
            self.assertEqual(store.due(seven - timedelta(microseconds=1)), [])
            store.mark_sync(seven, 11, {})
            self.assertFalse(store.claim(original["stable_id"], now=seven - timedelta(microseconds=1)))
        self.assertEqual(self.raw(), first)

    def test_migrated_reminder_keeps_exact_fifteen_minute_grace(self):
        seven = NOW.replace(hour=4)
        boundary = seven + timedelta(minutes=15)
        entries = [reminder(at=seven - timedelta(hours=4), uid=uid) for uid in ("first", "second")]
        self.legacy_contacts([], [(item, "pending", seven, True, None) for item in entries],
                             now=seven - timedelta(hours=8), version=2)
        with self.store(boundary) as store:
            self.assertEqual(len(store.due(boundary)), 2)
            store.mark_sync(boundary, 11, {})
            self.assertTrue(store.claim(entries[0]["stable_id"], now=boundary))
            store.complete(entries[0]["stable_id"], "accepted", now=boundary)
        with self.store(boundary + timedelta(microseconds=1)) as store:
            self.assertEqual(store.due(boundary + timedelta(microseconds=1)), [])
            self.assertEqual(store.summary(boundary + timedelta(microseconds=1))["counts"]["expired"], 1)

    def test_first_restart_beyond_migrated_grace_expires_without_backfill(self):
        seven = NOW.replace(hour=4)
        original = reminder(at=seven - timedelta(hours=4))
        restarted = seven + timedelta(minutes=15, seconds=1)
        self.legacy_contacts([], [(original, "pending", seven, True, None)],
                             now=seven - timedelta(hours=8), version=2)
        with self.store(restarted) as store:
            self.assertEqual(store.due(restarted), [])
            self.assertEqual(store.summary(restarted)["counts"]["expired"], 1)

    def test_retryable_night_failures_keep_backoff_across_migration_and_refresh(self):
        seven = NOW.replace(hour=4)
        restarted = seven + timedelta(minutes=5)
        first = reminder(at=seven - timedelta(hours=4), uid="first")
        second = reminder(at=seven - timedelta(hours=4), uid="second")
        later_retry = seven + timedelta(minutes=6)
        self.legacy_contacts([], [
            (first, "failed", seven, True, seven - timedelta(hours=3, minutes=59)),
            (second, "failed", seven, True, later_retry),
        ], now=seven - timedelta(hours=8), version=2)
        normalized = [dict(item, notify_at="2026-10-15T07:00:00+03:00") for item in (first, second)]
        with self.store(restarted) as store:
            self.assertEqual(store.due(restarted), [normalized[0]])
            self.plan(store, normalized, now=restarted)
            self.assertEqual(store.due(restarted), [normalized[0]])
            self.assertEqual(len(store.due(later_retry)), 2)
        rows = {row["stable_id"]: row for row in self.raw()}
        self.assertEqual(rows[first["stable_id"]]["retry_at"], seven.timestamp())
        self.assertEqual(rows[second["stable_id"]]["retry_at"], later_retry.timestamp())
        self.assertTrue(all(row["state"] == "failed" and row["retryable"] == 1 for row in rows.values()))

    def test_night_migration_never_changes_receipts_or_revives_stopped_work(self):
        seven = NOW.replace(hour=4)
        opened = seven - timedelta(hours=5)
        states = ("accepted", "uncertain", "sending", "failed", "cancelled", "expired")
        entries = [reminder(at=seven - timedelta(hours=4), uid=state) for state in states]
        self.legacy_contacts([], [(item, state, opened, False, None) for item, state in zip(entries, states)],
                             now=seven - timedelta(hours=8), version=2)
        before = {row["stable_id"]: row for row in self.raw()}
        with self.store(opened) as store:
            self.assertEqual(store.due(opened), [])
        after = {row["stable_id"]: row for row in self.raw()}
        for entry, state in zip(entries, states):
            old, new = before[entry["stable_id"]], after[entry["stable_id"]]
            self.assertEqual(new["notify_at"], old["notify_at"])
            self.assertEqual(new["payload"], old["payload"])
            self.assertEqual(new["state"], "uncertain" if state == "sending" else state)
        with self.store(seven) as store:
            self.assertEqual(store.due(seven), [])

    def test_policy_only_replan_does_not_revive_stopped_work_but_actual_move_can(self):
        seven = NOW.replace(hour=4)
        replanned = seven - timedelta(hours=1)
        states = ("expired", "cancelled", "failed")
        entries = [reminder(at=seven - timedelta(hours=4), uid=state) for state in states]
        self.legacy_contacts([], [(item, state, replanned, False, None)
                                 for item, state in zip(entries, states)],
                             now=seven - timedelta(hours=8), version=2)
        normalized = [dict(item, notify_at="2026-10-15T07:00:00+03:00") for item in entries]
        with self.store(replanned) as store:
            before = self.raw()
            self.plan(store, normalized, now=replanned)
            self.assertEqual(self.raw(), before)
            self.assertEqual(store.due(seven), [])
            # A true event move remains a new authoritative scheduling decision.
            moved = [dict(item, event_start="2026-10-15T10:00:00+03:00") for item in normalized]
            self.plan(store, moved, now=replanned)
            self.assertEqual(len(store.due(seven)), 3)

    def test_policy_only_replan_preserves_permanent_failure_before_old_due_time(self):
        seven = NOW.replace(hour=4)
        replanned = seven - timedelta(minutes=10)
        item = reminder(at=seven - timedelta(minutes=5))
        self.legacy_contacts([], [(item, "failed", replanned, False, None)],
                             now=seven - timedelta(hours=8), version=2)
        with self.store(replanned) as store:
            before = self.raw()
            self.plan(store, [dict(item, notify_at="2026-10-15T07:00:00+03:00")], now=replanned)
            self.assertEqual(self.raw(), before)
            self.assertEqual(store.summary(replanned)["counts"]["failed"], 1)
            self.assertEqual(store.due(seven), [])

    def test_quiet_hours_block_due_and_claim_on_actual_midnight_clock(self):
        last_minute = NOW.replace(hour=20, minute=59)  # Moscow 23:59.
        midnight = last_minute + timedelta(minutes=1)
        item = reminder(at=last_minute)
        with self.store(last_minute) as store:
            store.activate(last_minute)
            self.plan(store, [item], now=last_minute)
            self.assertEqual(store.due(last_minute), [item])
            # A batch chosen before midnight must not be submitted after it.
            self.assertEqual(store.due(midnight), [])
            self.assertFalse(store.claim(item["stable_id"], now=midnight))
            self.assertEqual(self.raw()[0]["state"], "pending")

    def test_retry_crossing_midnight_is_blocked_then_expires_without_seven_hour_delay(self):
        last_minute = NOW.replace(hour=20, minute=59)
        midnight = last_minute + timedelta(minutes=1)
        seven = midnight + timedelta(hours=7)
        item = reminder(at=last_minute)
        with self.store(last_minute) as store:
            store.activate(last_minute)
            self.plan(store, [item], now=last_minute)
            self.assertTrue(store.claim(item["stable_id"], now=last_minute))
            store.complete(item["stable_id"], "failed", now=last_minute, retryable=True)
        with self.store(midnight) as store:
            store.mark_sync(midnight, 11, {})
            self.assertEqual(store.due(midnight), [])
            self.assertFalse(store.claim(item["stable_id"], now=midnight))
            row = self.raw()[0]
            self.assertEqual(row["notify_at"], last_minute.timestamp())
            self.assertEqual(row["retry_at"], midnight.timestamp())
        with self.store(seven) as store:
            self.assertEqual(store.due(seven), [])
            self.assertEqual(store.summary(seven)["counts"]["expired"], 1)

    def test_night_migration_failure_rolls_back_rows_and_version_two(self):
        seven = NOW.replace(hour=4)
        original = reminder(at=seven - timedelta(hours=4))
        self.legacy_contacts([], [(original, "pending", seven, True, None)],
                             now=seven - timedelta(hours=8), version=2)
        before = self.raw()
        with sqlite3.connect(self.database) as database:
            database.execute("""CREATE TRIGGER reject_night_migration AFTER UPDATE OF notify_at ON reminders
                BEGIN SELECT RAISE(ABORT,'private migration detail'); END""")
        with self.assertRaises(QueueError) as failure, self.store(seven):
            pass
        self.assertNotIn("private migration detail", str(failure.exception))
        self.assertEqual(self.raw(), before)
        with sqlite3.connect(self.database) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 2)

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
