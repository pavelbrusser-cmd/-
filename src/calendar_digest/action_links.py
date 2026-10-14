"""Issue opaque action links into a privately saved digest; never send email."""

from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from .action_store import ActionStore, StoreError
from .email_html import render_email_html
from .event_actions import EventTarget
from .local_state import write_private
from .presentation import actions_for
from .selection import DigestEvent


def public_url(value):
    try:
        url = urlsplit(value)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.port not in (None, 443) or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError
        hostname = url.hostname.lower()
        if (len(hostname) > 253 or hostname.endswith(('.local', '.localhost'))
            or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})', hostname)):
            raise ValueError
    except (ValueError, TypeError):
        raise StoreError('Задайте постоянный публичный HTTPS-адрес в DIGEST_PUBLIC_URL.') from None
    return value.rstrip('/')


def issue_email(day: date, directory: Path = Path('.local'), *, state_dir=None, base_url=None, signing_key=None):
    base = public_url(base_url or os.environ.get('DIGEST_PUBLIC_URL') or os.environ.get('RENDER_EXTERNAL_URL', ''))
    key = signing_key or os.environ.get('DIGEST_ACTION_SECRET', '')
    store = ActionStore(Path(state_dir or os.environ.get('DIGEST_STATE_DIR', directory)) / 'actions.sqlite3', key)
    try:
        payload = json.loads((directory / f'preview-{day.isoformat()}.json').read_text(encoding='utf-8'))
        if payload['date'] != day.isoformat() or payload['timezone'] != 'Europe/Moscow':
            raise ValueError
        events = [DigestEvent(**item) for item in payload['events']]
    except Exception:
        raise StoreError('Сначала соберите образец дайджеста за выбранную дату.') from None
    links = {}
    for index, event in enumerate(events):
        target = EventTarget(event.source_id, event.calendar_id, event.uid, event.start, event.end,
                             event.recurrence_id, event.event_status, event.participation)
        for action in actions_for(event):
            token = store.issue(asdict(target), action)
            links[index, action] = f'{base}/a/{token}'
    html = render_email_html(events, day, first_snapshot=payload.get('first_snapshot', False), action_links=links)
    output = directory / f'email-actions-{day.isoformat()}.html'
    write_private(output, html)
    return {'date': day.isoformat(), 'events': len(events), 'action_links': len(links),
            'email_html': str(output), 'sent': False}
