"""Real cross-process flock and web/preview contention; no calendar network."""

from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
import errno
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest import calendar_operation_lock as locks
from calendar_digest.action_web import create_app
from calendar_digest.preview import create_preview
from calendar_digest.readonly_caldav import CalendarConnectionError
from calendar_digest.event_actions import EventTarget
from calendar_digest.writable_caldav import CalDAVUncertain
import test_action_web as web_fixtures
import test_preview_progress as preview_fixtures


PROBE = '''
from pathlib import Path
import sys
from calendar_digest.calendar_operation_lock import CalendarBusy, calendar_operation_lock
try:
    with calendar_operation_lock(Path(sys.argv[1])):
        print("acquired", flush=True)
except CalendarBusy:
    print("busy", flush=True)
'''

HOLD = '''
from pathlib import Path
import sys
from calendar_digest.calendar_operation_lock import calendar_operation_lock
with calendar_operation_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read(1)
'''


def child_probe(directory):
    return subprocess.run(
        [sys.executable, '-c', PROBE, str(directory)],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.strip()


@contextmanager
def child_lock(directory):
    child = subprocess.Popen(
        [sys.executable, '-c', HOLD, str(directory)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        readable, _, _ = select.select([child.stdout], [], [], 10)
        if not readable or child.stdout.readline().strip() != 'locked':
            raise AssertionError('Child lock did not become ready')
        yield child
    finally:
        if child.poll() is None:
            child.communicate('x', timeout=10)
        else:
            child.communicate(timeout=10)


class CalendarOperationLockTests(unittest.TestCase):
    def test_writable_managed_directory_does_not_require_permission_to_chmod_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'managed-volume'
            directory.mkdir()
            directory.chmod(0o2775)
            original_mode = stat.S_IMODE(directory.stat().st_mode)
            original_fchmod = os.fchmod

            def file_chmod_only(descriptor, mode):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise PermissionError(errno.EPERM, 'managed directory belongs to provider')
                return original_fchmod(descriptor, mode)

            with patch.object(locks.os, 'fchmod', side_effect=file_chmod_only):
                with locks.calendar_operation_lock(directory):
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), original_mode)
                    self.assertEqual(child_probe(directory), 'busy')
                    lock_file = directory / '.calendar-operation.lock'
                    self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)
            self.assertEqual(child_probe(directory), 'acquired')
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), original_mode)

    def test_new_state_directory_is_created_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'new-state'
            with locks.calendar_operation_lock(directory):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE((directory / '.calendar-operation.lock').stat().st_mode), 0o600)

    def test_cross_process_gate_is_nonblocking_private_and_keeps_one_inode(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'state'
            directory.mkdir(mode=0o755)
            directory.chmod(0o755)
            lock_file = directory / '.calendar-operation.lock'
            lock_file.touch(mode=0o644)
            lock_file.chmod(0o644)
            inode = lock_file.stat().st_ino
            with locks.calendar_operation_lock(directory):
                self.assertEqual(child_probe(directory), 'busy')
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                self.assertEqual(lock_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(child_probe(directory), 'acquired')
            self.assertEqual(lock_file.stat().st_ino, inode)

    def test_process_exit_and_body_exception_release_the_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with child_lock(directory) as child:
                with self.assertRaises(locks.CalendarBusy):
                    with locks.calendar_operation_lock(directory):
                        self.fail('Concurrent process entered')
                child.terminate()
                child.wait(timeout=10)
            self.assertEqual(child_probe(directory), 'acquired')
            with self.assertRaisesRegex(RuntimeError, 'work failed'):
                with locks.calendar_operation_lock(directory):
                    raise RuntimeError('work failed')
            self.assertEqual(child_probe(directory), 'acquired')

    def test_contention_and_filesystem_error_close_every_acquired_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with locks.calendar_operation_lock(directory):
                with patch.object(locks.os, 'open', wraps=os.open) as opened, \
                        patch.object(locks.os, 'close', wraps=os.close) as closed:
                    with self.assertRaises(locks.CalendarBusy):
                        with locks.calendar_operation_lock(directory):
                            self.fail('Concurrent owner entered')
                    self.assertEqual(opened.call_count, 2)
                    self.assertEqual(closed.call_count, 2)
            with patch.object(locks.os, 'open', wraps=os.open) as opened, \
                    patch.object(locks.os, 'close', wraps=os.close) as closed, \
                    patch.object(locks.os, 'fchmod', side_effect=PermissionError('private-path')):
                with self.assertRaises(locks.CalendarLockError) as error:
                    with locks.calendar_operation_lock(directory):
                        self.fail('Broken storage entered')
                self.assertEqual(opened.call_count, 2)
                self.assertEqual(closed.call_count, 2)
                self.assertNotIn('private-path', str(error.exception))
                self.assertNotIn(temporary, str(error.exception))

    def test_lock_symlink_and_unexpected_flock_error_have_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'state'
            directory.mkdir(mode=0o700)
            target = Path(temporary) / 'other'
            target.write_text('unrelated')
            target.chmod(0o644)
            lock_file = directory / '.calendar-operation.lock'
            lock_file.symlink_to(target)
            with self.assertRaises(locks.CalendarLockError) as error:
                with locks.calendar_operation_lock(directory):
                    self.fail('Symlink entered')
            self.assertNotIn(temporary, str(error.exception))
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            lock_file.unlink()
            with patch.object(locks.fcntl, 'flock', side_effect=OSError(errno.EIO, 'private-path')):
                with self.assertRaises(locks.CalendarLockError) as error:
                    with locks.calendar_operation_lock(directory):
                        self.fail('Filesystem failure entered')
                self.assertNotIn('private-path', str(error.exception))
            self.assertEqual(child_probe(directory), 'acquired')


class CalendarGateIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'state'
        self.output = Path(self.temp.name) / 'preview-output'
        self.enterContext(patch.dict(os.environ, {'DIGEST_STATE_DIR': str(self.directory)}))
        self.calendar = web_fixtures.MemoryCalendar()
        self.app = create_app({
            'TESTING': True, 'SECRET_KEY': 'x' * 40, 'WEB_PASSWORD': 'y' * 40,
            'STATE_DIR': str(self.directory),
        }, backend=self.calendar)
        self.client = self.app.test_client()
        self.store = self.app.extensions['action_store']
        self.target = EventTarget('icloud', 'synthetic', 'synthetic',
            '2026-10-14T12:00:00+03:00', '2026-10-14T13:00:00+03:00',
            expected_event_status='TENTATIVE')
        self.token = self.store.issue(asdict(self.target), 'confirm')
        self.path = '/a/' + self.token

    def get(self, path):
        return self.client.get(path, base_url='https://digest.example')

    def post(self, path, fields):
        return self.client.post(path, data=fields, base_url='https://digest.example')

    def login_only(self):
        fields = web_fixtures.Inputs(self.get(self.path)).fields
        fields['password'] = 'y' * 40
        self.assertEqual(self.post(self.path, fields).status_code, 303)

    def test_child_collector_gate_leaves_health_login_and_terminal_pages_available(self):
        with child_lock(self.directory):
            self.assertEqual(self.get('/health').json, {'status': 'ok'})
            self.assertEqual(self.get('/').status_code, 200)
            self.login_only()
            self.assertEqual(self.calendar.reads, 0)
            response = self.get(self.path)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers['Retry-After'], '30')
            self.assertIn('Календари сейчас обновляются', response.get_data(as_text=True))
            self.assertEqual(self.calendar.reads, 0)
            self.assertEqual(self.store.read(self.token)['state'], 'ready')
            self.assertTrue(self.store.claim(self.token))
            self.store.complete(self.token, {'message': 'Готово'})
            self.assertEqual(self.get(self.path).status_code, 200)
            self.assertEqual(self.calendar.reads, 0)

    def test_busy_post_does_not_read_claim_or_write_and_can_retry_after_release(self):
        self.login_only()
        fields = web_fixtures.Inputs(self.get(self.path)).fields
        before_reads = self.calendar.reads
        with child_lock(self.directory), patch.object(self.store, 'claim', wraps=self.store.claim) as claim:
            response = self.post(self.path, fields)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers['Retry-After'], '30')
            claim.assert_not_called()
            self.assertEqual(self.calendar.reads, before_reads)
            self.assertEqual(self.calendar.writes, 0)
            self.assertEqual(self.store.read(self.token)['state'], 'ready')
        self.assertEqual(self.post(self.path, fields).status_code, 303)
        self.assertEqual(self.calendar.writes, 1)

    def test_preview_uses_shared_state_even_when_output_directory_is_different(self):
        with child_lock(self.directory), patch('calendar_digest.preview.collect_all') as collect:
            with self.assertRaises(locks.CalendarBusy):
                create_preview(date(2026, 10, 15), self.output)
            collect.assert_not_called()
            self.assertFalse(self.output.exists())
        with patch('calendar_digest.preview.collect_all', side_effect=preview_fixtures.collection):
            result = create_preview(date(2026, 10, 15), self.output)
        self.assertEqual(result['events'], 1)
        self.assertTrue((self.output / '.preview.lock').is_file())

    def test_actual_preview_blocks_web_reads_and_releases_on_collection_failure(self):
        self.login_only()

        def collector():
            self.assertEqual(child_probe(self.directory), 'busy')
            self.assertEqual(self.get(self.path).status_code, 503)
            self.assertEqual(self.calendar.reads, 0)
            raise CalendarConnectionError('Недоступен источник.')

        with patch('calendar_digest.preview.collect_all', side_effect=collector):
            with self.assertRaises(CalendarConnectionError):
                create_preview(date(2026, 10, 15), self.output)
        self.assertEqual(child_probe(self.directory), 'acquired')
        self.assertEqual(self.get(self.path).status_code, 200)

    def test_web_holds_gate_through_calendar_write_and_releases_on_uncertain_result(self):
        self.login_only()
        fields = web_fixtures.Inputs(self.get(self.path)).fields
        original_read = self.calendar.read

        def read(target):
            self.assertEqual(child_probe(self.directory), 'busy')
            with patch('calendar_digest.preview.collect_all') as collect:
                with self.assertRaises(locks.CalendarBusy):
                    create_preview(date(2026, 10, 15), self.output)
                collect.assert_not_called()
            return original_read(target)

        def write(*args, **kwargs):
            self.assertEqual(child_probe(self.directory), 'busy')
            raise CalDAVUncertain('Результат не подтверждён.')

        with patch.object(self.calendar, 'read', side_effect=read), \
                patch.object(self.calendar, 'write', side_effect=write):
            self.assertEqual(self.post(self.path, fields).status_code, 400)
        self.assertEqual(child_probe(self.directory), 'acquired')
        self.assertEqual(self.store.read(self.token)['state'], 'uncertain')

    def test_lock_filesystem_failure_is_safe_and_leaves_token_ready(self):
        self.login_only()
        with patch('calendar_digest.calendar_operation_lock.os.open',
                   side_effect=PermissionError('private-state-path')):
            response = self.get(self.path)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-state-path', response.get_data(as_text=True))
        self.assertEqual(self.calendar.reads, 0)
        self.assertEqual(self.store.read(self.token)['state'], 'ready')
        self.assertEqual(self.get(self.path).status_code, 200)


if __name__ == '__main__':
    unittest.main()
