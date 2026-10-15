"""Real planner/cache/outbox workflows with fake CalDAV and push boundaries."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest import push_automation
from calendar_digest.calendar_operation_lock import CalendarBusy, calendar_operation_lock
from calendar_digest.calendar_sync import CachedResource, SyncResult
from calendar_digest.contact_birthdays_source import BirthdayCollection, ContactBirthday
from calendar_digest.push_automation import prepare_push, run_push_cycle, notification_text, PushCycleError
from calendar_digest.push_queue import QueueError, QueueStore, schedule_store
from calendar_digest.push_transport import PushError, PushUncertain
from calendar_digest.readonly_caldav import CalendarConnectionError
from test_push_transport import ENVIRONMENT
from test_reminders import event, resource


NOW = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)


def cached(uid='one', *, start='20261015T120000Z', status='CONFIRMED'):
    item = resource(event(uid, f'DTSTART:{start}', f'STATUS:{status}', 'SUMMARY:Частное поручение'))
    return CachedResource(item.source_id, item.calendar_id, item.calendar_name,
                          f'https://calendar.example.invalid/cal/{uid}.ics', '"v1"', item.data)


class AutomaticPushTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary)
        self.enterContext(patch.dict(os.environ, dict(ENVIRONMENT, DIGEST_STATE_DIR=temporary,
                                                    PUSH_SCHEDULE_ENABLED='true'), clear=True))
        self.now = NOW
        self.resources = [cached()]
        self.changed = 1
        self.collect = self.enterContext(patch('calendar_digest.push_automation.sync_calendars',
                                              side_effect=self.sync))
        self.sender = self.enterContext(patch('calendar_digest.push_automation.send_notification',
                                             side_effect=self.accept))

    def sync(self, previous):
        return SyncResult(self.resources, 11, self.changed, len(self.resources) if self.changed else 0,
                          max(0, len(previous) - len(self.resources)))

    def accept(self, settings, *, title, message, before_send):
        before_send()
        database = self.directory / 'push-schedule' / 'schedule.sqlite3'
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM reminders WHERE state='sending'").fetchone()[0], 1)
        self.assertIn('Поручение', title)
        self.assertIn('Частное поручение', message)
        return {'state': 'accepted', 'encrypted': True}

    def run_cycle(self):
        return run_push_cycle(self.directory, clock=lambda: self.now)

    def prepare(self):
        return prepare_push(self.directory, clock=lambda: self.now)

    def summary(self):
        with schedule_store(self.directory, now=self.now) as store:
            return store.summary(self.now)

    def test_prepare_needs_no_push_keys_and_does_not_activate_or_change_email_state(self):
        for name in ENVIRONMENT:
            os.environ.pop(name)
        email = self.directory / 'calendar-state.json'
        email.write_text('existing email baseline')
        result = self.prepare()
        self.assertFalse(result['sent'])
        self.assertIsNone(result['queue']['activated_at'])
        self.assertEqual(result['queue']['counts']['pending'], 1)
        self.assertEqual(email.read_text(), 'existing email baseline')
        self.sender.assert_not_called()
        for private in ('Частное поручение', 'example.invalid', 'synthetic'):
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False))

    def test_disabled_cycle_does_not_read_keys_or_calendars(self):
        os.environ['PUSH_SCHEDULE_ENABLED'] = 'false'
        with patch('calendar_digest.push_automation.PushoverSettings.from_environment') as settings:
            with self.assertRaises(PushCycleError):
                self.run_cycle()
        settings.assert_not_called()
        self.collect.assert_not_called()
        self.sender.assert_not_called()

    def birthday_source(self):
        self.resources = []
        self.now = datetime(2026, 10, 15, 5, 59, tzinfo=timezone.utc)
        os.environ['ICLOUD_CONTACT_BIRTHDAYS_ENABLED'] = 'true'
        contacts = [ContactBirthday('private-book', 'private-contact-uid', 'Private contact', 10, 18)]
        source = self.enterContext(patch('calendar_digest.push_automation.collect_contact_birthdays',
                                        return_value=BirthdayCollection(contacts, 1)))

        def accepted(settings, *, title, message, before_send):
            before_send()
            self.assertIn('День рождения', title)
            self.assertIn('Private contact', message)
            return {'state': 'accepted', 'encrypted': True}

        self.sender.side_effect = accepted
        return source

    def test_contacts_prepare_send_once_and_corrected_date_does_not_repeat_annual_rule(self):
        source = self.birthday_source()
        prepared = self.prepare()
        self.assertEqual(prepared['queue']['contacts'], 1)
        self.assertTrue(prepared['queue']['contacts_enabled'])
        self.assertEqual(prepared['queue']['counts']['pending'], 3)
        self.sender.assert_not_called()
        self.now += timedelta(minutes=1)
        self.changed = 0
        self.assertEqual(self.run_cycle()['accepted'], 1)
        source.return_value = BirthdayCollection([ContactBirthday('private-book', 'private-contact-uid', 'Private contact', 10, 19)], 1)
        self.assertEqual(self.run_cycle()['accepted'], 0)
        self.now += timedelta(days=1)
        result = self.run_cycle()
        self.assertEqual(result['accepted'], 0)
        self.assertEqual(result['queue']['counts']['accepted'], 1)
        self.assertEqual(result['queue']['counts']['pending'], 2)
        self.assertEqual(self.sender.call_count, 1)
        source.return_value = BirthdayCollection([], 1)
        result = self.run_cycle()
        self.assertEqual(result['queue']['contacts'], 0)
        self.assertEqual(result['queue']['counts']['cancelled'], 2)

    def test_failed_contacts_read_keeps_snapshot_and_never_sends_existing_queue(self):
        source = self.birthday_source()
        self.prepare()
        self.now += timedelta(minutes=1)
        source.side_effect = CalendarConnectionError('safe failure')
        self.collect.reset_mock()
        with self.assertRaises(QueueError):
            self.run_cycle()
        self.collect.assert_not_called()
        self.sender.assert_not_called()
        with schedule_store(self.directory, now=self.now) as store:
            self.assertEqual(len(store.load_contacts()), 1)
            self.assertEqual(store.summary(self.now)['counts']['pending'], 3)

    def test_failed_calendar_read_does_not_commit_changed_contacts(self):
        source = self.birthday_source()
        self.prepare()
        source.return_value = BirthdayCollection([], 1)
        self.collect.side_effect = CalendarConnectionError('safe failure')
        with self.assertRaises(QueueError):
            self.run_cycle()
        with schedule_store(self.directory, now=self.now) as store:
            self.assertEqual(len(store.load_contacts()), 1)
        self.sender.assert_not_called()

    def test_disabling_contacts_removes_pending_birthdays_without_reading_contacts(self):
        source = self.birthday_source()
        self.prepare()
        os.environ['ICLOUD_CONTACT_BIRTHDAYS_ENABLED'] = 'false'
        source.reset_mock()
        self.changed = 0
        result = self.run_cycle()
        source.assert_not_called()
        self.sender.assert_not_called()
        self.assertFalse(result['queue']['contacts_enabled'])
        self.assertEqual(result['queue']['contacts'], 0)
        self.assertEqual(result['queue']['counts']['cancelled'], 3)

    def test_due_notification_is_checkpointed_once_and_unchanged_sync_skips_planning(self):
        with patch('calendar_digest.push_automation.plan_reminders', wraps=push_automation.plan_reminders) as planner:
            first = self.run_cycle()
            self.assertEqual(first['accepted'], 1)
            self.assertTrue(first['sent'])
            self.changed = 0
            self.now += timedelta(minutes=1)
            second = self.run_cycle()
            self.assertEqual(second['accepted'], 0)
            self.assertEqual(second['queue']['counts']['accepted'], 1)
            planner.assert_called_once()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.collect.call_count, 2)
        self.assertEqual(len(self.collect.call_args.args[0]), 1)

    def test_initial_activation_does_not_send_a_past_prepared_notice(self):
        self.prepare()
        self.now += timedelta(minutes=1)
        self.changed = 0
        result = self.run_cycle()
        self.assertEqual(result['queue']['counts']['expired'], 1)
        self.sender.assert_not_called()

    def test_pending_move_reschedules_then_sends_at_new_time(self):
        self.prepare()
        self.resources = [cached(start='20261015T140000Z')]
        result = self.run_cycle()
        self.assertFalse(result['sent'])
        self.assertEqual(result['queue']['next_due_at'], (NOW + timedelta(hours=2)).isoformat())
        self.changed = 0
        self.now += timedelta(hours=2)
        self.assertEqual(self.run_cycle()['accepted'], 1)

    def test_accepted_move_does_not_repeat_the_same_rule(self):
        self.run_cycle()
        self.resources = [cached(start='20261015T140000Z')]
        self.run_cycle()
        self.changed = 0
        self.now += timedelta(hours=2)
        self.assertEqual(self.run_cycle()['accepted'], 0)
        self.assertEqual(self.sender.call_count, 1)

    def test_deleted_and_cancelled_pending_events_are_not_sent(self):
        for entries in ([], [cached(status='CANCELLED')]):
            with self.subTest(deletion=not entries):
                with tempfile.TemporaryDirectory() as temporary:
                    self.directory = Path(temporary)
                    os.environ['DIGEST_STATE_DIR'] = temporary
                    self.resources = [cached()]
                    self.prepare()
                    self.resources = entries
                    result = self.run_cycle()
                    self.assertEqual(result['queue']['counts']['cancelled'], 1)
        self.sender.assert_not_called()

    def test_failed_sync_never_sends_stale_queue_or_replaces_cache(self):
        self.prepare()
        with patch('calendar_digest.push_automation.sync_calendars', side_effect=CalendarConnectionError('safe')):
            with self.assertRaises((QueueError, CalendarConnectionError)):
                self.run_cycle()
        with schedule_store(self.directory, now=self.now) as store:
            self.assertEqual(len(store.load_cache()), 1)
            self.assertEqual(store.summary(self.now)['counts']['pending'], 1)
        self.sender.assert_not_called()

    def test_failed_planning_does_not_commit_changed_cache(self):
        self.prepare()
        self.resources = [replace(cached(), data='broken calendar content')]
        with self.assertRaises(Exception):
            self.run_cycle()
        with schedule_store(self.directory, now=self.now) as store:
            self.assertIn('BEGIN:VCALENDAR', store.load_cache()[0]['data'])
        self.sender.assert_not_called()

    def test_shared_calendar_gate_blocks_collection_and_delivery(self):
        with calendar_operation_lock(self.directory):
            with self.assertRaises(CalendarBusy):
                self.run_cycle()
        self.collect.assert_not_called()
        self.sender.assert_not_called()

    def test_lost_reply_is_terminal_after_restart_and_refresh(self):
        def lost(settings, *, before_send, **kwargs):
            before_send()
            raise PushUncertain('private reply must stay hidden')
        self.sender.side_effect = lost
        first = self.run_cycle()
        self.assertEqual(first['uncertain'], 1)
        self.changed = 0
        self.assertEqual(self.run_cycle()['uncertain'], 0)
        self.assertEqual(self.summary()['counts']['uncertain'], 1)
        self.assertEqual(self.sender.call_count, 1)

    def test_known_connection_failure_retries_after_one_minute(self):
        self.sender.side_effect = PushError('safe', retryable=True)
        self.assertEqual(self.run_cycle()['failed'], 1)
        self.changed = 0
        self.sender.side_effect = self.accept
        self.now += timedelta(seconds=30)
        self.assertEqual(self.run_cycle()['accepted'], 0)
        self.now += timedelta(seconds=30)
        self.assertEqual(self.run_cycle()['accepted'], 1)

    def test_stale_sync_after_long_device_validation_prevents_submission(self):
        def slow_validation(settings, *, before_send, **kwargs):
            self.now += timedelta(seconds=121)
            before_send()
            self.fail('A stale snapshot must not reach HTTP submission')
        self.sender.side_effect = slow_validation
        result = self.run_cycle()
        self.assertTrue(result['refresh_required'])
        self.assertFalse(result['sent'])
        self.assertEqual(result['queue']['counts']['pending'], 1)

    def test_slow_complete_sync_waits_for_a_fresh_inventory_before_delivery(self):
        def slow(previous):
            self.now += timedelta(seconds=121)
            return self.sync(previous)
        self.collect.side_effect = slow
        result = self.run_cycle()
        self.assertTrue(result['refresh_required'])
        self.assertTrue(result['queue']['sync_recent'])
        self.assertFalse(result['queue']['sync_fresh_for_delivery'])
        self.sender.assert_not_called()
        self.collect.side_effect = self.sync
        self.changed = 0
        self.now += timedelta(seconds=30)
        self.assertEqual(self.run_cycle()['accepted'], 1)

    def test_receipt_failure_leaves_unknown_delivery_consumed(self):
        complete = QueueStore.complete
        def broken(store, identifier, state, **kwargs):
            if state == 'accepted':
                raise OSError('private failure')
            return complete(store, identifier, state, **kwargs)
        with patch.object(QueueStore, 'complete', new=broken), self.assertRaises(Exception):
            self.run_cycle()
        self.assertEqual(self.summary()['counts']['uncertain'], 1)
        self.changed = 0
        self.assertEqual(self.run_cycle()['accepted'], 0)
        self.assertEqual(self.sender.call_count, 1)

    def test_policy_change_and_horizon_rollover_force_replanning(self):
        self.prepare()
        self.changed = 0
        with patch('calendar_digest.push_automation.plan_reminders', wraps=push_automation.plan_reminders) as planner:
            with patch('calendar_digest.push_automation.POLICY_VERSION', '2'):
                self.prepare()
                self.assertEqual(self.summary()['policy_version'], '2')
                self.now += timedelta(days=1, seconds=1)
                self.prepare()
            self.assertEqual(planner.call_count, 2)

    def test_batch_limit_leaves_remaining_due_items_for_next_fresh_cycle(self):
        self.resources = [cached(str(number)) for number in range(3)]
        with patch('calendar_digest.push_automation.MAX_BATCH', 2):
            self.assertEqual(self.run_cycle()['accepted'], 2)
            self.changed = 0
            self.now += timedelta(seconds=30)
            self.assertEqual(self.run_cycle()['accepted'], 1)

    def test_late_retry_expires_after_fifteen_minutes(self):
        self.sender.side_effect = PushError('safe', retryable=True)
        self.run_cycle()
        self.changed = 0
        self.now += timedelta(minutes=15, seconds=1)
        result = self.run_cycle()
        self.assertEqual(result['queue']['counts']['expired'], 1)
        self.assertEqual(self.sender.call_count, 1)


class PushFormattingTests(unittest.TestCase):
    def test_birthday_text_is_plain_bounded_and_has_calendar_date(self):
        title, message = notification_text({'category':'birthdays','rule':'d1', 'all_day':True,
            'event_start':'2026-10-16', 'title':'<b>Имя</b>\x00\n' + 'Я' * 2000,
            'calendar_name':'График'})
        self.assertEqual(title, 'День рождения · за 1 день')
        self.assertIn('16.10.2026 · весь день', message)
        self.assertNotIn('\x00', message)
        self.assertLessEqual(len(message), 1024)
        self.assertIn('<b>Имя</b>', message)  # Pushover receives plain text, no html flag.


if __name__ == '__main__':
    unittest.main()
