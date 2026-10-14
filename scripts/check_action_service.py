#!/usr/bin/env python3
"""Start Gunicorn and exercise HTTP readiness without modifying calendars.

Run with .venv/bin/python. Test secrets are generated in memory and discarded.
No public links are created; the internal listener stops before this exits.
"""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

from calendar_digest.action_store import ActionStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', action='store_true', help='Exercise the Render supervisor with scheduling disabled')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix='digest-smoke-') as directory:
        key, password = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
        store = ActionStore(Path(directory) / 'actions.sqlite3', key)
        token = store.issue({'source_id': 'icloud', 'calendar_id': 'synthetic', 'uid': 'synthetic',
                             'expected_start': '2026-10-14', 'expected_end': '2026-10-15',
                             'recurrence_id': None, 'expected_event_status': 'CONFIRMED',
                             'expected_participation': 'UNKNOWN'}, 'cancel')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        env = dict(os.environ, DIGEST_ACTION_SECRET=key, DIGEST_WEB_PASSWORD=password,
                   DIGEST_STATE_DIR=directory, DIGEST_SCHEDULE_ENABLED='false', PORT=str(port))
        command = [
            str(root / '.venv/bin/gunicorn'), 'calendar_digest.action_web:create_app()',
            '--bind', f'127.0.0.1:{port}', '--workers', '1', '--threads', '4', '--timeout', '90',
            '--access-logfile', '/dev/null', '--error-logfile', '/dev/null',
        ]
        if args.runtime:
            command = [str(root / '.venv/bin/python'), str(root / 'scripts/start_service.py')]
        process = subprocess.Popen(command, cwd=root, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f'http://127.0.0.1:{port}'
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urlopen(base + '/health', timeout=1) as response:
                        assert response.status == 200 and b'"ok"' in response.read()
                    break
                except OSError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        raise RuntimeError('Gunicorn health check failed') from None
                    time.sleep(0.15)
            with urlopen(base + '/a/' + token, timeout=2) as response:
                body = response.read().decode()
                assert 'Пароль сервиса' in body
                assert 'Синтетическая' not in body
                assert response.headers['Cache-Control'] == 'no-store'
                assert 'Secure' in response.headers['Set-Cookie']
            try:
                urlopen(base + '/a/invalid', timeout=2)
                raise AssertionError('Unsigned action link was accepted')
            except HTTPError as error:
                assert error.code == 400
            assert store.read(token)['state'] == 'ready'
            print('Gunicorn: health, protected action page, invalid-link rejection, GET does not consume action — passed.')
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
