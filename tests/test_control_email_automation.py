"""Synthetic calendar/planner/durable queue integration, with no SMTP sockets."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest import control_email_automation as automation
from calendar_digest.calendar_operation_lock import CalendarBusy, calendar_operation_lock
from calendar_digest.calendar_sync import CachedResource, SyncResult
from calendar_digest.control_email_queue import ControlEmailQueueError, ControlEmailStore, control_email_store
from calendar_digest.readonly_caldav import CalendarConnectionError
from calendar_digest.smtp_delivery import SMTPDeliveryError, SMTPDeliveryUncertain
from test_reminders import event, resource
from test_smtp_delivery import ENVIRONMENT


NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
CONTROL_ENVIRONMENT = dict(ENVIRONMENT, CONTROL_EMAIL_ENABLED='true', MAILRU_2_USERNAME='gpb.control@mail.ru',
                          MAILRU_2_APP_PASSWORD='synthetic-calendar-password',
                          ICLOUD_USERNAME='icloud-owner@example.test', MAILRU_1_USERNAME='first-owner@example.test',
                          MAILRU_3_USERNAME='third-owner@example.test')


def cached(uid='one', *, start='20261019T100000Z', status='CONFIRMED',
           participants=('participant@example.test',), title='Частное поручение'):
    item = resource(event(uid, f'DTSTART:{start}', f'STATUS:{status}', f'SUMMARY:{title}',
                          'LOCATION:Переговорная',
                          *(f'ATTENDEE;PARTSTAT=ACCEPTED:mailto:{value}' for value in participants)))
    return CachedResource(item.source_id, item.calendar_id, item.calendar_name,
                          f'https://calendar.example.invalid/cal/{uid}.ics', '"v1"', item.data)


class ControlEmailAutomationTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary)
        self.enterContext(patch.dict(os.environ, dict(CONTROL_ENVIRONMENT, DIGEST_STATE_DIR=temporary), clear=True))
        self.now = NOW
        self.resources = [cached()]
        self.changed = 1
        self.collect = self.enterContext(patch.object(automation, 'sync_calendars', side_effect=self.sync))
        self.sender = self.enterContext(patch.object(automation, 'submit_control_email', side_effect=self.accept))

    def sync(self, previous, *, source_ids):
        self.assertEqual(source_ids, ('mailru_2',))
        self.assertTrue(all(item.source_id == 'mailru_2' for item in previous))
        return SyncResult(self.resources, 2, self.changed, len(self.resources) if self.changed else 0,
                          max(0, len(previous) - len(self.resources)))

    def accept(self, reminder, settings, *, generated_at, before_data):
        before_data()
        with sqlite3.connect(self.directory / 'control-email' / 'schedule.sqlite3') as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM reminders WHERE state='sending'").fetchone()[0], 1)
        self.assertEqual(reminder['recipient'], 'participant@example.test')
        self.assertEqual(reminder['account_email'], 'gpb.control@mail.ru')
        self.assertEqual(settings.username, ENVIRONMENT['SMTP_USERNAME'])

    def cycle(self):
        return automation.run_control_email_cycle(self.directory, clock=lambda: self.now)

    def prepare(self):
        return automation.prepare_control_email(self.directory, clock=lambda: self.now)

    def summary(self):
        with control_email_store(self.directory, now=self.now) as store:
            return store.summary(self.now)

    def test_prepare_needs_no_smtp_password_and_leaves_other_state_and_activation_untouched(self):
        os.environ.pop('SMTP_PASSWORD')
        for path in ('calendar-state.json', 'push-schedule/schedule.sqlite3'):
            target = self.directory / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('other service state')
        with patch.object(automation.MailSettings, 'from_environment') as settings:
            result = self.prepare()
        settings.assert_not_called()
        self.sender.assert_not_called()
        self.assertFalse(result['sent'])
        self.assertIsNone(result['queue']['activated_at'])
        self.assertEqual(result['queue']['counts']['pending'], 1)
        for path in ('calendar-state.json', 'push-schedule/schedule.sqlite3'):
            self.assertEqual((self.directory / path).read_text(), 'other service state')
        serialized = json.dumps(result, ensure_ascii=False)
        for private in ('participant@example.test', 'Частное поручение', 'example.invalid', 'gpb.control@mail.ru'):
            self.assertNotIn(private, serialized)

    def test_disabled_cycle_reads_no_accounts_smtp_or_calendar_and_creates_no_files(self):
        os.environ['CONTROL_EMAIL_ENABLED'] = 'false'
        with patch.object(automation, 'control_email_accounts') as accounts, \
                patch.object(automation.MailSettings, 'from_environment') as settings, \
                self.assertRaises(automation.ControlEmailCycleError):
            self.cycle()
        accounts.assert_not_called()
        settings.assert_not_called()
        self.collect.assert_not_called()
        self.sender.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_wrong_source_account_is_rejected_before_collection_or_smtp_settings(self):
        os.environ['MAILRU_2_USERNAME'] = 'private-wrong@example.test'
        with patch.object(automation.MailSettings, 'from_environment') as settings, \
                self.assertRaises(automation.ControlEmailCycleError) as caught:
            self.cycle()
        settings.assert_not_called()
        self.collect.assert_not_called()
        self.assertNotIn('private-wrong', str(caught.exception))

    def test_owner_exclusions_include_all_configured_accounts_and_google_default(self):
        source, own = automation.control_email_accounts()
        self.assertEqual(source, 'gpb.control@mail.ru')
        self.assertEqual(own, frozenset({'gpb.control@mail.ru', 'icloud-owner@example.test', 'first-owner@example.test',
                                       'third-owner@example.test', 'sender@example.test', 'owner@example.test',
                                       'pavel.brusser@gmail.com'}))
        os.environ['GOOGLE_CALENDAR_EMAIL'] = 'OTHER-GOOGLE@EXAMPLE.TEST'
        self.assertIn('other-google@example.test', automation.control_email_accounts()[1])

    def test_enabled_control_mail_is_independent_of_push_and_digest_flags_and_credentials(self):
        os.environ.update(PUSH_SCHEDULE_ENABLED='false', DIGEST_SCHEDULE_ENABLED='false')
        self.assertEqual(self.cycle()['accepted'], 1)
        self.assertEqual(self.sender.call_count, 1)

    def test_restart_deduplicates_and_unchanged_source_uses_incremental_cache(self):
        with patch.object(automation, 'plan_control_emails', wraps=automation.plan_control_emails) as planner:
            self.assertEqual(self.cycle()['accepted'], 1)
            self.changed = 0
            self.now += timedelta(seconds=30)
            second = self.cycle()
            self.assertEqual(second['accepted'], 0)
            self.assertEqual(second['queue']['counts']['accepted'], 1)
            planner.assert_called_once()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(len(self.collect.call_args.args[0]), 1)

    def test_no_initial_backlog_from_a_prepared_queue(self):
        self.prepare()
        self.now += timedelta(seconds=1)
        self.changed = 0
        self.assertEqual(self.cycle()['queue']['counts']['expired'], 1)
        self.sender.assert_not_called()

    def test_source_deletion_removes_a_pending_participant_notice(self):
        self.prepare()
        self.resources = []
        result = self.cycle()
        self.assertEqual(result['queue']['counts']['cancelled'], 1)
        self.sender.assert_not_called()

    def test_removing_attendee_cancels_pending_mail(self):
        self.prepare()
        self.resources = [cached(participants=())]
        result = self.cycle()
        self.assertEqual(result['queue']['counts']['cancelled'], 1)
        self.sender.assert_not_called()

    def test_owner_exclusion_change_replans_even_when_calendar_is_unchanged(self):
        self.prepare()
        self.changed = 0
        os.environ['MAILRU_3_USERNAME'] = 'participant@example.test'
        with patch.object(automation, 'plan_control_emails', wraps=automation.plan_control_emails) as planner:
            result = self.cycle()
            planner.assert_called_once()
        self.assertEqual(result['queue']['counts']['cancelled'], 1)
        self.sender.assert_not_called()

    def test_pending_move_reschedules_but_accepted_move_is_not_replayed(self):
        self.prepare()
        self.resources = [cached(start='20261019T120000Z')]
        self.assertFalse(self.cycle()['sent'])
        self.now += timedelta(hours=2)
        self.changed = 0
        self.assertEqual(self.cycle()['accepted'], 1)
        self.resources = [cached(start='20261019T130000Z')]
        self.changed = 1
        self.cycle()
        self.now += timedelta(hours=1)
        self.changed = 0
        self.assertEqual(self.cycle()['accepted'], 0)
        self.assertEqual(self.sender.call_count, 1)

    def test_failed_fresh_read_never_sends_existing_queue_or_changes_cache(self):
        self.prepare()
        self.collect.side_effect = CalendarConnectionError('safe failure')
        with self.assertRaises(CalendarConnectionError):
            self.cycle()
        self.sender.assert_not_called()
        with control_email_store(self.directory, now=self.now) as store:
            self.assertEqual(len(store.load_cache()), 1)
            self.assertEqual(store.summary(self.now)['counts']['pending'], 1)

    def test_invalid_changed_calendar_does_not_replace_previous_snapshot(self):
        self.prepare()
        self.resources = [replace(cached(), data='invalid calendar')]
        with self.assertRaises(Exception):
            self.cycle()
        self.sender.assert_not_called()
        with control_email_store(self.directory, now=self.now) as store:
            self.assertIn('BEGIN:VCALENDAR', store.load_cache()[0]['data'])

    def test_calendar_gate_stops_collection_and_sending_without_waiting(self):
        with calendar_operation_lock(self.directory), self.assertRaises(CalendarBusy):
            self.cycle()
        self.collect.assert_not_called()
        self.sender.assert_not_called()

    def test_lost_smtp_reply_is_terminal_after_restart(self):
        def lost(reminder, settings, *, before_data, **kwargs):
            before_data()
            raise SMTPDeliveryUncertain('private server response')
        self.sender.side_effect = lost
        self.assertEqual(self.cycle()['uncertain'], 1)
        self.changed = 0
        self.assertEqual(self.cycle()['uncertain'], 0)
        self.assertEqual(self.summary()['counts']['uncertain'], 1)
        self.assertEqual(self.sender.call_count, 1)

    def test_explicit_temporary_rejection_retries_after_one_minute(self):
        self.sender.side_effect = SMTPDeliveryError('safe failure', retryable=True)
        self.assertEqual(self.cycle()['failed'], 1)
        self.changed = 0
        self.sender.side_effect = self.accept
        self.now += timedelta(seconds=30)
        self.assertEqual(self.cycle()['accepted'], 0)
        self.now += timedelta(seconds=30)
        self.assertEqual(self.cycle()['accepted'], 1)

    def test_slow_authentication_stops_before_data_when_snapshot_is_stale(self):
        def slow(reminder, settings, *, before_data, **kwargs):
            self.now += timedelta(seconds=121)
            before_data()
            self.fail('Stale inventory must not reach DATA')
        self.sender.side_effect = slow
        result = self.cycle()
        self.assertFalse(result['sent'])
        self.assertTrue(result['refresh_required'])
        self.assertEqual(result['queue']['counts']['pending'], 1)

    def test_slow_read_requires_another_fresh_cycle_before_smtp(self):
        def slow(previous, *, source_ids):
            self.now += timedelta(seconds=121)
            return self.sync(previous, source_ids=source_ids)
        self.collect.side_effect = slow
        self.assertTrue(self.cycle()['refresh_required'])
        self.sender.assert_not_called()
        self.collect.side_effect = self.sync
        self.changed = 0
        self.assertEqual(self.cycle()['accepted'], 1)

    def test_before_seven_does_not_send_then_deferred_past_event_sends_once_at_seven(self):
        self.now = datetime(2026, 10, 19, 3, 59, tzinfo=timezone.utc)
        self.resources = [cached(start='20261019T020000Z')]
        first = self.cycle()
        self.assertFalse(first['sent'])
        self.assertFalse(first['refresh_required'])
        self.assertEqual(first['queue']['next_due_at'], '2026-10-19T04:00:00+00:00')
        self.sender.assert_not_called()
        self.changed = 0
        self.now += timedelta(minutes=1)
        self.assertEqual(self.cycle()['accepted'], 1)
        self.assertEqual(self.cycle()['accepted'], 0)

    def test_authentication_crossing_midnight_cannot_submit_data(self):
        self.now = datetime(2026, 10, 19, 20, 59, 59, tzinfo=timezone.utc)
        self.resources = [cached(start='20261019T215959Z')]
        def midnight(reminder, settings, *, before_data, **kwargs):
            self.now += timedelta(seconds=2)
            before_data()
            self.fail('Night dispatch must not reach DATA')
        self.sender.side_effect = midnight
        result = self.cycle()
        self.assertFalse(result['sent'])
        self.assertTrue(result['refresh_required'])
        self.assertEqual(result['queue']['counts']['pending'], 1)

    def test_receipt_failure_remains_uncertain_after_restart(self):
        complete = ControlEmailStore.complete
        def broken(store, identifier, state, **kwargs):
            if state == 'accepted':
                raise OSError('private failure')
            return complete(store, identifier, state, **kwargs)
        with patch.object(ControlEmailStore, 'complete', new=broken), self.assertRaises(automation.ControlEmailCycleError):
            self.cycle()
        self.changed = 0
        self.assertEqual(self.cycle()['queue']['counts']['uncertain'], 1)
        self.assertEqual(self.sender.call_count, 1)

    def test_batch_limit_is_fifty_or_lower_and_each_remaining_item_needs_fresh_cycle(self):
        self.resources = [cached(str(number)) for number in range(3)]
        with patch.object(automation, 'MAX_BATCH', 2):
            self.assertEqual(self.cycle()['accepted'], 2)
            self.changed = 0
            self.now += timedelta(seconds=30)
            self.assertEqual(self.cycle()['accepted'], 1)
        self.assertEqual(self.collect.call_count, 2)

    def test_status_reports_busy_without_calendar_or_smtp_access(self):
        self.prepare()
        self.collect.reset_mock()
        with control_email_store(self.directory, now=self.now):
            status = automation.automatic_control_email_status(self.directory, now=self.now)
        self.assertEqual(status['queue'], {'busy': True})
        self.assertTrue(status['enabled'])
        self.collect.assert_not_called()
        self.sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
