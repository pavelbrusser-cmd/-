"""Exercise operator commands through a real outbox with a fake SMTP boundary."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from email import policy
from email.parser import BytesParser
import fcntl
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from calendar_digest.calendar_collection import Collection
from calendar_digest.cli import main
from test_preview import resource


class DeliveryCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = {
            'DIGEST_STATE_DIR': temporary.name,
            'DIGEST_SCHEDULE_ENABLED': 'false',
            'DIGEST_ACTION_SECRET': 'fictional-signing-key-' * 3,
            'DIGEST_PUBLIC_URL': 'https://digest.example.test',
            'SMTP_USERNAME': 'sender@example.invalid',
            'SMTP_PASSWORD': 'fictional-secret-never-print',
            'DIGEST_RECIPIENT': 'recipient@example.invalid',
        }
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.smtp = Mock()
        self.smtp.ehlo.return_value = (250, b'fake server')
        self.smtp.login.return_value = (235, b'authenticated')
        self.smtp.mail.return_value = (250, b'ok')
        self.smtp.rcpt.return_value = (250, b'ok')
        self.smtp.data.return_value = (250, b'accepted')
        self.smtp.quit.return_value = (221, b'bye')
        self.connection = self.enterContext(patch('calendar_digest.smtp_delivery.smtplib.SMTP_SSL',
                                                 return_value=self.smtp))

    def command(self, *arguments):
        output, diagnostics = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            code = main(list(arguments))
        for value in ('sender@example.invalid', 'recipient@example.invalid', 'fictional-secret-never-print'):
            self.assertNotIn(value, output.getvalue() + diagnostics.getvalue())
        return code, json.loads(output.getvalue()) if output.getvalue() else None, diagnostics.getvalue()

    def test_explicit_test_command_sends_complete_mime_once_and_reports_acceptance(self):
        from dataclasses import replace

        event = resource(title='Проверочная встреча')
        event = replace(event, data=event.data.replace('END:VEVENT', 'STATUS:CONFIRMED\r\nEND:VEVENT'))
        with patch('calendar_digest.preview.collect_all', return_value=Collection([event], 11)) as collect:
            code, result, diagnostics = self.command('send-test', '--date', '2026-10-14')
            self.assertEqual(code, 0)
            self.assertEqual(result['state'], 'accepted')
            self.assertEqual(result['kind'], 'test')
            self.assertEqual(result['events'], 1)
            self.assertNotIn('Проверочная встреча', diagnostics)
            self.assertIn('Дайджест сохранён.', diagnostics)
            self.smtp.data.assert_called_once()
            message = BytesParser(policy=policy.default).parsebytes(self.smtp.data.call_args.args[0])
            self.assertIn('[Проверка]', str(message['Subject']))
            self.assertIn('14 октября 2026', str(message['Subject']))
            self.assertIn('Проверочная встреча', message.get_body(preferencelist=('plain',)).get_content())
            html = message.get_body(preferencelist=('html',)).get_content()
            self.assertIn('https://digest.example.test/a/', html)
            self.assertNotIn('data-action=', html)
            code, again, _ = self.command('send-test', '--date', '2026-10-14')
            self.assertEqual(code, 0)
            self.assertEqual(again['state'], 'accepted')
            collect.assert_called_once()
            self.smtp.data.assert_called_once()
        os.environ.pop('SMTP_PASSWORD')
        code, status, _ = self.command('delivery-status')
        self.assertEqual(code, 0)
        self.assertFalse(status['schedule']['enabled'])
        self.assertFalse(status['schedule']['heartbeat_recent'])
        self.assertEqual(status['deliveries'][0]['state'], 'accepted')
        self.connection.assert_called_once()

    def test_probe_authenticates_without_calendar_reads_or_envelope(self):
        with patch('calendar_digest.preview.collect_all') as collect:
            code, result, _ = self.command('probe-smtp')
        self.assertEqual(code, 0)
        self.assertTrue(result['authenticated'])
        self.assertFalse(result['sent'])
        collect.assert_not_called()
        self.smtp.mail.assert_not_called()
        self.smtp.rcpt.assert_not_called()
        self.smtp.data.assert_not_called()

    def test_status_remains_useful_while_preparation_holds_lock(self):
        with (self.directory / '.delivery.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, status, _ = self.command('delivery-status')
        self.assertEqual(code, 0)
        self.assertTrue(status['busy'])
        self.assertFalse(status['schedule']['enabled'])
        self.smtp.data.assert_not_called()


if __name__ == '__main__':
    unittest.main()
