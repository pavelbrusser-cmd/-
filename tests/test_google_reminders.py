"""Google reminder policies through the planner, preview and durable outbox.

Only synthetic calendars and a fake push transport are used.
"""

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest import push_automation
from calendar_digest.calendar_sync import CachedResource, SyncResult
from calendar_digest.push_automation import notification_text, prepare_push, run_push_cycle
from calendar_digest.push_transport import PushUncertain
from calendar_digest.reminder_preview import render_reminder_preview
from test_push_transport import ENVIRONMENT
from test_reminders import event, plan, resource


def google(*events, **kwargs):
    return resource(*events, source="google", **kwargs)


class GoogleReminderPolicyTests(unittest.TestCase):
    def test_timed_meeting_has_moscow_previous_morning_and_one_hour_notice(self):
        item = google(event("meeting", "DTSTART:20261015T120000Z", "SUMMARY:Meeting"))
        result = plan([item])
        self.assertEqual([(entry.rule, entry.notify_at) for entry in result.reminders], [
            ("d1", "2026-10-14T09:00:00+03:00"),
            ("h1", "2026-10-15T14:00:00+03:00"),
        ])
        self.assertTrue(all(entry.category == "google_events" for entry in result.reminders))
        self.assertEqual(result.summary["by_category"]["google_events"], 2)
        self.assertEqual(result.summary["by_category"]["control"], 0)

    def test_all_day_and_multiday_events_use_start_day_morning(self):
        for end in ("20261016", "20261019"):
            with self.subTest(end=end):
                item = google(event("day", "DTSTART;VALUE=DATE:20261015",
                                    f"DTEND;VALUE=DATE:{end}", "SUMMARY:Trip"))
                entries = plan([item]).reminders
                self.assertEqual([(entry.rule, entry.notify_at) for entry in entries], [
                    ("d1", "2026-10-14T09:00:00+03:00"),
                    ("d0", "2026-10-15T09:00:00+03:00"),
                ])
                self.assertTrue(all(entry.all_day for entry in entries))

    def test_google_periodic_title_is_still_an_ordinary_google_event(self):
        for marker in ("weekly", "monthly", "quarterly", "yearly", "daily", "biweekly"):
            with self.subTest(marker=marker):
                item = google(event("meeting", "DTSTART:20261015T120000Z", f"SUMMARY:Review {marker}"))
                entries = plan([item]).reminders
                self.assertEqual([entry.rule for entry in entries], ["d1", "h1"])
                self.assertEqual({entry.category for entry in entries}, {"google_events"})

    def test_existing_sources_keep_their_rules(self):
        contents = event("meeting", "DTSTART:20261015T120000Z", "SUMMARY:Meeting")
        for source in ("icloud", "mailru_1", "mailru_3"):
            with self.subTest(source=source):
                self.assertEqual(plan([resource(contents, source=source)]).reminders, [])
        self.assertEqual([entry.rule for entry in plan([resource(contents)]).reminders], ["d3", "d1", "h3"])
        periodic = resource(event("control", "DTSTART:20261015T120000Z", "SUMMARY:Review weekly"))
        self.assertEqual({entry.category for entry in plan([periodic]).reminders}, {"periodic_control"})

    def test_google_birthdays_keep_three_morning_notices(self):
        cases = (
            ("Private title", "CATEGORIES:BIRTHDAY", "Synthetic calendar"),
            ("Private title", "", "Дни рождения"),
            ("Birthday Person weekly", "", "Synthetic calendar"),
        )
        for title, extra, calendar in cases:
            with self.subTest(title=title, calendar=calendar):
                item = google(event("birthday", "DTSTART;VALUE=DATE:20261015",
                                    "RRULE:FREQ=YEARLY;COUNT=1", f"SUMMARY:{title}", extra), name=calendar)
                entries = plan([item]).reminders
                self.assertEqual([entry.rule for entry in entries], ["d3", "d1", "d0"])
                self.assertEqual({entry.category for entry in entries}, {"birthdays"})

    def test_all_invitation_states_are_included_but_cancelled_events_are_not(self):
        for participation in ("ACCEPTED", "TENTATIVE", "DECLINED", "NEEDS-ACTION"):
            for status in ("CONFIRMED", "TENTATIVE", "CANCELLED"):
                with self.subTest(participation=participation, status=status):
                    item = google(event("invite", "DTSTART:20261015T120000Z", "SUMMARY:Invitation",
                                        f"STATUS:{status}",
                                        f"ATTENDEE;PARTSTAT={participation}:mailto:self@example.invalid"))
                    result = plan([item])
                    self.assertEqual(len(result.reminders), 0 if status == "CANCELLED" else 2)

    def test_recurrence_exclusions_moves_and_cancellations_use_current_starts(self):
        master = google(event("series", "DTSTART;TZID=Europe/Moscow:20261015T120000",
                              "RRULE:FREQ=DAILY;COUNT=5", "EXDATE;TZID=Europe/Moscow:20261016T120000",
                              "SUMMARY:Meeting"))
        moved = google(event("series", "RECURRENCE-ID;TZID=Europe/Moscow:20261017T120000",
                             "DTSTART;TZID=Europe/Moscow:20261018T150000", "SUMMARY:Moved meeting"))
        cancelled = google(event("series", "RECURRENCE-ID;TZID=Europe/Moscow:20261019T120000",
                                 "DTSTART;TZID=Europe/Moscow:20261019T120000", "STATUS:CANCELLED"))
        entries = plan([master, moved, cancelled]).reminders
        self.assertEqual(len(entries), 6)
        self.assertEqual([entry.notify_at for entry in entries if entry.rule == "h1"], [
            "2026-10-15T11:00:00+03:00", "2026-10-18T11:00:00+03:00", "2026-10-18T14:00:00+03:00",
        ])
        moved_entries = [entry for entry in entries if entry.title == "Moved meeting"]
        self.assertEqual({entry.recurrence_id for entry in moved_entries}, {"2026-10-17T12:00:00+03:00"})
        self.assertEqual({entry.event_start for entry in moved_entries}, {"2026-10-18T15:00:00+03:00"})

    def test_timezone_conversion_uses_moscow_calendar_day(self):
        item = google(event("foreign", "DTSTART;TZID=America/New_York:20261014T230000", "SUMMARY:Meeting"))
        entries = plan([item]).reminders
        self.assertEqual([(entry.rule, entry.notify_at) for entry in entries], [
            ("d1", "2026-10-14T09:00:00+03:00"),
            ("h1", "2026-10-15T07:00:00+03:00"),
        ])
        self.assertEqual({entry.event_start for entry in entries}, {"2026-10-15T06:00:00+03:00"})

    def test_early_event_floor_keeps_event_even_after_it_started(self):
        item = google(event("early", "DTSTART;TZID=Europe/Moscow:20261015T040000", "SUMMARY:Early meeting"))
        entries = plan([item], start="2026-10-15T07:00:00+03:00", end="2026-10-15T07:01:00+03:00").reminders
        entry, = entries
        self.assertEqual(entry.rule, "h1")
        self.assertEqual(entry.notify_at, "2026-10-15T07:00:00+03:00")
        self.assertEqual(entry.event_start, "2026-10-15T04:00:00+03:00")
        self.assertEqual(plan([item], start="2026-10-15T00:00:00+03:00",
                              end="2026-10-15T07:00:00+03:00").reminders, [])

    def test_hourly_notice_crossing_midnight_keeps_previous_evening(self):
        item = google(event("midnight", "DTSTART;TZID=Europe/Moscow:20261015T003000", "SUMMARY:Meeting"))
        entries = plan([item]).reminders
        self.assertEqual([(entry.rule, entry.notify_at) for entry in entries], [
            ("d1", "2026-10-14T09:00:00+03:00"),
            ("h1", "2026-10-14T23:30:00+03:00"),
        ])

    def test_push_and_preview_use_google_labels_and_escape_untrusted_text(self):
        item = google(event("early", "DTSTART;TZID=Europe/Moscow:20261015T040000",
                            "SUMMARY:<b>Private meeting</b>"), name="Google <script>private</script>")
        entries = plan([item], start="2026-10-15T00:00:00+03:00", end="2026-10-16T00:00:00+03:00").reminders
        title, message = notification_text(asdict(entries[0]))
        self.assertEqual(title, "Событие Google · перенесено на 07:00 МСК")
        self.assertIn("15.10.2026 в 04:00 МСК", message)
        self.assertNotIn("Поручение", title)
        html = render_reminder_preview(entries, date(2026, 10, 15), days=1,
                                       generated_at=datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertIn("События Google", html)
        self.assertIn("Google Календарь", html)
        self.assertIn("Перенесено на 07:00 МСК", html)
        self.assertIn("&lt;b&gt;Private meeting&lt;/b&gt;", html)
        self.assertIn("Google &lt;script&gt;private&lt;/script&gt;", html)
        self.assertNotIn("<script>", html)


class GoogleAutomaticReminderTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, dict(ENVIRONMENT, DIGEST_STATE_DIR=str(self.directory),
                                                    PUSH_SCHEDULE_ENABLED="true"), clear=True))
        self.now = datetime(2026, 10, 14, 5, 59, tzinfo=timezone.utc)
        self.changed = 1
        self.resources = [self.cached("meeting", "20261015T120000Z")]
        self.enterContext(patch("calendar_digest.push_automation.sync_calendars", side_effect=self.sync))
        self.sender = self.enterContext(patch("calendar_digest.push_automation.send_notification",
                                             side_effect=self.accept))

    def cached(self, uid, start):
        item = google(event(uid, f"DTSTART:{start}", "SUMMARY:Synthetic meeting"))
        return CachedResource(item.source_id, item.calendar_id, item.calendar_name,
                              f"https://calendar.example.invalid/{uid}.ics", '"v1"', item.data)

    def sync(self, previous):
        return SyncResult(self.resources, 12, self.changed, len(self.resources) if self.changed else 0, 0)

    def accept(self, settings, *, title, message, before_send):
        before_send()
        self.assertTrue(title.startswith("Событие Google · "))
        return {"state": "accepted", "encrypted": True}

    def run_cycle(self):
        return run_push_cycle(self.directory, clock=lambda: self.now)

    def test_day_and_hour_rules_send_once_each_and_survive_policy_upgrade(self):
        with patch("calendar_digest.push_automation.POLICY_VERSION", "2"):
            prepared = prepare_push(self.directory, clock=lambda: self.now)
        self.assertEqual(prepared["queue"]["counts"]["pending"], 2)
        self.sender.assert_not_called()
        self.changed = 0
        self.now += timedelta(minutes=1)
        with patch("calendar_digest.push_automation.plan_reminders", wraps=push_automation.plan_reminders) as planner:
            morning = self.run_cycle()
        planner.assert_called_once()
        self.assertEqual(morning["queue"]["policy_version"], "3")
        self.assertEqual(morning["accepted"], 1)
        self.assertEqual(self.sender.call_args.kwargs["title"], "Событие Google · за 1 день")
        self.assertEqual(self.run_cycle()["accepted"], 0)
        self.now = datetime(2026, 10, 15, 11, tzinfo=timezone.utc)
        hourly = self.run_cycle()
        self.assertEqual(hourly["accepted"], 1)
        self.assertEqual(self.sender.call_args.kwargs["title"], "Событие Google · за 1 час")
        self.assertEqual(self.run_cycle()["accepted"], 0)
        self.assertEqual(self.sender.call_count, 2)

    def test_pending_move_then_delete_updates_google_queue_without_sending(self):
        self.run_cycle()
        self.resources = [self.cached("meeting", "20261016T140000Z")]
        moved = self.run_cycle()
        self.assertEqual(moved["queue"]["counts"]["pending"], 2)
        self.assertEqual(moved["queue"]["next_due_at"], "2026-10-15T06:00:00+00:00")
        self.resources = []
        cancelled = self.run_cycle()
        self.assertEqual(cancelled["queue"]["counts"]["pending"], 0)
        self.assertEqual(cancelled["queue"]["counts"]["cancelled"], 2)
        self.sender.assert_not_called()

    def test_google_early_event_cannot_send_until_seven(self):
        self.now = datetime(2026, 10, 15, 3, 59, tzinfo=timezone.utc)
        self.resources = [self.cached("early", "20261015T010000Z")]
        before = self.run_cycle()
        self.assertEqual(before["queue"]["next_due_at"], "2026-10-15T04:00:00+00:00")
        self.sender.assert_not_called()
        self.changed = 0
        self.now += timedelta(minutes=1)
        self.assertEqual(self.run_cycle()["accepted"], 1)
        self.assertEqual(self.sender.call_args.kwargs["title"], "Событие Google · перенесено на 07:00 МСК")
        self.assertEqual(self.run_cycle()["accepted"], 0)

    def test_unknown_google_delivery_is_never_retried(self):
        self.run_cycle()
        self.now += timedelta(minutes=1)

        def uncertain(settings, *, before_send, **kwargs):
            before_send()
            raise PushUncertain("safe synthetic failure")

        self.sender.side_effect = uncertain
        result = self.run_cycle()
        self.assertEqual(result["uncertain"], 1)
        self.changed = 0
        result = self.run_cycle()
        self.assertEqual(result["queue"]["counts"]["uncertain"], 1)
        self.assertEqual(self.sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
