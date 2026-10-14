"""Owner-authenticated confirmation pages for single-use calendar actions."""

from datetime import date, datetime, timedelta
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import threading
import time

from flask import Flask, redirect, render_template_string, request, session
from werkzeug.exceptions import HTTPException

from .action_store import ActionStore, StoreError
from .event_actions import EventTarget, SafeActionError, ActionConflict, prepare_change, verify_change
from .selection import MOSCOW
from .writable_caldav import (
    CalDAVActionBackend, CalDAVActionError, CalDAVConflict, CalDAVUncertain,
    validate_change_capability,
)


TEMPLATE = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{{ heading }} · Ваш календарь</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f0f3ef;color:#344c54;font:16px/1.6 Arial,Helvetica,sans-serif}
main{max-width:640px;margin:48px auto;padding:30px;background:#f8faf7;border:1px solid #dfe7df;border-top:6px solid #244f59;border-radius:14px}
h1,h2,label,strong{color:#244f59}h1{font-size:28px;line-height:1.25;margin:16px 0}h2{font-size:20px;font-weight:600;overflow-wrap:anywhere}
p{margin:12px 0;overflow-wrap:anywhere}.kicker{font-size:11px;letter-spacing:2px;color:#718188}.small{font-size:13px;color:#64777b}
.note{padding:14px 16px;background:#eaf0e9;border-radius:8px}.error{background:#fff0e8;color:#95473e}.result{border-left:4px solid #3b6858}
label{display:block;font-size:14px;margin:16px 0}input{display:block;width:100%;padding:12px;border:1px solid #b8cbc4;border-radius:6px;background:#fff;color:#344c54;font:inherit;margin-top:6px;min-width:0}
button{padding:12px 18px;border:0;border-radius:6px;background:#244f59;color:#fff;font:600 15px Arial;cursor:pointer;margin-top:12px}
button:focus-visible,input:focus-visible{outline:3px solid #c39b4d;outline-offset:3px}.cancel{background:#95473e}.dates{display:flex;gap:14px}.dates label{flex:1;min-width:0}
@media(max-width:600px){main{margin:16px 10px;padding:24px 18px}h1{font-size:25px}.dates{display:block}}
</style></head><body><main><div class="kicker">ВАШ КАЛЕНДАРЬ</div><h1>{{ heading }}</h1>
{% if message %}<p class="note {{ 'error' if error else 'result' }}" role="status">{{ message }}</p>{% endif %}
{% if login %}<p>Войдите, чтобы управлять событиями из дайджеста.</p><form method="post">
<input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="operation" value="login">
<label>Пароль сервиса<input type="password" name="password" autocomplete="current-password" required maxlength="256"></label>
<button type="submit">Войти</button></form>{% endif %}
{% if change %}<h2>{{ change.title }}</h2><p class="small">{{ calendar_name }} · Московское время</p>
<p class="note">Сейчас: <strong>{{ before }}</strong></p>
<p>{{ description }}</p>{% if recurring %}<p class="small">Изменится только выбранное повторение. Остальные даты серии сохранятся.</p>{% endif %}
{% if scheduling %}<p class="small">Календарь поддерживает автоматические уведомления. Участники могут получить обновление от провайдера календаря.</p>{% endif %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="operation" value="apply">
<input type="hidden" name="etag" value="{{ etag }}">
{% if reschedule %}<div class="dates"><label>{{ 'Первый день' if all_day else 'Новое начало' }}<input type="{{ 'date' if all_day else 'datetime-local' }}" name="new_start" value="{{ start_value }}" required></label>
<label>{{ 'Последний день (включительно)' if all_day else 'Новое окончание' }}<input type="{{ 'date' if all_day else 'datetime-local' }}" name="new_end" value="{{ end_value }}" required></label></div>{% endif %}
<button type="submit" class="{{ 'cancel' if cancel else '' }}">{{ button }}</button></form>
<p class="small">Изменение сохранится после нажатия этой кнопки.</p>{% endif %}
{% if result %}<h2>{{ result.title }}</h2><p>Было: {{ result.before }}</p><p><strong>Стало: {{ result.after }}</strong></p>{% endif %}
{% if not login and not change %}<p class="small">Можно закрыть эту страницу и вернуться к дайджесту.</p>{% endif %}
</main></body></html>'''

OPERATIONS = {
    'confirm_event': ('Подтвердить событие', 'В календаре будет установлен статус «Подтверждено».', 'Подтвердить событие'),
    'accept_invitation': ('Принять приглашение', 'Ваш ответ на приглашение изменится на «Принято».', 'Принять приглашение'),
    'decline_participation': ('Отклонить участие', 'Ваш ответ изменится на «Отклонено».', 'Отклонить участие'),
    'cancel_event': ('Отменить событие', 'Событие получит статус «Отменено» и останется в календаре.', 'Отменить событие'),
    'reschedule': ('Перенести событие', 'Выберите новую дату и время события.', 'Сохранить новое время'),
}


def _interval(start, end):
    if 'T' not in start:
        first, last = date.fromisoformat(start), date.fromisoformat(end) - timedelta(days=1)
        return f'{first:%d.%m.%Y} · весь день' if last <= first else f'{first:%d.%m.%Y} — {last:%d.%m.%Y} · весь день'
    first, last = datetime.fromisoformat(start).astimezone(MOSCOW), datetime.fromisoformat(end).astimezone(MOSCOW)
    return f'{first:%d.%m.%Y %H:%M} — {last:%H:%M}' if first.date() == last.date() else f'{first:%d.%m.%Y %H:%M} — {last:%d.%m.%Y %H:%M}'


def _new_interval(target):
    try:
        start, end = request.form['new_start'], request.form['new_end']
        if 'T' not in target.expected_start:
            return date.fromisoformat(start).isoformat(), (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        values = [datetime.fromisoformat(value) for value in (start, end)]
        if any(value.tzinfo is not None for value in values):
            raise ValueError
        return tuple(value.replace(tzinfo=MOSCOW).isoformat() for value in values)
    except (ValueError, KeyError):
        raise SafeActionError('Укажите корректные начало и окончание по московскому времени.') from None


def create_app(config=None, backend=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get('DIGEST_ACTION_SECRET', ''),
        WEB_PASSWORD=os.environ.get('DIGEST_WEB_PASSWORD', ''),
        STATE_DIR=os.environ.get('DIGEST_STATE_DIR', '.local'),
        SESSION_COOKIE_NAME='digest_owner', SESSION_COOKIE_SECURE=True,
        # Lax retains the login on top-level GET navigation from a webmail link;
        # every write still needs the per-session CSRF token and explicit POST.
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12), MAX_CONTENT_LENGTH=8192,
    )
    if config:
        app.config.update(config)
    if len(app.config['SECRET_KEY']) < 32 or len(app.config['WEB_PASSWORD']) < 20:
        raise RuntimeError('Требуются DIGEST_ACTION_SECRET (от 32 символов) и DIGEST_WEB_PASSWORD (от 20 символов).')
    store = ActionStore(Path(app.config['STATE_DIR']) / 'actions.sqlite3', app.config['SECRET_KEY'])
    calendar = backend or CalDAVActionBackend()
    app.extensions['action_store'] = store
    attempts = []
    attempts_lock = threading.Lock()
    password_digest = hashlib.sha256(app.config['WEB_PASSWORD'].encode()).digest()

    def page(status=200, **values):
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        return render_template_string(TEMPLATE, csrf=session['csrf'], **values), status

    def terminal(record):
        state = record['state']
        result = record.get('result') or {}
        messages = {'running': 'Действие уже выполняется. Обновите страницу немного позже.',
                    'uncertain': 'Результат записи не подтверждён. Проверьте календарь перед новым действием.',
                    'failed': 'Изменение не подтверждено. Обновите дайджест перед повторной попыткой.'}
        return page(heading='Изменение сохранено' if state == 'done' else 'Состояние действия',
                    message=result.get('message') or messages.get(state), result=result if state == 'done' else None,
                    error=state in {'failed', 'uncertain'})

    @app.after_request
    def secure_headers(response):
        response.headers.update({
            'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Strict-Transport-Security': 'max-age=31536000',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        })
        return response

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.get('/')
    def home():
        return page(heading='Ваш календарь',
                    message='Сервис действий готов. Откройте кнопку у нужного события в дайджесте, чтобы войти и выполнить действие.')

    @app.route('/a/<token>', methods=['GET', 'POST'])
    def action_page(token):
        claimed = False
        write_started = False
        try:
            # Signed, unexpired server-side target; never use IDs from form fields.
            record = store.read(token)
            if request.method == 'POST':
                csrf = request.form.get('csrf', '')
                if not csrf or not hmac.compare_digest(csrf, session.get('csrf', '')):
                    return page(400, heading='Откройте ссылку заново', message='Не удалось проверить форму.', error=True)
            if not session.get('owner'):
                if request.method == 'POST' and request.form.get('operation') == 'login':
                    # One owner, one process. Strong generated password plus a bounded global attempt budget.
                    with attempts_lock:
                        now = time.monotonic()
                        attempts[:] = [moment for moment in attempts if now - moment < 60]
                        limited = len(attempts) >= 10
                        if not limited:
                            attempts.append(now)
                    if limited:
                        return page(429, heading='Вход', message='Слишком много попыток. Подождите минуту.', login=True, error=True)
                    supplied = hashlib.sha256(request.form.get('password', '').encode()).digest()
                    if hmac.compare_digest(password_digest, supplied):
                        session.clear()
                        session.update(owner=True, csrf=secrets.token_urlsafe(32))
                        session.permanent = True
                        return redirect(request.path, code=303)
                    return page(401, heading='Вход', message='Пароль не подошёл.', login=True, error=True)
                return page(heading='Вход в календарь', login=True)
            if record['state'] != 'ready':
                return terminal(record)
            target = EventTarget(**record['target'])
            version = calendar.read(target)
            if version.can_write is not True:
                raise SafeActionError('Право изменения этого календаря не подтверждено.')
            values = (target.expected_start, target.expected_end) if record['action'] == 'reschedule' else (None, None)
            if request.method == 'POST':
                if request.form.get('operation') != 'apply':
                    raise SafeActionError('Неизвестное действие формы.')
                if request.form.get('etag') != version.etag:
                    raise ActionConflict('Событие изменилось после открытия формы. Обновите дайджест.')
                if record['action'] == 'reschedule':
                    values = _new_interval(target)
            change = prepare_change(version.data, target, record['action'], version.account_email,
                                    *values, allow_organizer_updates=version.can_organize)
            validate_change_capability(version, change)
            if request.method == 'GET':
                heading, description, button = OPERATIONS[change.operation]
                all_day = 'T' not in target.expected_start
                start = target.expected_start if all_day else datetime.fromisoformat(target.expected_start).astimezone(MOSCOW).strftime('%Y-%m-%dT%H:%M')
                end = (date.fromisoformat(target.expected_end) - timedelta(days=1)).isoformat() if all_day else datetime.fromisoformat(target.expected_end).astimezone(MOSCOW).strftime('%Y-%m-%dT%H:%M')
                return page(heading=heading, change=change, description=description, button=button,
                            before=_interval(change.before_start, change.before_end), calendar_name=version.calendar_name,
                            recurring=bool(target.recurrence_id), etag=version.etag,
                            all_day=all_day, start_value=start, end_value=end,
                            reschedule=record['action'] == 'reschedule', cancel=record['action'] == 'cancel',
                            scheduling=version.automatic_scheduling and ('ATTENDEE' in version.data))
            if not store.claim(token):
                return terminal(store.read(token))
            claimed = True
            write_started = True
            stored = calendar.write(target, version, change.data,
                verify=lambda actual: verify_change(actual, target, change, version.account_email))
            if not verify_change(stored.data, target, change, version.account_email):
                raise CalDAVUncertain('Результат записи не подтверждён. Проверьте календарь.')
            result = {'message': change.summary, 'title': change.title,
                      'before': _interval(change.before_start, change.before_end),
                      'after': _interval(change.after_start, change.after_end), 'operation': change.operation}
            store.complete(token, result)
            return redirect(request.path, code=303)
        except (StoreError, SafeActionError, CalDAVActionError) as exc:
            uncertain = isinstance(exc, CalDAVUncertain) or (write_started and isinstance(exc, StoreError))
            message = ('Результат операции не удалось сохранить. Проверьте календарь перед новым действием.'
                       if write_started and isinstance(exc, StoreError) else str(exc))
            if claimed:
                try:
                    store.fail(token, {'message': message}, uncertain=uncertain)
                except StoreError:
                    # Keep running/terminal state if the receipt database failed;
                    # neither state permits an automatic repeat of this action.
                    pass
            status = 409 if isinstance(exc, (ActionConflict, CalDAVConflict)) else 400
            return page(status, heading='Изменение не подтверждено', message=message, error=True)
        except Exception:
            # A crash after claiming can mean that PUT succeeded: never retry automatically.
            if claimed:
                try:
                    store.fail(token, {'message': 'Результат операции не подтверждён. Проверьте календарь.'}, uncertain=True)
                except Exception:
                    pass
            return page(503, heading='Не удалось завершить действие',
                        message='Проверьте календарь перед повторной попыткой.' if claimed else 'Сервис временно недоступен. Попробуйте позже.', error=True)

    @app.errorhandler(HTTPException)
    def http_error(error):
        return page(error.code, heading='Страница недоступна', message='Откройте ссылку из актуального дайджеста.', error=True)

    return app
