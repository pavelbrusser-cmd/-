from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
import smtplib
import unittest
from unittest.mock import Mock, patch

from calendar_digest.control_email_delivery import make_control_email_message, submit_control_email
from calendar_digest.control_email_rules import ControlEmailReminder
from calendar_digest.smtp_delivery import SMTPDeliveryError, SMTPDeliveryUncertain
from test_smtp_delivery import FakeSMTP, SETTINGS


NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)


def reminder(**changes):
    values = dict(source_id='mailru_2', calendar_id='private-calendar-id', calendar_name='Контроль поручений',
                  account_email='gpb.control@mail.ru', uid='private-event-id', recurrence_id=None,
                  recipient='participant@example.test', title='Обсуждение проекта',
                  event_start='2026-10-19T13:00:00+03:00', event_end='2026-10-19T13:30:00+03:00',
                  all_day=False, location='Переговорная', rule='h1', notify_at='2026-10-19T12:00:00+03:00')
    values.update(changes)
    item = ControlEmailReminder(**values)
    return dict(asdict(item), stable_id=item.stable_id)


def decode(payload):
    return BytesParser(policy=default).parsebytes(payload)


class ControlEmailMessageTests(unittest.TestCase):
    def build(self, item=None):
        item = item or reminder()
        return decode(make_control_email_message(item, replace(SETTINGS, recipient=item['recipient']), generated_at=NOW))

    def test_exact_subject_and_high_importance_have_real_sender_and_single_recipient(self):
        message = self.build()
        self.assertEqual(str(message['Subject']), 'ВНИМАНИЕ! Контроль и мониторинг поручений!')
        self.assertEqual(message['Importance'], 'high')
        self.assertEqual(message['X-Priority'], '1 (Highest)')
        self.assertEqual(message['X-MSMail-Priority'], 'High')
        self.assertEqual(message['From'].addresses[0].addr_spec, SETTINGS.username)
        self.assertEqual([address.addr_spec for address in message['To'].addresses], ['participant@example.test'])
        for header in ('Cc', 'Bcc', 'Reply-To', 'Sender'):
            self.assertNotIn(header, message)
        self.assertEqual([part.get_content_type() for part in message.iter_parts()], ['text/plain', 'text/html'])
        self.assertFalse(list(message.iter_attachments()))

    def test_both_alternatives_contain_only_the_selected_event_fields(self):
        item = reminder(title='<script>PRIVATE_TITLE</script>\nСледующая строка', location='<b>PRIVATE_LOCATION</b>')
        item.update(description='FORBIDDEN_DESCRIPTION', attendees=['other@example.test'],
                    owner_action='https://private.example.test/a/OWNER_ACTION_TOKEN', digest='OTHER_EVENT_TITLE')
        message = self.build(item)
        plain, html = [part.get_content() for part in message.iter_parts()]
        for text in (plain, html):
            for allowed in ('PRIVATE_TITLE', 'PRIVATE_LOCATION', '19.10.2026, 13:00–13:30 МСК',
                            'За 1 час', 'Контроль поручений', 'gpb.control@mail.ru'):
                self.assertIn(allowed, text)
            for forbidden in ('FORBIDDEN_DESCRIPTION', 'other@example.test', 'OWNER_ACTION_TOKEN',
                              'OTHER_EVENT_TITLE', 'private-event-id', 'private-calendar-id', SETTINGS.recipient):
                self.assertNotIn(forbidden, text)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('background:#fee2e2;color:#b91c1c;', html)
        self.assertNotIn('text/calendar', message.as_string())

    def test_message_id_is_stable_for_same_identity_and_separate_for_each_recipient(self):
        first = self.build()
        later = reminder(title='Updated title', event_start='2026-10-19T14:00:00+03:00',
                         event_end='2026-10-19T15:00:00+03:00')
        self.assertEqual(first['Message-ID'], self.build(later)['Message-ID'])
        self.assertNotEqual(first['Message-ID'], self.build(reminder(recipient='another@example.test'))['Message-ID'])
        self.assertTrue(first['Message-ID'].endswith('@example.test>'))

    def test_all_day_exclusive_end_and_moscow_conversion_are_readable(self):
        all_day = self.build(reminder(all_day=True, event_start='2026-10-19', event_end='2026-10-22', rule='d1'))
        self.assertIn('19.10.2026 — 21.10.2026 · весь день', all_day.get_body(preferencelist=('plain',)).get_content())
        timed = self.build(reminder(event_start='2026-10-19T10:00:00+00:00', event_end='2026-10-19T10:30:00+00:00'))
        self.assertIn('19.10.2026, 13:00–13:30 МСК', timed.get_body(preferencelist=('plain',)).get_content())

    def test_deferred_hourly_label_does_not_claim_exact_hour(self):
        message = self.build(reminder(event_start='2026-10-19T06:00:00+03:00',
                                     event_end='2026-10-19T06:30:00+03:00', notify_at='2026-10-19T07:00:00+03:00'))
        for part in message.iter_parts():
            self.assertIn('Перенесено на 07:00 МСК', part.get_content())
            self.assertNotIn('За 1 час', part.get_content())

    def test_bad_scope_or_payload_is_rejected_without_echoing_values(self):
        for change in ({'source_id': 'icloud'}, {'account_email': 'private-wrong@example.test'},
                       {'stable_id': 'private-unsafe-id'}, {'recipient': 'private@example.test\r\nBcc: other@example.test'},
                       {'event_start': '2026-10-19T13:00:00'}, {'rule': 'private-unknown'}):
            item = dict(reminder(), **change)
            with self.subTest(change=change), self.assertRaises(SMTPDeliveryError) as caught:
                make_control_email_message(item, replace(SETTINGS, recipient='participant@example.test'), generated_at=NOW)
            self.assertNotIn('private', str(caught.exception))


