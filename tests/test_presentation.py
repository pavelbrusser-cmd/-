"""Presentation choices from synthetic events, independently of calendar I/O."""

from dataclasses import asdict, replace
from types import SimpleNamespace
import unittest

from calendar_digest.presentation import CATEGORY_LABELS, actions_for, classify_event
from calendar_digest.selection import DigestEvent


def sample(**changes):
    event = DigestEvent(
        source_id="synthetic", calendar_id="example", calendar_name="Личный календарь",
        uid="example-event", recurrence_id=None, title="Общее событие",
        start="2026-10-14T12:00:00+03:00", end="2026-10-14T13:00:00+03:00",
        all_day=False, event_status="UNSPECIFIED", participation="UNKNOWN",
        recurring=False, multi_day=False,
    )
    return replace(event, **changes)


class PresentationTests(unittest.TestCase):
    def test_explicit_title_categories_and_russian_labels(self):
        cases = {
            "День рождения — тестовый пример": "birthdays",
            "Birthday: synthetic example": "birthdays",
            "Example's birthday": "birthdays",
            "Новый год": "holidays",
            "Christmas": "holidays",
            "Праздник: учебный пример": "holidays",
            "Встреча по проекту": "meetings",
            "Совещание по плану": "meetings",
            "Meeting: synthetic example": "meetings",
        }
        for title, category in cases.items():
            with self.subTest(title=title):
                self.assertEqual(classify_event(sample(title=title)), category)
        self.assertEqual(CATEGORY_LABELS["birthdays"], "Дни рождения")
        self.assertEqual(CATEGORY_LABELS["all_day"], "На весь день")

    def test_possessive_birthday_with_complete_numeric_ordinal(self):
        for apostrophe in ("'", "’"):
            for ordinal in ("1st", "2nd", "3rd", "11th", "12th", "13th", "21st", "22nd", "23rd", "46th"):
                title = f"Example Person{apostrophe}s {ordinal} Birthday"
                with self.subTest(title=title):
                    self.assertEqual(classify_event(sample(title=title)), "birthdays")

    def test_neutral_titles_do_not_imply_birthday_holiday_or_meeting(self):
        for title in (
            "Учебное событие", "ДР — служебное сокращение",
            "Подготовить подарок ко дню рождения", "Новый год — подготовка отчёта",
            "Meetinghouse maintenance", "Подготовить материалы для встречи",
            "Example Person’s 46t...", "Example Person’s 46th", "Example Person 46th",
            "46th", "Example Person’s 46th birthdayish", "Example Person’s 46th meeting",
            "Example Person’s 46t birthday", "Example Person’s 46 birthday",
        ):
            with self.subTest(title=title):
                self.assertEqual(classify_event(sample(title=title)), "events")
        self.assertEqual(classify_event(sample(participation="ACCEPTED")), "events")

    def test_all_day_fallback_does_not_guess_semantic_category(self):
        self.assertEqual(classify_event(sample(all_day=True)), "all_day")
        self.assertEqual(classify_event(sample(all_day=True, title="День рождения — пример")), "birthdays")
        self.assertEqual(classify_event(sample(all_day=True, title="Рождество")), "holidays")

    def test_metadata_wins_but_conflicting_metadata_stays_neutral(self):
        event = asdict(sample(title="Встреча по плану"))
        self.assertEqual(classify_event(SimpleNamespace(**event, categories=["Birthday"])), "birthdays")
        self.assertEqual(classify_event(SimpleNamespace(**event, category="holidays")), "holidays")
        self.assertEqual(classify_event(SimpleNamespace(**event, categories=["Birthday", "Meeting"])), "events")
        self.assertEqual(classify_event(SimpleNamespace(**event, categories=["Unrecognized"])), "meetings")

    def test_explicit_calendar_category_and_conflicting_title(self):
        self.assertEqual(classify_event(sample(calendar_name="Дни рождения")), "birthdays")
        self.assertEqual(classify_event(sample(calendar_name="Праздники")), "holidays")
        self.assertEqual(classify_event(sample(calendar_name="Дни рождения", title="Встреча по плану")), "events")

    def test_cancelled_events_never_have_actions(self):
        for participation in ("ACCEPTED", "TENTATIVE", "NEEDS-ACTION", "DECLINED", "UNKNOWN"):
            with self.subTest(participation=participation):
                self.assertEqual(actions_for(sample(event_status="CANCELLED", participation=participation)), ())

    def test_accepted_invitation_gets_reschedule_and_cancel(self):
        for status in ("CONFIRMED", "TENTATIVE", "UNSPECIFIED"):
            with self.subTest(status=status):
                self.assertEqual(actions_for(sample(event_status=status, participation="ACCEPTED")), ("reschedule", "cancel"))

    def test_pending_tentative_and_declined_can_confirm_regardless_of_event_status(self):
        for participation in ("TENTATIVE", "NEEDS-ACTION", "DECLINED"):
            for status in ("CONFIRMED", "TENTATIVE", "UNSPECIFIED"):
                with self.subTest(participation=participation, status=status):
                    self.assertEqual(actions_for(sample(event_status=status, participation=participation)), ("confirm",))

    def test_unknown_participation_uses_only_explicit_event_status(self):
        self.assertEqual(actions_for(sample(event_status="CONFIRMED")), ("reschedule", "cancel"))
        self.assertEqual(actions_for(sample(event_status="TENTATIVE")), ("confirm",))
        self.assertEqual(actions_for(sample()), ())
        self.assertEqual(actions_for(sample(all_day=True)), ())
        self.assertEqual(actions_for(sample(participation="DELEGATED", event_status="CONFIRMED")), ())
        self.assertEqual(actions_for(sample(participation="OTHER", event_status="CONFIRMED")), ())


if __name__ == "__main__":
    unittest.main()
