"""Exercise operator push commands without any network or real settings."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from calendar_digest.calendar_collection import Collection
from calendar_digest.calendar_sync import SyncResult
from calendar_digest.calendar_operation_lock import calendar_operation_lock
from calendar_digest.contact_birthdays_source import BirthdayCollection, ContactBirthday
from calendar_digest.cli import main
from test_preview import resource
from test_push_transport import Connection, Response, ENVIRONMENT, SEND_PATH, VALIDATE_PATH, decrypted


class PushCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, dict(ENVIRONMENT, DIGEST_STATE_DIR=temporary.name), clear=True))
        self.connection = Connection()
        self.factory = self.enterContext(patch('calendar_digest.push_transport.http.client.HTTPSConnection',
                                              return_value=self.connection))

    def command(self, *arguments):
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            result = main(list(arguments))
        for secret in ENVIRONMENT.values():
            self.assertNotIn(secret, output.getvalue() + diagnostics.getvalue())
        return result, json.loads(output.getvalue()) if output.getvalue() else None, diagnostics.getvalue()

    def test_probe_only_validates_device_without_delivery(self):
        code, result, _ = self.command('probe-push')
        self.assertEqual(code, 0)
        self.assertEqual(result, {'pushover': 'ok', 'device_valid': True, 'sent': False})
        self.assertEqual([request[1] for request in self.connection.requests], [VALIDATE_PATH])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_birthday_probe_only_prints_counts_and_needs_no_push_credentials(self):
        for name in ENVIRONMENT:
            os.environ.pop(name)
        contacts = BirthdayCollection([ContactBirthday('private-book', 'private-uid', 'Private name', 10, 20)], 1)
        with patch('calendar_digest.cli.collect_contact_birthdays', return_value=contacts) as collect:
            code, result, diagnostics = self.command('probe-birthdays')
        self.assertEqual(code, 0)
        self.assertEqual(result, {'icloud_contacts': 'ok', 'addressbooks': 1, 'birthdays': 1, 'sent': False})
        self.assertEqual(diagnostics, '')
        collect.assert_called_once_with()
        self.factory.assert_not_called()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_explicit_test_sends_encrypted_text_once_and_status_needs_no_credentials(self):
        code, result, _ = self.command('send-push-test')
        self.assertEqual(code, 0)
        self.assertEqual(result['state'], 'accepted')
        code, repeated, _ = self.command('send-push-test')
        self.assertEqual(code, 0)
        self.assertEqual(repeated, result)
        requests = [item for item in self.connection.requests if item[1] == SEND_PATH]
        self.assertEqual(len(requests), 1)
        values = parse_qs(requests[0][2].decode('ascii'))
        self.assertEqual(values['encrypted'], ['1'])
        self.assertTrue(decrypted(values['title'][0]))
        self.assertTrue(decrypted(values['message'][0]))
        for name in ENVIRONMENT:
            os.environ.pop(name)
        code, status, _ = self.command('push-status')
        self.assertEqual(code, 0)
        self.assertEqual(status['deliveries'][0]['state'], 'accepted')
        self.factory.assert_called_once()

    def test_lost_reply_reports_failure_and_repeat_does_not_send(self):
        self.connection.message = OSError('private-provider-error')
        code, result, diagnostics = self.command('send-push-test')
        self.assertEqual(code, 1)
        self.assertNotIn('private-provider-error', diagnostics)
        code, result, _ = self.command('send-push-test')
        self.assertEqual(code, 1)
        self.assertEqual(result['state'], 'uncertain')
        self.assertFalse(result['retryable'])
        self.assertEqual(len([item for item in self.connection.requests if item[1] == SEND_PATH]), 1)

    def test_preview_is_read_only_and_needs_no_pushover_credentials(self):
        for name in ENVIRONMENT:
            os.environ.pop(name)
        baseline = self.directory / 'calendar-state.json'
        baseline.write_text('email-baseline-unchanged', encoding='utf-8')
        with patch('calendar_digest.reminder_preview.collect_all', return_value=Collection([resource()], 11)):
            code, result, diagnostics = self.command('preview-reminders', '--date', '2026-10-15', '--days', '7')
        self.assertEqual(code, 0)
        self.assertFalse(result['sent'])
        self.assertTrue(Path(result['preview_html']).is_file())
        self.assertEqual(baseline.read_text(encoding='utf-8'), 'email-baseline-unchanged')
        self.assertIn('Предпросмотр сохранён.', diagnostics)
        self.factory.assert_not_called()

    def test_invalid_preview_window_is_rejected_before_reading_calendars(self):
        with patch('calendar_digest.reminder_preview.collect_all') as collect, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(['preview-reminders', '--days', '32'])
        self.assertEqual(caught.exception.code, 2)
        collect.assert_not_called()
        self.factory.assert_not_called()

    def test_prepare_warms_queue_without_sending_even_when_schedule_flag_is_enabled(self):
        from test_push_automation import cached

        os.environ['PUSH_SCHEDULE_ENABLED'] = 'true'
        with patch('calendar_digest.push_automation.sync_calendars',
                   return_value=SyncResult([cached()], 11, 1, 1, 0)):
            code, result, diagnostics = self.command('prepare-push')
        self.assertEqual(code, 0)
        self.assertEqual(result['mode'], 'prepare')
        self.assertFalse(result['sent'])
        self.assertIsNone(result['queue']['activated_at'])
        self.assertEqual(result['queue']['resources'], 1)
        self.assertIn('Уведомления не отправляются.', diagnostics)
        self.factory.assert_not_called()

    def test_automatic_cycle_requires_explicit_schedule_flag(self):
        with patch('calendar_digest.push_automation.sync_calendars') as collect:
            code, result, diagnostics = self.command('push-cycle')
        self.assertEqual(code, 1)
        self.assertIsNone(result)
        self.assertIn('PUSH_SCHEDULE_ENABLED=false', diagnostics)
        collect.assert_not_called()
        self.factory.assert_not_called()

    def test_status_includes_optional_scheduler_and_queue_without_credentials_or_network(self):
        for name in ENVIRONMENT:
            os.environ.pop(name)
        with patch('calendar_digest.push_automation.sync_calendars') as collect:
            code, result, _ = self.command('push-status')
        self.assertEqual(code, 0)
        self.assertFalse(result['automatic']['enabled'])
        self.assertFalse(result['automatic']['heartbeat_recent'])
        self.assertEqual(result['automatic']['queue']['resources'], 0)
        collect.assert_not_called()
        self.factory.assert_not_called()

    def test_busy_calendar_gate_has_temporary_exit_code_for_scheduler_retry(self):
        with calendar_operation_lock(self.directory), patch('calendar_digest.push_automation.sync_calendars') as collect:
            code, result, diagnostics = self.command('prepare-push')
        self.assertEqual(code, 75)
        self.assertIsNone(result)
        self.assertIn('Календари сейчас обновляются', diagnostics)
        collect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
