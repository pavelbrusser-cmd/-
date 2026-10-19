"""Source labels survive current selection and pre-label stored records."""

from dataclasses import asdict
from datetime import date
import unittest

from calendar_digest.action_store import StoreError
from calendar_digest.cancellation_archive import _event
from calendar_digest.selection import DigestEvent, select_day, select_day_with_changes
from calendar_digest.series_changes import parse_series_changes
from test_selection import event, resource


DAY = date(2026, 10, 14)


class SourceMetadataTests(unittest.TestCase):
    def test_account_comes_from_resource_and_survives_saved_event(self):
        selected, = select_day([resource(event("single", "DTSTART:20261014T090000Z",
            "DTEND:20261014T100000Z", "SUMMARY:Example"), account="owner@example.invalid")], DAY)
        self.assertEqual(selected.account_email, "owner@example.invalid")
        payload = asdict(selected)
        self.assertEqual(DigestEvent(**payload), selected)
        del payload["account_email"]
        self.assertEqual(DigestEvent(**payload).account_email, "")
        payload["event_status"] = "CANCELLED"
        self.assertEqual(_event(payload).account_email, "")
        payload["account_email"] = 5
        with self.assertRaises(StoreError):
            _event(payload)

    def test_series_notice_uses_current_account_and_reads_legacy_payload(self):
        old = resource(event("series", "DTSTART:20261013T090000Z", "DTEND:20261013T100000Z",
                             "RRULE:FREQ=DAILY;COUNT=4"), account="old@example.invalid")
        current = resource(event("series", "DTSTART:20261013T110000Z", "DTEND:20261013T120000Z",
                                 "RRULE:FREQ=DAILY;COUNT=4"), account="owner@example.invalid")
        result = select_day_with_changes([current], DAY, series_previous_resources=[old])
        notice, = result.series_changes
        self.assertEqual(notice.account_email, "owner@example.invalid")
        payload = asdict(notice)
        self.assertEqual(parse_series_changes([payload]), [notice])
        del payload["account_email"]
        self.assertEqual(parse_series_changes([payload])[0].account_email, "")
        payload["account_email"] = ["owner@example.invalid"]
        with self.assertRaises(ValueError):
            parse_series_changes([payload])


if __name__ == "__main__":
    unittest.main()
