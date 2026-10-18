"""Owner-only Google Calendar connection page and single-use OAuth callback."""

import hashlib
import hmac
import os
import secrets
import time

from flask import redirect, render_template_string, request, session

from .google_auth import CALLBACK_PATH, GoogleAuthError, GoogleConnectionStore


TEMPLATE = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Google Календарь · Ваш календарь</title>
<style>*{box-sizing:border-box}body{margin:0;background:#f0f3ef;color:#344c54;font:16px/1.6 Arial,Helvetica,sans-serif}
main{max-width:680px;margin:48px auto;padding:30px;background:#f8faf7;border:1px solid #dfe7df;border-top:6px solid #244f59;border-radius:14px}
h1,label,strong{color:#244f59}h1{font-size:28px;line-height:1.25}.small{font-size:13px;color:#64777b}p{overflow-wrap:anywhere}
.note{padding:14px 16px;background:#eaf0e9;border-radius:8px}.error{background:#fff0e8;color:#95473e}
label{display:block;font-size:14px;margin:16px 0}input{display:block;width:100%;padding:12px;border:1px solid #b8cbc4;border-radius:6px;background:#fff;color:#344c54;font:inherit;margin-top:6px}
button{padding:12px 18px;border:0;border-radius:6px;background:#244f59;color:#fff;font:600 15px Arial;cursor:pointer}
button:focus-visible,input:focus-visible{outline:3px solid #c39b4d;outline-offset:3px}@media(max-width:600px){main{margin:16px 10px;padding:24px 18px}}</style>
</head><body><main><h1>Подключение Google Календаря</h1>
{% if message %}<p class="note {{ 'error' if error else '' }}" role="status">{{ message }}</p>{% endif %}
{% if login %}<p>Войдите с паролем сервиса.</p><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<input type="hidden" name="operation" value="login"><label>Пароль сервиса<input type="password" name="password" autocomplete="current-password" required maxlength="256"></label>
<button type="submit">Войти</button></form>
{% else %}
{% if direct %}<p class="note">Google настроен через защищённые переменные среды. Ожидаемый аккаунт: <strong>{{ expected_email }}</strong>.</p>
<p>Чтобы перейти на подключение через эту страницу, сначала удалите GOOGLE_REFRESH_TOKEN из переменных среды сервиса.</p>
{% elif connected %}<p class="note">Подключён аккаунт <strong>{{ connected }}</strong>.</p>{% else %}<p>Аккаунт Google пока не подключён.</p>{% endif %}
{% if disabled %}<p class="note error">Чтение Google Календаря выключено. Для включения этого источника задайте GOOGLE_CALENDAR_ENABLED=true в настройках сервиса.</p>{% endif %}
{% if not direct %}
<p>Сервис получит доступ только к чтению календаря. Вход и разрешение доступа выполняются на странице Google.</p>
<p class="small">Для подключения нужен OAuth-клиент типа «Веб-приложение» в проекте Google с включённым Calendar API. Зарегистрированный адрес возврата: <strong>{{ redirect_uri }}</strong></p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="operation" value="connect">
<label>Ожидаемый аккаунт Google<input type="email" name="expected_email" value="{{ expected_email }}" autocomplete="username" maxlength="254" required {{ 'readonly' if email_fixed else '' }}></label>
<label>OAuth Client ID<input type="text" name="client_id" value="{{ client_id }}" autocomplete="off" maxlength="512" {{ '' if configuration_available else 'required' }}></label>
<label>OAuth Client Secret<input type="password" name="client_secret" autocomplete="off" maxlength="1024" {{ '' if configuration_available else 'required' }}></label>
{% if configuration_available %}<p class="small">Оставьте Client Secret пустым, чтобы использовать уже сохранённые настройки OAuth-клиента.</p>{% endif %}
<button type="submit">{{ 'Подключить заново через Google' if connected else 'Подключить через Google' }}</button></form>
<p class="small">Пароль Google здесь не вводится. Настройки подключения хранятся в зашифрованном виде на защищённом диске сервиса.</p>
{% endif %}
{% endif %}</main></body></html>'''


def register_google_connection(app, *, attempts, attempts_lock, password_digest):
    store = GoogleConnectionStore(app.config["STATE_DIR"], app.config["SECRET_KEY"])
    app.extensions["google_connection_store"] = store

    def callback_uri():
        # The configured deployment URL, never attacker-controlled Host headers.
        from .action_links import public_url
        from .action_store import StoreError
        try:
            return public_url(app.config.get("PUBLIC_URL") or os.environ.get("DIGEST_PUBLIC_URL")
                              or os.environ.get("RENDER_EXTERNAL_URL", "")) + CALLBACK_PATH
        except StoreError:
            raise GoogleAuthError("Укажите постоянный HTTPS-адрес сервиса в DIGEST_PUBLIC_URL.") from None

    def page(status=200, **values):
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return render_template_string(TEMPLATE, csrf=session["csrf"], **values), status

    def configuration(connection):
        # Never combine an entered client ID with an unrelated stored secret.
        source = connection or {}
        if os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_SECRET"):
            client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
            client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        else:
            client_id = source.get("client_id", "")
            client_secret = source.get("client_secret", "")
        email = os.environ.get("GOOGLE_CALENDAR_EMAIL") or source.get("expected_email", "")
        return client_id, client_secret, email

    @app.route("/settings/google", methods=["GET", "POST"])
    def google_settings():
        try:
            if request.method == "POST":
                supplied = request.form.get("csrf", "")
                if not supplied or not hmac.compare_digest(supplied, session.get("csrf", "")):
                    return page(400, login=not session.get("owner"), message="Не удалось проверить форму. Откройте страницу заново.", error=True)
            if not session.get("owner"):
                if request.method == "POST" and request.form.get("operation") == "login":
                    with attempts_lock:
                        now = time.monotonic()
                        attempts[:] = [moment for moment in attempts if now - moment < 60]
                        limited = len(attempts) >= 10
                        if not limited:
                            attempts.append(now)
                    if limited:
                        return page(429, login=True, message="Слишком много попыток. Подождите минуту.", error=True)
                    supplied = hashlib.sha256(request.form.get("password", "").encode()).digest()
                    if hmac.compare_digest(password_digest, supplied):
                        session.clear()
                        session.update(owner=True, csrf=secrets.token_urlsafe(32))
                        session.permanent = True
                        return redirect("/settings/google", code=303)
                    return page(401, login=True, message="Пароль не подошёл.", error=True)
                return page(login=True)
            connection = store.connection()
            client_id, client_secret, expected_email = configuration(connection)
            redirect_uri = callback_uri()
            direct = bool(os.environ.get("GOOGLE_REFRESH_TOKEN"))
            if request.method == "POST":
                if request.form.get("operation") != "connect":
                    raise GoogleAuthError("Неизвестное действие формы.")
                if direct:
                    raise GoogleAuthError("Подключение Google управляется защищёнными переменными среды. Удалите GOOGLE_REFRESH_TOKEN для подключения через эту страницу.")
                entered_id = request.form.get("client_id", "").strip()
                entered_secret = request.form.get("client_secret", "")
                if entered_id and entered_id != client_id and not entered_secret:
                    raise GoogleAuthError("Для нового OAuth Client ID укажите соответствующий Client Secret.")
                url = store.begin(entered_id or client_id, entered_secret or client_secret,
                                  request.form.get("expected_email", "").strip(),
                                  redirect_uri=redirect_uri, browser_binding=session["csrf"])
                return redirect(url, code=303)
            message = session.pop("google_message", None)
            error = session.pop("google_error", False)
            return page(connected=connection.get("expected_email") if connection else None,
                        expected_email=expected_email, client_id=client_id,
                        configuration_available=bool(client_id and client_secret),
                        redirect_uri=redirect_uri, message=message, error=error,
                        direct=direct, email_fixed=bool(os.environ.get("GOOGLE_CALENDAR_EMAIL")),
                        disabled=(os.environ.get("GOOGLE_CALENDAR_ENABLED", "").strip().casefold() in {"false", "0", "no"}
                                  or (direct and "GOOGLE_CALENDAR_ENABLED" not in os.environ)))
        except GoogleAuthError as error:
            return page(400, message=str(error), error=True, login=not session.get("owner"))
        except Exception:
            return page(503, message="Не удалось открыть защищённое подключение Google. Попробуйте позже.", error=True)

    @app.get(CALLBACK_PATH)
    def google_callback():
        if not session.get("owner"):
            return redirect("/settings/google", code=303)
        try:
            if len(request.args.getlist("state")) != 1 or len(request.args.getlist("code")) > 1:
                raise GoogleAuthError("Не удалось проверить ответ Google. Начните подключение заново.")
            flow = store.consume(request.args.get("state", ""), browser_binding=session.get("csrf", ""),
                                 redirect_uri=callback_uri())
            if os.environ.get("GOOGLE_REFRESH_TOKEN"):
                raise GoogleAuthError("Подключение Google управляется защищёнными переменными среды. Подключение через браузер не активировано.")
            if "error" in request.args:
                raise GoogleAuthError("Разрешение на чтение календаря не получено. Подключение не изменено.")
            store.complete(flow, request.args.get("code", ""))
            session["google_message"] = "Аккаунт проверен. Google Календарь подключён для дайджеста и действующих правил push."
            session["google_error"] = False
        except GoogleAuthError as error:
            session["google_message"], session["google_error"] = str(error), True
        except Exception:
            session["google_message"] = "Не удалось завершить подключение Google. Попробуйте заново."
            session["google_error"] = True
        # Strip the short-lived authorization code from subsequent navigation.
        return redirect("/settings/google", code=303)
