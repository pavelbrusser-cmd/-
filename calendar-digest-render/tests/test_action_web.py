from dataclasses import asdict, replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest.action_store import StoreError
from html.parser import HTMLParser

from calendar_digest.action_web import create_app
from calendar_digest.event_actions import EventTarget, prepare_change, verify_change
from calendar_digest.writable_caldav import ResourceVersion, CalDAVConflict, CalDAVUncertain


DATA = '''BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Synthetic//EN\r
BEGIN:VEVENT\r
UID:synthetic\r
DTSTAMP:20261001T000000Z\r
DTSTART:20261014T090000Z\r
DTEND:20261014T100000Z\r
SUMMARY:Синтетическая встреча <script>alert(1)</script>\r
STATUS:TENTATIVE\r
END:VEVENT\r
END:VCALENDAR\r
'''


class Inputs(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.fields = {}
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and 'name' in attrs:
            self.fields[attrs['name']] = attrs.get('value', '')


class MemoryCalendar:
    def __init__(self):
        self.version = ResourceVersion(DATA, '"v1"', 'owner@example.invalid', 'icloud', 'synthetic', 'synthetic',
                                       'https://caldav.icloud.com/synthetic.ics', 'Тест', True)
        self.reads = 0
        self.writes = 0
        self.problem = None

    def read(self, target):
        self.reads += 1
        return self.version

    def write(self, target, version, data, *, verify):
        self.writes += 1
        if self.problem:
            raise self.problem
        if version.etag != self.version.etag:
            raise CalDAVConflict('Конфликт версии.')
        self.version = replace(self.version, data=data, etag='"v2"')
        if not verify(data):
            raise CalDAVUncertain('Проверка записи не удалась.')
        return self.version


class ActionWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calendar = MemoryCalendar()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'x' * 40, 'WEB_PASSWORD': 'y' * 40,
                               'STATE_DIR': self.temp.name}, backend=self.calendar)
        self.client = self.app.test_client()
        self.store = self.app.extensions['action_store']
        self.target = EventTarget('icloud', 'synthetic', 'synthetic', '2026-10-14T12:00:00+03:00',
                                  '2026-10-14T13:00:00+03:00', expected_event_status='TENTATIVE')

    def link(self, action='confirm', target=None):
        return '/a/' + self.store.issue(asdict(target or self.target), action)

    def get(self, path):
        return self.client.get(path, base_url='https://digest.example')

    def post(self, path, data):
        return self.client.post(path, data=data, base_url='https://digest.example')

    def login(self, path):
        first = self.get(path)
        fields = Inputs(first).fields
        fields['password'] = 'y' * 40
        result = self.post(path, fields)
        self.assertEqual(result.status_code, 303)
        return self.get(path)

    def test_health_and_invalid_links_never_touch_calendar(self):
        self.assertEqual(self.get('/health').json, {'status': 'ok'})
        self.assertEqual(self.get('/a/forged').status_code, 400)
        self.assertEqual(self.calendar.reads, 0)
        self.assertEqual(self.calendar.writes, 0)

    def test_login_and_scanner_get_cannot_change_or_consume_link(self):
        path = self.link()
        for _ in range(3):
            response = self.get(path)
            self.assertIn('Пароль сервиса', response.get_data(as_text=True))
        self.assertEqual(self.calendar.reads, 0)
        self.assertEqual(self.store.read(path[3:])['state'], 'ready')
        response = self.login(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calendar.writes, 0)
        self.assertEqual(self.store.read(path[3:])['state'], 'ready')
        self.assertNotIn('<script>', response.get_data(as_text=True))
        self.assertIn('&lt;script&gt;', response.get_data(as_text=True))
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_csrf_and_bad_password_block_writes(self):
        path = self.link()
        fields = Inputs(self.get(path)).fields
        fields['password'] = 'wrong'
        self.assertEqual(self.post(path, fields).status_code, 401)
        fields = Inputs(self.login(path)).fields
        fields['csrf'] = 'forged'
        self.assertEqual(self.post(path, fields).status_code, 400)
        self.assertEqual(self.calendar.writes, 0)

    def test_confirm_is_written_verified_and_not_replayed(self):
        path = self.link()
        fields = Inputs(self.login(path)).fields
        result = self.post(path, fields)
        self.assertEqual(result.status_code, 303)
        self.assertIn('STATUS:CONFIRMED', self.calendar.version.data)
        self.assertEqual(self.store.read(path[3:])['state'], 'done')
        self.assertIn('Изменение сохранено', self.get(path).get_data(as_text=True))
        self.assertEqual(self.post(path, fields).status_code, 200)
        self.assertEqual(self.calendar.writes, 1)

    def test_stale_page_etag_and_stale_digest_both_block(self):
        path = self.link()
        fields = Inputs(self.login(path)).fields
        self.calendar.version = replace(self.calendar.version, etag='"external"')
        self.assertEqual(self.post(path, fields).status_code, 409)
        self.assertEqual(self.calendar.writes, 0)
        self.calendar.version = replace(self.calendar.version, data=DATA.replace('STATUS:TENTATIVE', 'STATUS:CONFIRMED'))
        self.assertEqual(self.get(path).status_code, 409)
        self.assertEqual(self.calendar.writes, 0)

    def test_reschedule_moscow_and_invalid_interval(self):
        path = self.link('reschedule')
        fields = Inputs(self.login(path)).fields
        fields.update(new_start='2026-10-15T15:30', new_end='2026-10-15T15:00')
        self.assertEqual(self.post(path, fields).status_code, 400)
        self.assertEqual(self.calendar.writes, 0)
        fields['new_end'] = '2026-10-15T16:45'
        self.assertEqual(self.post(path, fields).status_code, 303)
        self.assertIn('DTSTART:20261015T123000Z', self.calendar.version.data)
        self.assertIn('DTEND:20261015T134500Z', self.calendar.version.data)
        self.assertEqual(self.calendar.writes, 1)

    def test_all_day_form_end_is_inclusive(self):
        self.calendar.version = replace(self.calendar.version, data=DATA.replace(
            'DTSTART:20261014T090000Z\r\nDTEND:20261014T100000Z',
            'DTSTART;VALUE=DATE:20261014\r\nDTEND;VALUE=DATE:20261015'))
        target = replace(self.target, expected_start='2026-10-14', expected_end='2026-10-15')
        path = self.link('reschedule', target)
        fields = Inputs(self.login(path)).fields
        self.assertEqual(fields['new_end'], '2026-10-14')
        fields.update(new_start='2026-10-17', new_end='2026-10-18')
        self.assertEqual(self.post(path, fields).status_code, 303)
        self.assertIn('DTEND;VALUE=DATE:20261019', self.calendar.version.data)

    def test_uncertain_write_is_never_retried(self):
        path = self.link('cancel')
        fields = Inputs(self.login(path)).fields
        self.calendar.problem = CalDAVUncertain('Проверьте календарь.')
        self.assertEqual(self.post(path, fields).status_code, 400)
        self.assertEqual(self.store.read(path[3:])['state'], 'uncertain')
        self.assertEqual(self.post(path, fields).status_code, 200)
        self.assertEqual(self.calendar.writes, 1)

    def test_unknown_exception_after_write_is_uncertain_and_hidden(self):
        path = self.link()
        fields = Inputs(self.login(path)).fields
        self.calendar.problem = RuntimeError('password=SECRET should never appear')
        result = self.post(path, fields)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn('SECRET', result.get_data(as_text=True))
        self.assertEqual(self.store.read(path[3:])['state'], 'uncertain')
        self.post(path, fields)
        self.assertEqual(self.calendar.writes, 1)

    def test_mail_status_cancel_is_blocked_before_confirmation_and_write(self):
        self.calendar.version = replace(self.calendar.version, source_id='mailru_1')
        target = replace(self.target, source_id='mailru_1')
        path = self.link('cancel', target)
        response = self.login(path)
        self.assertEqual(response.status_code, 400)
        self.assertIn('пока недоступна', response.get_data(as_text=True))
        self.assertNotIn('name="operation" value="apply"', response.get_data(as_text=True))
        self.assertEqual(self.store.read(path[3:])['state'], 'ready')
        self.assertEqual(self.calendar.writes, 0)

    def test_receipt_failure_after_successful_put_is_uncertain(self):
        path = self.link()
        fields = Inputs(self.login(path)).fields
        with patch.object(self.store, 'complete', side_effect=StoreError('Storage failed')):
            response = self.post(path, fields)
        self.assertEqual(response.status_code, 400)
        self.assertIn('STATUS:CONFIRMED', self.calendar.version.data)
        self.assertEqual(self.store.read(path[3:])['state'], 'uncertain')
        self.post(path, fields)
        self.assertEqual(self.calendar.writes, 1)

    def test_readonly_permission_and_wrong_target_fields_do_not_write(self):
        path = self.link()
        response = self.login(path)
        self.calendar.version = replace(self.calendar.version, writable=False)
        fields = Inputs(response).fields
        fields.update(uid='unrelated', action='reschedule', calendar_id='unrelated')
        self.assertEqual(self.post(path, fields).status_code, 400)
        self.assertEqual(self.calendar.writes, 0)

    def test_confirmation_operation_differs_for_participant(self):
        self.calendar.version = replace(self.calendar.version, data=DATA.replace('STATUS:TENTATIVE',
            'STATUS:CONFIRMED\r\nORGANIZER:mailto:someone@example.invalid\r\nATTENDEE;PARTSTAT=ACCEPTED:mailto:owner@example.invalid'))
        target = replace(self.target, expected_event_status='CONFIRMED', expected_participation='ACCEPTED')
        path = self.link('cancel', target)
        response = self.login(path)
        self.assertIn('Отклонить участие', response.get_data(as_text=True))
        self.assertEqual(self.post(path, Inputs(response).fields).status_code, 303)
        self.assertIn('PARTSTAT=DECLINED', self.calendar.version.data)
        self.assertIn('STATUS:CONFIRMED', self.calendar.version.data)


if __name__ == '__main__':
    unittest.main()