class ControlEmailTransportTests(unittest.TestCase):
    def test_authentication_and_one_envelope_precede_durable_claim_and_data(self):
        client = FakeSMTP()
        def claim():
            client.commands.append('claim')
        with patch('calendar_digest.smtp_delivery.smtplib.SMTP_SSL', return_value=client):
            submit_control_email(reminder(), SETTINGS, generated_at=NOW, before_data=claim)
        self.assertLess(client.commands.index('login'), client.commands.index('claim'))
        self.assertLess(client.commands.index('rcpt'), client.commands.index('claim'))
        self.assertLess(client.commands.index('claim'), client.commands.index('data'))
        self.assertEqual(client.sender, SETTINGS.username)
        self.assertEqual(client.recipient, 'participant@example.test')
        self.assertEqual(SETTINGS.recipient, 'owner@example.test')

    def test_claim_refusal_never_submits_data(self):
        client = FakeSMTP()
        class Stale(Exception):
            pass
        with patch('calendar_digest.smtp_delivery.smtplib.SMTP_SSL', return_value=client), self.assertRaises(Stale):
            submit_control_email(reminder(), SETTINGS, generated_at=NOW, before_data=Mock(side_effect=Stale()))
        self.assertNotIn('data', client.commands)
        self.assertIn('close', client.commands)

    def test_lost_data_reply_is_uncertain_and_explicit_rejection_preserves_retryability(self):
        for error, expected, retryable in ((OSError('private connection'), SMTPDeliveryUncertain, False),
                                          (smtplib.SMTPDataError(451, b'private reply'), SMTPDeliveryError, True),
                                          (smtplib.SMTPDataError(550, b'private reply'), SMTPDeliveryError, False)):
            with self.subTest(error=type(error).__name__):
                client = FakeSMTP(data=error)
                with patch('calendar_digest.smtp_delivery.smtplib.SMTP_SSL', return_value=client), self.assertRaises(expected) as caught:
                    submit_control_email(reminder(), SETTINGS, generated_at=NOW, before_data=lambda: None)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertNotIn('private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
