"""Pure contact birthday planning with synthetic minimal contact records."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from calendar_digest import contact_birthdays
from calendar_digest.contact_birthdays import contact_birthdays_enabled, plan_contact_birthdays
from calendar_digest.contact_identity import canonical_contact_book_id
from calendar_digest.reminders import ReminderError


MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class Contact:
    book_id: str = "book-1"
    uid: str = "private-contact-uid"
    name: str = "Тестовый Контакт"
    month: int = 10
    day: int = 18


def moment(value):
    return datetime.fromisoformat(value).replace(tzinfo=MOSCOW)


class ContactBirthdayTests(unittest.TestCase):
    def plan(self, contacts=None, *, start="2026-10-15T00:00", end="2026-10-22T00:00"):
        return plan_contact_birthdays([Contact()] if contacts is None else contacts,
                                      window_start=moment(start), window_end=moment(end))

    def test_explicit_enable_flag_defaults_off_and_rejects_other_values(self):
        self.assertFalse(contact_birthdays_enabled({}))
        self.assertFalse(contact_birthdays_enabled({"ICLOUD_CONTACT_BIRTHDAYS_ENABLED": "false"}))
        self.assertTrue(contact_birthdays_enabled({"ICLOUD_CONTACT_BIRTHDAYS_ENABLED": "true"}))
        for value in ("TRUE", "1", "yes", " true ", "private-secret-value"):
            with self.assertRaises(ReminderError) as failure:
                contact_birthdays_enabled({"ICLOUD_CONTACT_BIRTHDAYS_ENABLED": value})
            self.assertNotIn("private-secret-value", str(failure.exception))
        with patch.dict(os.environ, {"ICLOUD_CONTACT_BIRTHDAYS_ENABLED": "true"}):
            self.assertTrue(contact_birthdays_enabled())

    def test_three_calendar_day_offsets_at_nine_moscow(self):
        plan = self.plan()
        self.assertEqual([(item.rule, item.notify_at) for item in plan.reminders], [
            ("d3", "2026-10-15T09:00:00+03:00"),
            ("d1", "2026-10-17T09:00:00+03:00"),
            ("d0", "2026-10-18T09:00:00+03:00"),
        ])
        for item in plan.reminders:
            self.assertEqual(item.source_id, "icloud_contacts")
            self.assertEqual(item.calendar_id, "book-1")
            self.assertEqual(item.calendar_name, "Дни рождения · iCloud Contacts")
            self.assertEqual(item.event_start, "2026-10-18")
            self.assertTrue(item.all_day)
            self.assertEqual(item.category, "birthdays")
            self.assertEqual(item.recurrence_id, "birthday:2026")
            self.assertEqual(item.classification_evidence, ("contacts",))
        self.assertEqual(plan.summary["reminders_total"], 3)
        self.assertEqual(plan.summary["by_category"]["birthdays"], 3)
        self.assertEqual(plan.summary["classification_evidence"], {"contacts": 1})

    def test_half_open_window_includes_start_and_excludes_end(self):
        plan = self.plan(start="2026-10-15T09:00", end="2026-10-17T09:00")
        self.assertEqual([item.rule for item in plan.reminders], ["d3"])
        plan = self.plan(start="2026-10-15T09:00:01", end="2026-10-17T09:00:01")
        self.assertEqual([item.rule for item in plan.reminders], ["d1"])

    def test_year_boundary_pre_notification_uses_next_annual_identity(self):
        plan = self.plan([Contact(month=1, day=2)], start="2026-12-29T00:00", end="2027-01-04T00:00")
        self.assertEqual([item.notify_at[:10] for item in plan.reminders],
                         ["2026-12-30", "2027-01-01", "2027-01-02"])
        self.assertEqual({item.recurrence_id for item in plan.reminders}, {"birthday:2027"})

    def test_february_29_has_no_invented_non_leap_observance(self):
        leap = Contact(month=2, day=29)
        plan = self.plan([leap], start="2028-02-25T00:00", end="2028-03-02T00:00")
        self.assertEqual([item.notify_at[:10] for item in plan.reminders],
                         ["2028-02-26", "2028-02-28", "2028-02-29"])
        plan = self.plan([leap], start="2027-02-25T00:00", end="2027-03-02T00:00")
        self.assertEqual(plan.reminders, [])
        self.assertEqual(plan.summary["series_total"], 1)

    def test_date_and_name_correction_preserve_annual_rule_identity(self):
        initial = self.plan().reminders
        edited = self.plan([Contact(name="Изменённое имя", day=19)]).reminders
        self.assertEqual({item.rule: item.stable_id for item in initial},
                         {item.rule: item.stable_id for item in edited})
        self.assertNotEqual(initial[0].notify_at, edited[0].notify_at)
        following = self.plan(start="2027-10-15T00:00", end="2027-10-22T00:00").reminders
        self.assertNotEqual(initial[0].stable_id, following[0].stable_id)

    def test_namesakes_and_separate_addressbooks_remain_separate(self):
        plan = self.plan([Contact(), Contact(uid="other-contact"), Contact(book_id="book-2")])
        self.assertEqual(len(plan.reminders), 9)
        self.assertEqual(len({item.stable_id for item in plan.reminders}), 9)
        self.assertEqual(plan.summary["series_total"], 3)

    def test_book_url_aliases_produce_identical_annual_reminder_ids(self):
        original = "https://contacts.icloud.com/123/carddavhome/book/"
        expected = self.plan([Contact(book_id=original)])
        aliases = (
            "https://p42-contacts.icloud.com/123/carddavhome/book/",
            "https://p43-contacts.icloud.com/123/carddavhome/book",
            "https://p42-contacts.icloud.com/123/carddavhome/%62ook/",
            canonical_contact_book_id(original),
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                actual = self.plan([Contact(book_id=alias)])
                self.assertEqual(actual, expected)
                self.assertEqual({item.stable_id for item in actual.reminders},
                                 {item.stable_id for item in expected.reminders})
        duplicates = self.plan([Contact(book_id=original), Contact(book_id=aliases[0])])
        self.assertEqual(duplicates, expected)
        with self.assertRaises(ReminderError):
            self.plan([Contact(book_id=original), Contact(book_id=aliases[0], day=19)])

    def test_equal_uid_in_different_accounts_or_books_remains_distinct(self):
        books = (
            "https://contacts.icloud.com/123/carddavhome/book/",
            "https://p42-contacts.icloud.com/456/carddavhome/book/",
            "https://p43-contacts.icloud.com/123/carddavhome/other/",
        )
        plan = self.plan([Contact(book_id=book) for book in books])
        self.assertEqual(len(plan.reminders), 9)
        self.assertEqual(len({item.stable_id for item in plan.reminders}), 9)
        self.assertEqual(plan.summary["series_total"], 3)

    def test_identical_duplicate_input_does_not_duplicate_notifications(self):
        plan = self.plan([Contact(), Contact()])
        self.assertEqual(len(plan.reminders), 3)
        self.assertEqual(plan.summary["series_total"], 1)
        with self.assertRaises(ReminderError):
            self.plan([Contact(), Contact(day=19)])

    def test_utc_window_produces_same_moscow_notification_times(self):
        expected = self.plan()
        actual = plan_contact_birthdays([Contact()],
            window_start=moment("2026-10-15T00:00").astimezone(timezone.utc),
            window_end=moment("2026-10-22T00:00").astimezone(timezone.utc))
        self.assertEqual(expected, actual)

    def test_custom_morning_and_empty_input(self):
        plan = plan_contact_birthdays([Contact()], window_start=moment("2026-10-15T00:00"),
                                     window_end=moment("2026-10-22T00:00"), morning_time=time(8, 30))
        self.assertTrue(all(item.notify_at.endswith("08:30:00+03:00") for item in plan.reminders))
        empty = self.plan([])
        self.assertEqual(empty.reminders, [])
        self.assertEqual(empty.summary["series_total"], 0)

    def test_early_custom_morning_is_floored_before_notification_window(self):
        plan = plan_contact_birthdays([Contact()], window_start=moment("2026-10-18T07:00"),
                                     window_end=moment("2026-10-18T07:01"), morning_time=time(5))
        reminder, = plan.reminders
        self.assertEqual(reminder.rule, "d0")
        self.assertEqual(reminder.notify_at, "2026-10-18T07:00:00+03:00")

    def test_invalid_dates_and_incomplete_contacts_fail_without_content(self):
        for contact in (Contact(day=32), Contact(month=2, day=30), Contact(month=True),
                        Contact(uid=""), Contact(name=" "), Contact(book_id="")):
            with self.subTest(contact=contact), self.assertRaises(ReminderError) as failure:
                self.plan([contact])
            self.assertNotIn("Тестовый Контакт", str(failure.exception))
            self.assertNotIn("private-contact-uid", str(failure.exception))

    def test_bounded_window_and_input_limit(self):
        start = moment("2026-10-15T00:00")
        for first, last in ((start.replace(tzinfo=None), start + timedelta(days=1)),
                            (start, start), (start, start + timedelta(days=32))):
            with self.assertRaises(ReminderError):
                plan_contact_birthdays([], window_start=first, window_end=last)
        with patch.object(contact_birthdays, "MAX_CONTACT_BIRTHDAYS", 1):
            with self.assertRaises(ReminderError):
                self.plan([Contact(), Contact(uid="other")])

    def test_summary_contains_no_contact_identifiers_or_names(self):
        summary = json.dumps(self.plan().summary, ensure_ascii=False)
        for private in (Contact().name, Contact().uid, Contact().book_id):
            self.assertNotIn(private, summary)


if __name__ == "__main__":
    unittest.main()
