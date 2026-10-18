"""Optional Google snapshots share the complete DAV collection contract."""

from dataclasses import replace
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from calendar_digest import calendar_collection, calendar_sync
from calendar_digest.calendar_collection import CollectionLimitError
from calendar_digest.readonly_caldav import CalendarConnectionError
import test_calendar_collection as collection_fixtures
import test_calendar_sync as sync_fixtures


ACCOUNT = "fixture-google@example.invalid"


def google_resource(event_id="master", data=collection_fixtures.RAW_SERIES, *, etag='"v1"'):
    calendar_hash = hashlib.sha256(ACCOUNT.encode()).hexdigest()
    event_hash = hashlib.sha256(event_id.encode()).hexdigest()
    return SimpleNamespace(
        calendar_id=ACCOUNT, calendar_name="Google fixture", account_email=ACCOUNT,
        href=f"https://www.googleapis.com/calendar/v3/calendars/{calendar_hash}/events/{event_hash}.ics",
        etag=etag, data=data,
    )


def snapshot(*resources):
    return SimpleNamespace(calendar_count=1, resources=list(resources))


class GoogleCollectionTests(unittest.TestCase):
    def setUp(self):
        # Reuse the real CalDAV parser/transport fixture without subclassing
        # and rerunning its unrelated tests. It clears all environment values.
        self.dav = collection_fixtures.CalendarCollectionTests()
        self.dav.setUp()
        self.addCleanup(self.dav.doCleanups)
        self.enabled = self.enterContext(patch.object(calendar_collection, "google_enabled", return_value=True))
        self.google = self.enterContext(patch("calendar_digest.google_calendar.collect_google"))
        self.google.return_value = snapshot(google_resource())

    def test_enabled_google_preserves_account_and_complete_recurrence_set(self):
        result = calendar_collection.collect_all()
        self.assertEqual((result.calendar_count, len(result.resources)), (5, 3))
        resource = result.resources[-1]
        self.assertEqual((resource.source_id, resource.calendar_id, resource.account_email),
                         ("google", ACCOUNT, ACCOUNT))
        self.assertEqual(resource.data, collection_fixtures.RAW_SERIES)
        self.assertEqual(self.dav.reported_sources, ["icloud", "mailru_1", "mailru_2", "mailru_3"])
        self.assertNotIn("Fictional master", repr(result))

    def test_disabled_google_does_not_read_credentials_or_contact_google(self):
        self.enabled.return_value = False
        result = calendar_collection.collect_all()
        self.assertEqual((result.calendar_count, len(result.resources)), (4, 2))
        self.google.assert_not_called()

    def test_google_failure_aborts_complete_collection_without_private_error(self):
        self.google.side_effect = RuntimeError("private-token private-calendar-response")
        with self.assertRaises(CalendarConnectionError) as error:
            calendar_collection.collect_all()
        self.assertIn("google", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(len(self.dav.reported_sources), 4)

    def test_unreadable_google_activation_does_not_report_mailru_failure(self):
        self.enabled.side_effect = RuntimeError("private-encrypted-config")
        with self.assertRaises(CalendarConnectionError) as error:
            calendar_collection.collect_all()
        self.assertIn("google", str(error.exception))
        self.assertNotIn("mailru", str(error.exception))
        self.assertNotIn("private", str(error.exception))
        self.google.assert_not_called()

    def test_resource_and_byte_budgets_cover_dav_and_google_together(self):
        # Each provider is individually below these limits; the union is not.
        base_bytes = sum(len(value.encode()) for value in self.dav.raw_records.values())
        for constant, limit in (("MAX_RESOURCES", 2), ("MAX_TOTAL_BYTES", base_bytes)):
            with self.subTest(constant=constant), patch.object(calendar_collection, constant, limit):
                with self.assertRaises(CollectionLimitError):
                    calendar_collection.collect_all()


class GoogleSyncTests(unittest.TestCase):
    def setUp(self):
        self.dav = sync_fixtures.CalendarSyncTests()
        self.dav.setUp()
        self.addCleanup(self.dav.doCleanups)
        self.enabled = self.enterContext(patch.object(calendar_sync, "google_enabled", return_value=True))
        self.google = self.enterContext(patch("calendar_digest.google_calendar.collect_google"))
        self.google.return_value = snapshot(
            google_resource(), google_resource("exception", collection_fixtures.RAW_EXCEPTION),
        )

    def test_unchanged_complete_google_snapshot_does_not_trigger_replanning(self):
        first = calendar_sync.sync_calendars([])
        self.assertEqual((first.calendar_count, first.changed, first.fetched, first.deleted), (5, 4, 4, 0))
        second = calendar_sync.sync_calendars(first.resources)
        self.assertEqual(second.resources, first.resources)
        self.assertEqual((second.changed, second.fetched, second.deleted), (0, 2, 0))
        converted = calendar_sync.to_calendar_resources(second.resources)
        self.assertEqual(converted[-1].source_id, "google")
        self.assertEqual(converted[-1].data, collection_fixtures.RAW_EXCEPTION)

    def test_changed_google_resource_is_counted_with_new_etag_and_body(self):
        first = calendar_sync.sync_calendars([])
        self.google.return_value.resources[0] = google_resource(
            data=collection_fixtures.RAW_SERIES.replace("Fictional master", "Updated fixture"), etag='"v2"',
        )
        second = calendar_sync.sync_calendars(first.resources)
        self.assertEqual((second.changed, second.fetched, second.deleted), (1, 2, 0))
        self.assertIn("Updated fixture", second.resources[-2].data)

    def test_deleted_google_resource_is_removed_only_after_complete_snapshot(self):
        first = calendar_sync.sync_calendars([])
        self.google.return_value.resources.pop()
        second = calendar_sync.sync_calendars(first.resources)
        self.assertEqual((second.changed, second.fetched, second.deleted), (1, 1, 1))
        self.assertEqual(len(second.resources), 3)

    def test_google_rename_is_a_change_even_with_same_event_etags(self):
        first = calendar_sync.sync_calendars([])
        for resource in self.google.return_value.resources:
            resource.calendar_name = "Renamed Google calendar"
        second = calendar_sync.sync_calendars(first.resources)
        self.assertEqual(second.changed, 2)
        self.assertEqual(second.deleted, 0)

    def test_explicit_disable_removes_only_google_from_existing_cache(self):
        first = calendar_sync.sync_calendars([])
        self.google.reset_mock()
        self.enabled.return_value = False
        second = calendar_sync.sync_calendars(first.resources)
        self.assertEqual((second.calendar_count, second.changed, second.fetched, second.deleted), (4, 2, 0, 2))
        self.assertTrue(all(item.source_id != "google" for item in second.resources))
        self.google.assert_not_called()

    def test_google_api_failure_does_not_return_or_modify_partial_cache(self):
        first = calendar_sync.sync_calendars([])
        saved = list(first.resources)
        self.google.side_effect = RuntimeError("private-token private-calendar-response")
        with self.assertRaises(CalendarConnectionError) as error:
            calendar_sync.sync_calendars(first.resources)
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(first.resources, saved)

    def test_old_google_identity_and_body_are_validated_before_network_reads(self):
        first = calendar_sync.sync_calendars([])
        original = first.resources[-1]
        invalid = [
            replace(original, href=original.href.replace("www.googleapis.com", "evil.example")),
            replace(original, href=original.href.replace("https:", "http:")),
            replace(original, href=original.href + "?token=private"),
            replace(original, calendar_id="other@example.invalid"),
            replace(original, data="private-invalid-calendar"),
            replace(original, data="BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
                    "DTSTART:20261015T090000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"),
            replace(original, data="BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VTODO\r\n"
                    "UID:fixture\r\nEND:VTODO\r\nEND:VCALENDAR\r\n"),
        ]
        for resource in invalid:
            with self.subTest(field_changed=resource != original):
                self.google.reset_mock()
                self.dav.inventories.clear()
                with self.assertRaises(CalendarConnectionError) as error:
                    calendar_sync.sync_calendars([*first.resources[:-1], resource])
                self.assertNotIn("private", str(error.exception))
                self.assertEqual(self.dav.inventories, [])
                self.google.assert_not_called()

    def test_duplicate_google_resources_never_create_partial_cache(self):
        resource = self.google.return_value.resources[0]
        self.google.return_value.resources = [resource, resource]
        with self.assertRaises(CalendarConnectionError):
            calendar_sync.sync_calendars([])

    def test_global_limits_include_google_and_prior_google_cache(self):
        first = calendar_sync.sync_calendars([])
        base_bytes = sum(len(item.data.encode()) for item in first.resources if item.source_id != "google")
        for previous in ([], first.resources):
            for constant, limit in (("MAX_RESOURCES", 3), ("MAX_TOTAL_BYTES", base_bytes)):
                with self.subTest(cached=bool(previous), constant=constant), \
                        patch.object(calendar_sync, constant, limit):
                    with self.assertRaises(CalendarConnectionError):
                        calendar_sync.sync_calendars(previous)


if __name__ == "__main__":
    unittest.main()
