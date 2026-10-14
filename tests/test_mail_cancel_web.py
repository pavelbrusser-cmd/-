"""Owner confirmation, durable backup and no replay for personal cancellation."""

from dataclasses import asdict, replace
from datetime import date
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from calendar_digest.action_links import issue_email
from calendar_digest.action_store import StoreError
from calendar_digest.action_web import create_app
from calendar_digest.calendar_collection import CalendarResource, Collection
from calendar_digest.cancellation_archive import merge_cancellations
from calendar_digest.event_actions import EventTarget
from calendar_digest.preview import create_preview
from calendar_digest.selection import select_day
from calendar_digest.writable_caldav import CalDAVConflict, CalDAVUncertain
from test_action_web import DATA, Inputs, MemoryCalendar
from test_action_links import Markup


class MailCalendar(MemoryCalendar):
    def __init__(self):
        super().__init__()
        self.version = replace(
            self.version, data=DATA.replace('STATUS:TENTATIVE', 'STATUS:CONFIRMED'),
            source_id='mailru_1', resource_url='https://calendar.mail.ru/test/synthetic.ics',
            can_delete=True,
        )
        self.deletes = 0
        self.before_delete = None
        self.deleted = False

    def delete(self, target, version):
        self.deletes += 1
        if self.before_delete:
            self.before_delete()
        if self.problem:
            raise self.problem
        if version.etag != self.version.etag:
            raise CalDAVConflict('Конфликт версии.')
        self.deleted = True


class MailCancellationWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calendar = MailCalendar()
        self.config = {'TESTING': True, 'SECRET_KEY': 'x' * 40, 'WEB_PASSWORD': 'y' * 40,
                       'STATE_DIR': self.temp.name}
        self.app = create_app(self.config, backend=self.calendar)
        self.client = self.app.test_client()
        self.store = self.app.extensions['action_store']
        self.target = EventTarget('mailru_1', 'synthetic', 'synthetic',
                                 '2026-10-14T12:00:00+03:00', '2026-10-14T13:00:00+03:00',
                                 expected_event_status='CONFIRMED')

    def link(self):
        return '/a/' + self.store.issue(asdict(self.target), 'cancel')

    def get(self, path):
        return self.client.get(path, base_url='https://digest.example')

    def post(self, path, fields):
        return self.client.post(path, data=fields, base_url='https://digest.example')

    def login(self, path):
        response = self.get(path)
        fields = Inputs(response).fields
        if 'password' not in fields:
            return response
        fields['password'] = 'y' * 40
        self.assertEqual(self.post(path, fields).status_code, 303)
        return self.get(path)

    def archived(self):
        return merge_cancellations([], [], date(2026, 10, 14), self.directory)

    def test_owner_get_explains_deletion_and_post_archives_once_across_restart(self):
        path = self.link()
        self.get(path)
        self.assertEqual(self.calendar.reads, 0)
        response = self.login(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Отменить и удалить из календаря', response.get_data(as_text=True))
        self.assertNotIn('останется в календаре', response.get_data(as_text=True))
        self.assertEqual(self.calendar.deletes, 0)
        self.assertEqual(self.archived(), [])
        self.calendar.before_delete = lambda: self.assertEqual(self.archived(), [])
        fields = Inputs(response).fields
        self.assertEqual(self.post(path, fields).status_code, 303)
        self.assertTrue(self.calendar.deleted)
        self.assertEqual(self.calendar.writes, 0)
        self.assertEqual(self.store.read(path[3:])['state'], 'done')
        self.assertEqual(len(self.archived()), 1)
        self.assertEqual(self.archived()[0].calendar_name, 'Тест')
        self.assertEqual(self.archived()[0].event_status, 'CANCELLED')
        self.assertIn('Отменено', self.get(path).get_data(as_text=True))
        self.assertEqual(self.post(path, fields).status_code, 200)
        self.assertEqual(self.calendar.deletes, 1)
        restarted = create_app(self.config, backend=self.calendar)
        self.assertEqual(restarted.extensions['action_store'].read(path[3:])['state'], 'done')
        self.assertEqual(len(self.archived()), 1)

    def test_backup_failure_prevents_delete(self):
        path = self.link()
        fields = Inputs(self.login(path)).fields
        with patch.object(self.store, 'stage_cancellation', side_effect=StoreError('Копия не сохранена.')):
            response = self.post(path, fields)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calendar.deletes, 0)
        self.assertEqual(self.calendar.writes, 0)
        self.assertEqual(self.store.read(path[3:])['state'], 'failed')
        self.assertEqual(self.archived(), [])

    def test_uncertain_delete_and_receipt_failure_are_never_replayed_or_shown_as_cancelled(self):
        for failure in ('network', 'receipt'):
            with self.subTest(failure=failure):
                path = self.link()
                fields = Inputs(self.login(path)).fields
                if failure == 'network':
                    self.calendar.problem = CalDAVUncertain('Проверьте календарь.')
                    response = self.post(path, fields)
                else:
                    self.calendar.problem = None
                    with patch.object(self.store, 'complete', side_effect=StoreError('Ошибка сохранения.')):
                        response = self.post(path, fields)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.store.read(path[3:])['state'], 'uncertain')
                self.assertEqual(self.archived(), [])
                count = self.calendar.deletes
                self.post(path, fields)
                self.assertEqual(self.calendar.deletes, count)

    def test_recurrence_is_put_only_and_preserves_neighbor_instances(self):
        self.calendar.version = replace(self.calendar.version, data=self.calendar.version.data.replace(
            'STATUS:CONFIRMED', 'STATUS:CONFIRMED\r\nRRULE:FREQ=DAILY;COUNT=3'), can_delete=False)
        self.target = replace(self.target, recurrence_id=self.target.expected_start)
        path = self.link()
        response = self.login(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Отменить только это повторение', response.get_data(as_text=True))
        self.assertEqual(self.post(path, Inputs(response).fields).status_code, 303)
        self.assertEqual(self.calendar.deletes, 0)
        self.assertEqual(self.calendar.writes, 1)
        resource = SimpleNamespace(source_id='mailru_1', calendar_id='synthetic', calendar_name='Тест',
                                   account_email=self.calendar.version.account_email, data=self.calendar.version.data)
        self.assertEqual(select_day([resource], date(2026, 10, 14)), [])
        self.assertEqual(len(select_day([resource], date(2026, 10, 15))), 1)
        cancelled = merge_cancellations([], [resource], date(2026, 10, 14), self.directory)
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].event_status, 'CANCELLED')

    def test_organizer_invitation_is_not_deleted_and_participant_reply_stays_put(self):
        original = self.calendar.version
        self.calendar.version = replace(original, data=original.data.replace('STATUS:CONFIRMED',
            'STATUS:CONFIRMED\r\nORGANIZER:mailto:owner@example.invalid\r\n'
            'ATTENDEE;PARTSTAT=ACCEPTED:mailto:guest@example.invalid'), can_organize=True)
        path = self.link()
        self.assertEqual(self.login(path).status_code, 400)
        self.assertEqual(self.calendar.deletes, 0)
        self.assertEqual(self.calendar.writes, 0)
        self.calendar.version = replace(original, data=original.data.replace('STATUS:CONFIRMED',
            'STATUS:CONFIRMED\r\nORGANIZER:mailto:guest@example.invalid\r\n'
            'ATTENDEE;PARTSTAT=ACCEPTED:mailto:owner@example.invalid'))
        self.target = replace(self.target, expected_participation='ACCEPTED')
        path = self.link()
        response = self.get(path)
        self.assertIn('Отклонить участие', response.get_data(as_text=True))
        self.assertEqual(self.post(path, Inputs(response).fields).status_code, 303)
        self.assertEqual(self.calendar.deletes, 0)
        self.assertEqual(self.calendar.writes, 1)
        self.assertIn('PARTSTAT=DECLINED', self.calendar.version.data)
        self.assertEqual(self.archived(), [])

    def test_invalid_csrf_and_changed_form_cannot_delete(self):
        path = self.link()
        fields = Inputs(self.login(path)).fields
        self.assertEqual(self.post(path, {**fields, 'csrf': 'forged'}).status_code, 400)
        self.calendar.version = replace(self.calendar.version, etag='"external-change"')
        self.assertEqual(self.post(path, fields).status_code, 409)
        self.assertEqual(self.calendar.deletes, 0)
        self.assertEqual(self.archived(), [])

    def test_preview_link_cancel_and_next_digest_keep_move_history_without_live_resource(self):
        old_day, new_day = date(2026, 10, 14), date(2026, 10, 15)
        resource = CalendarResource('mailru_1', 'synthetic', 'Тест',
                                    self.calendar.version.account_email, self.calendar.version.data)
        with patch.dict('os.environ', {'DIGEST_STATE_DIR': str(self.directory)}), patch(
            'calendar_digest.preview.collect_all', return_value=Collection([resource], 1)
        ) as collect:
            create_preview(old_day, self.directory)
            moved = resource.data.replace('20261014T090000Z', '20261015T130000Z').replace(
                '20261014T100000Z', '20261015T140000Z')
            resource = replace(resource, data=moved)
            self.calendar.version = replace(self.calendar.version, data=moved, etag='"moved"')
            collect.return_value = Collection([resource], 1)
            result = create_preview(old_day, self.directory)
            self.assertEqual(result['moved'], 1)
            issued = issue_email(old_day, self.directory, state_dir=self.directory,
                                 base_url='https://digest.example', signing_key=self.config['SECRET_KEY'])
            markup = Markup(Path(issued['email_html']).read_text())
            cancel = next(link for link in markup.action_links if link['aria-label'].startswith('Отменить:'))
            path = urlsplit(cancel['href']).path
            fields = Inputs(self.login(path)).fields
            self.assertEqual(self.post(path, fields).status_code, 303)
            collect.return_value = Collection([], 1)
            for day in (old_day, new_day):
                result = create_preview(day, self.directory)
                self.assertEqual(result['resources'], 0)
                self.assertEqual(result['event_statuses'], {'CANCELLED': 1})
                event = json.loads(Path(result['preview_json']).read_text())['events'][0]
                self.assertEqual(event['original_start'], '2026-10-14T12:00:00+03:00')
                self.assertEqual(event['start'], '2026-10-15T16:00:00+03:00')
                issued = issue_email(day, self.directory, state_dir=self.directory,
                                     base_url='https://digest.example', signing_key=self.config['SECRET_KEY'])
                self.assertEqual(issued['action_links'], 0)
                self.assertEqual(Markup(Path(issued['email_html']).read_text()).action_links, [])


if __name__ == '__main__':
    unittest.main()
