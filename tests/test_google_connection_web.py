from dataclasses import asdict
from html.parser import HTMLParser
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from calendar_digest.action_web import create_app
from calendar_digest.event_actions import EventTarget
from calendar_digest import google_auth as auth


CLIENT = "synthetic-client.apps.googleusercontent.com"
SECRET = "synthetic-client-secret"
EMAIL = "owner@example.invalid"
PUBLIC = "https://digest.example"


class Inputs(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.values = {}
        self.feed(response.get_data(as_text=True))

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        if tag == "input" and "name" in attributes:
            self.values[attributes["name"]] = attributes.get("value", "")


class GoogleConnectionWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GOOGLE_")}
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app({"TESTING": True, "STATE_DIR": self.temporary.name,
                               "SECRET_KEY": "signing-secret" * 4, "WEB_PASSWORD": "owner-password" * 4,
                               "PUBLIC_URL": PUBLIC})
        self.client = self.app.test_client()
        self.store = self.app.extensions["google_connection_store"]

    def get(self, path, **kwargs):
        return self.client.get(path, base_url=PUBLIC, **kwargs)

    def post(self, path, data):
        return self.client.post(path, base_url=PUBLIC, data=data)

    def login(self):
        values = Inputs(self.get("/settings/google")).values
        if values.get("operation") != "login":
            return self.get("/settings/google")
        values["password"] = "owner-password" * 4
        self.assertEqual(self.post("/settings/google", values).status_code, 303)
        return self.get("/settings/google")

    def start(self):
        values = Inputs(self.login()).values
        values.update(client_id=CLIENT, client_secret=SECRET, expected_email=EMAIL)
        response = self.post("/settings/google", values)
        self.assertEqual(response.status_code, 303)
        return response, parse_qs(urlsplit(response.location).query)

    def test_anonymous_page_does_not_show_connection_settings_or_make_requests(self):
        with patch.object(auth, "_json_request") as request:
            response = self.get("/settings/google")
        request.assert_not_called()
        self.assertIn("Пароль сервиса", response.get_data(as_text=True))
        self.assertNotIn("OAuth Client Secret", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.get("/health").json, {"status": "ok"})

    def test_login_and_connect_require_csrf_and_share_action_login_budget(self):
        values = Inputs(self.get("/settings/google")).values
        values.update(password="wrong", csrf="forged")
        self.assertEqual(self.post("/settings/google", values).status_code, 400)
        values = Inputs(self.get("/settings/google")).values
        values["password"] = "wrong"
        for unused in range(10):
            self.assertEqual(self.post("/settings/google", values).status_code, 401)
        target = EventTarget("icloud", "test", "synthetic", "2026-10-19T10:00:00+03:00", "2026-10-19T11:00:00+03:00")
        token = self.app.extensions["action_store"].issue(asdict(target), "reschedule")
        path = "/a/" + token
        values = Inputs(self.get(path)).values
        values["password"] = "owner-password" * 4
        self.assertEqual(self.post(path, values).status_code, 429)

    def test_authorization_redirect_uses_configured_origin_and_cookie_contains_no_secrets(self):
        response, query = self.start()
        self.assertEqual(urlsplit(response.location).hostname, "accounts.google.com")
        self.assertEqual(query["redirect_uri"], [PUBLIC + auth.CALLBACK_PATH])
        for secret in (SECRET, query["state"][0], "code_verifier"):
            self.assertNotIn(secret, response.headers.get("Set-Cookie", ""))
        with self.client.session_transaction(base_url=PUBLIC) as session:
            self.assertEqual(set(session), {"owner", "csrf", "_permanent"})
        response = self.client.get("/settings/google", base_url=PUBLIC, headers={"Host": "evil.example"})
        self.assertNotIn("https://evil.example", response.get_data(as_text=True))

    def test_callback_requires_same_owner_browser_and_single_use_state(self):
        unused, query = self.start()
        callback = auth.CALLBACK_PATH + "?state=" + query["state"][0] + "&code=synthetic-code"
        with patch.object(auth, "_json_request") as request:
            self.app.test_client().get(callback, base_url=PUBLIC)
        request.assert_not_called()
        replies = [{"access_token": "access-secret", "refresh_token": "refresh-secret", "token_type": "Bearer"}, {"id": EMAIL}]
        with patch.object(auth, "_json_request", side_effect=replies) as request:
            response = self.get(callback)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.location, "/settings/google")
            self.assertEqual(request.call_count, 2)
            self.get(callback)
            self.assertEqual(request.call_count, 2)
        self.assertEqual(self.store.connection()["expected_email"], EMAIL)
        with self.client.session_transaction(base_url=PUBLIC) as session:
            representation = str(dict(session))
            for secret in (SECRET, "access-secret", "refresh-secret", "synthetic-code"):
                self.assertNotIn(secret, representation)

    def test_wrong_account_or_denied_consent_does_not_activate(self):
        unused, query = self.start()
        callback = auth.CALLBACK_PATH + "?state=" + query["state"][0] + "&code=code"
        with patch.object(auth, "_json_request", side_effect=[
            {"access_token": "access", "refresh_token": "refresh", "token_type": "Bearer"},
            {"id": "other@example.invalid"},
        ]):
            self.get(callback)
        self.assertIsNone(self.store.connection())
        self.assertIn("другой аккаунт", self.get("/settings/google").get_data(as_text=True))
        unused, query = self.start()
        with patch.object(auth, "_json_request") as request:
            self.get(auth.CALLBACK_PATH + "?state=" + query["state"][0] + "&error=access_denied")
        request.assert_not_called()
        self.assertIsNone(self.store.connection())

    def test_owner_secret_is_never_prefilled_and_new_client_requires_new_secret(self):
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": CLIENT, "GOOGLE_CLIENT_SECRET": SECRET,
                                    "GOOGLE_CALENDAR_EMAIL": EMAIL}):
            page = self.login()
            self.assertNotIn(SECRET, page.get_data(as_text=True))
            values = Inputs(page).values
            values["client_id"] = "another.apps.googleusercontent.com"
            response = self.post("/settings/google", values)
            self.assertEqual(response.status_code, 400)
            self.assertIn("соответствующий Client Secret", response.get_data(as_text=True))
        self.assertIsNone(self.store.connection())

    def test_logged_in_post_still_requires_csrf_and_callback_ignores_untrusted_error(self):
        unused, query = self.start()
        self.assertEqual(self.post("/settings/google", {"operation": "connect", "csrf": "bad"}).status_code, 400)
        with patch.object(auth, "_json_request") as request:
            self.get(auth.CALLBACK_PATH + "?state=" + query["state"][0] + "&error=%3Cscript%3Eprivate-marker%3C/script%3E")
        request.assert_not_called()
        page = self.get("/settings/google").get_data(as_text=True)
        self.assertNotIn("<script>", page)
        self.assertNotIn("private-marker", page)

    def test_csp_permits_google_oauth_form_redirect_only_on_connection_page(self):
        response, query = self.start()
        self.assertIn("form-action 'self' https://accounts.google.com;", response.headers["Content-Security-Policy"])
        self.assertNotIn("accounts.google.com", self.get("/").headers["Content-Security-Policy"])

    def test_direct_refresh_token_mode_cannot_be_overridden_by_browser_oauth(self):
        unused, query = self.start()
        with patch.dict(os.environ, {"GOOGLE_REFRESH_TOKEN": "protected-refresh", "GOOGLE_CALENDAR_EMAIL": EMAIL}):
            response = self.get("/settings/google")
            self.assertIn("защищённые переменные", response.get_data(as_text=True))
            self.assertNotIn('name="client_secret"', response.get_data(as_text=True))
            with self.client.session_transaction(base_url=PUBLIC) as session:
                csrf = session["csrf"]
            self.assertEqual(self.post("/settings/google", {"csrf": csrf, "operation": "connect"}).status_code, 400)
            with patch.object(auth, "_json_request") as request:
                self.get(auth.CALLBACK_PATH + "?state=" + query["state"][0] + "&code=code")
            request.assert_not_called()
        self.assertIsNone(self.store.connection())


if __name__ == "__main__":
    unittest.main()
