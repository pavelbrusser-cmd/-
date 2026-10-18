"""Safe, actionable distinctions at the OAuth and Calendar HTTP boundaries."""

import json
import os
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from calendar_digest import google_auth as auth
from calendar_digest.action_web import create_app


CLIENT = "synthetic-client.apps.googleusercontent.com"
SECRET = "private-client-secret-marker"
REFRESH = "private-refresh-token-marker"
ACCESS = "private-access-token-marker"
EMAIL = "owner@example.invalid"
PUBLIC = "https://digest.example"
PRIVATE = "private-provider-message-marker"
PROJECT = "private-project-id-marker-123456789"
CODE = "private-authorization-code-marker"


def connection(status, body):
    result = MagicMock()
    response = result.getresponse.return_value
    response.status = status
    response.read.return_value = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
    return result


class GoogleAuthHTTPErrorTests(unittest.TestCase):
    def failure(self, status, body, *, calendar=False, grant_type="authorization_code"):
        transport = connection(status, body)
        kwargs = ({"access_token": ACCESS} if calendar else {
            "form": {"grant_type": grant_type, "client_id": CLIENT,
                     "client_secret": SECRET, "code": CODE, "refresh_token": REFRESH},
        })
        with patch.object(auth.http.client, "HTTPSConnection", return_value=transport) as factory:
            with self.assertRaises(auth.GoogleAuthError) as raised:
                auth._json_request("www.googleapis.com" if calendar else "oauth2.googleapis.com",
                                   "/calendar/v3/calendars/primary" if calendar else "/token", **kwargs)
        factory.assert_called_once()
        transport.request.assert_called_once()
        transport.close.assert_called_once()
        transport.getresponse.return_value.read.assert_called_once_with(auth._MAX_BYTES + 1)
        formatted = "".join(traceback.format_exception(raised.exception))
        for private in (SECRET, REFRESH, ACCESS, PRIVATE, PROJECT, CODE, "<script>"):
            self.assertNotIn(private, str(raised.exception))
            self.assertNotIn(private, repr(raised.exception))
            self.assertNotIn(private, formatted)
        return str(raised.exception)

    def test_oauth_error_codes_are_whitelisted_and_response_fields_never_echoed(self):
        examples = [
            (401, "invalid_client", "GOOGLE_OAUTH_INVALID_CLIENT"),
            (400, "invalid_grant", "GOOGLE_OAUTH_INVALID_GRANT"),
            (400, "redirect_uri_mismatch", "GOOGLE_OAUTH_REDIRECT"),
            (400, "invalid_scope", "GOOGLE_OAUTH_SCOPE"),
            (403, "access_denied", "GOOGLE_OAUTH_DENIED"),
        ]
        for status, reason, expected in examples:
            with self.subTest(reason=reason):
                message = self.failure(status, {
                    "error": reason, "error_description": PRIVATE + " " + SECRET,
                    "error_uri": "https://untrusted.invalid/" + PROJECT,
                    "message": REFRESH, "details": {"project": PROJECT, "access_token": ACCESS},
                })
                self.assertIn("[" + expected + "]", message)
                self.assertNotIn("GOOGLE_CALENDAR_", message)

    def test_expired_refresh_grant_requires_reconnect_instead_of_reusing_code(self):
        message = self.failure(400, {"error": "invalid_grant", "error_description": PRIVATE},
                               grant_type="refresh_token")
        self.assertIn("[GOOGLE_OAUTH_RECONNECT]", message)
        self.assertNotIn("GOOGLE_OAUTH_INVALID_GRANT", message)

    def test_token_throttling_and_server_errors_have_retry_classification(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status=status):
                self.assertIn("[GOOGLE_OAUTH_RETRY]", self.failure(status, {
                    "error": "server_error", "error_description": PRIVATE,
                }))

    def test_calendar_api_disabled_supports_legacy_and_modern_errorinfo(self):
        errors = [
            {"code": 403, "message": PRIVATE, "errors": [
                {"domain": "usageLimits", "reason": "accessNotConfigured", "message": PROJECT},
            ]},
            {"code": 403, "status": "PERMISSION_DENIED", "message": PRIVATE, "details": [
                {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                 "domain": "googleapis.com", "metadata": {
                     "consumer": PROJECT, "service": "calendar-json.googleapis.com",
                     "activationUrl": "https://untrusted.invalid/" + SECRET,
                 }},
            ]},
        ]
        for body in errors:
            with self.subTest(body_type="details" if "details" in body else "errors"):
                self.assertIn("[GOOGLE_CALENDAR_API_DISABLED]", self.failure(403, {"error": body}, calendar=True))

    def test_calendar_missing_scope_is_distinct_from_api_disabled(self):
        for fields in ({"errors": [{"reason": "insufficientPermissions", "message": PRIVATE}]},
                       {"details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                     "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                                     "metadata": {"project": PROJECT}}]}):
            message = self.failure(403, {"error": {"message": PRIVATE, **fields}}, calendar=True)
            self.assertIn("[GOOGLE_CALENDAR_SCOPE]", message)
            self.assertNotIn("GOOGLE_CALENDAR_API_DISABLED", message)

    def test_calendar_http_auth_missing_calendar_and_retry_have_distinct_codes(self):
        for status, expected in ((401, "AUTH"), (404, "NOT_FOUND"), (429, "RETRY"),
                                 (500, "RETRY"), (503, "RETRY")):
            with self.subTest(status=status):
                message = self.failure(status, {"error": {"message": PRIVATE}}, calendar=True)
                self.assertIn("[GOOGLE_CALENDAR_" + expected + "]", message)
        self.assertIn("[GOOGLE_CALENDAR_RETRY]", self.failure(403, {
            "error": {"message": PRIVATE, "errors": [{"reason": "rateLimitExceeded", "message": PROJECT}]},
        }, calendar=True))

    def test_unknown_malformed_and_html_errors_keep_stage_without_private_response(self):
        bodies = [
            {"error": PRIVATE, "error_description": SECRET},
            {"error": {"message": PRIVATE, "errors": [{"reason": PROJECT}], "details": ACCESS}},
            {"error": {"message": PRIVATE, "errors": [None, PRIVATE], "details": [None, 5]}},
            {"error": [PRIVATE]},
            [PRIVATE],
            ("<html><script>" + PRIVATE + "</script>" + SECRET + "</html>").encode(),
            ("{invalid-json-" + PRIVATE).encode(),
            b"\xff" + PRIVATE.encode(),
        ]
        for calendar in (False, True):
            for body in bodies:
                with self.subTest(calendar=calendar, body_kind=type(body).__name__):
                    message = self.failure(403, body, calendar=calendar)
                    self.assertIn("[GOOGLE_CALENDAR_HTTP]" if calendar else "[GOOGLE_OAUTH_HTTP]", message)

    def test_error_descriptions_cannot_spoof_a_whitelisted_reason(self):
        message = self.failure(403, {
            "error": {"message": "SERVICE_DISABLED " + PRIVATE,
                      "errors": [{"reason": PROJECT, "message": "accessNotConfigured " + SECRET}],
                      "details": [{"metadata": {"reason": "SERVICE_DISABLED", "project": PROJECT}}]},
        }, calendar=True)
        self.assertIn("[GOOGLE_CALENDAR_HTTP]", message)
        self.assertNotIn("GOOGLE_CALENDAR_API_DISABLED", message)

    def test_redirects_are_not_followed_or_misreported_as_authorization_errors(self):
        for calendar in (False, True):
            message = self.failure(302, b"<html>" + PRIVATE.encode() + b"</html>", calendar=calendar)
            self.assertIn("[GOOGLE_CALENDAR_HTTP]" if calendar else "[GOOGLE_OAUTH_HTTP]", message)

    def test_oversized_responses_are_bounded_before_json_parsing(self):
        for status in (200, 403):
            with self.subTest(status=status):
                raw = (PRIVATE + " ").encode() + b"x" * (auth._MAX_BYTES + 1)
                with patch.object(auth.json, "loads", side_effect=AssertionError("must not parse oversized response")):
                    message = self.failure(status, raw, calendar=True)
                self.assertIn("[GOOGLE_RESPONSE_TOO_LARGE]", message)


class GoogleOAuthFailureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GOOGLE_")}
        environment.update(DIGEST_STATE_DIR=str(self.directory), DIGEST_ACTION_SECRET="test-owner-secret" * 3)
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app({"TESTING": True, "STATE_DIR": str(self.directory),
                               "SECRET_KEY": os.environ["DIGEST_ACTION_SECRET"],
                               "WEB_PASSWORD": "test-owner-password" * 3, "PUBLIC_URL": PUBLIC})
        self.store = self.app.extensions["google_connection_store"]
        self.client = self.app.test_client()
        with self.client.session_transaction(base_url=PUBLIC) as session:
            session.update(owner=True, csrf="browser-binding")

    def begin(self):
        url = self.store.begin(CLIENT, SECRET, EMAIL, redirect_uri=PUBLIC + auth.CALLBACK_PATH,
                               browser_binding="browser-binding")
        return parse_qs(urlsplit(url).query)["state"][0]

    def activate(self):
        flow = self.store.consume(self.begin(), redirect_uri=PUBLIC + auth.CALLBACK_PATH,
                                  browser_binding="browser-binding")
        with patch.object(auth, "_json_request", side_effect=[
            {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer"}, {"id": EMAIL},
        ]):
            self.store.complete(flow, CODE)
        return self.store.connection()

    def test_failed_reconnection_preserves_existing_verified_connection(self):
        before = self.activate()
        flow = self.store.consume(self.begin(), redirect_uri=PUBLIC + auth.CALLBACK_PATH,
                                  browser_binding="browser-binding")
        transports = [
            connection(200, {"access_token": ACCESS, "refresh_token": "replacement-token", "token_type": "Bearer"}),
            connection(403, {"error": {"message": PRIVATE, "errors": [{"reason": "accessNotConfigured"}]}}),
        ]
        with patch.object(auth.http.client, "HTTPSConnection", side_effect=transports):
            with self.assertRaisesRegex(auth.GoogleAuthError, "GOOGLE_CALENDAR_API_DISABLED"):
                self.store.complete(flow, CODE)
        self.assertEqual(self.store.connection(), before)
        self.assertTrue(auth.google_enabled())

    def test_callback_shows_safe_stage_error_without_activation_or_code_reuse(self):
        state = self.begin()
        callback = auth.CALLBACK_PATH + "?" + urlencode({"state": state, "code": CODE})
        transports = [
            connection(200, {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "Bearer"}),
            connection(403, {"error": {"message": PRIVATE + " " + PROJECT,
                                       "errors": [{"reason": "accessNotConfigured", "message": SECRET}]}}),
        ]
        with patch.object(auth.http.client, "HTTPSConnection", side_effect=transports) as factory:
            response = self.client.get(callback, base_url=PUBLIC)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.location, "/settings/google")
            self.assertEqual(factory.call_count, 2)
            with self.client.session_transaction(base_url=PUBLIC) as session:
                message = session["google_message"]
                self.assertIn("[GOOGLE_CALENDAR_API_DISABLED]", message)
                for private in (SECRET, REFRESH, ACCESS, PRIVATE, PROJECT, CODE):
                    self.assertNotIn(private, str(dict(session)))
            page = self.client.get("/settings/google", base_url=PUBLIC).get_data(as_text=True)
            self.assertIn("[GOOGLE_CALENDAR_API_DISABLED]", page)
            for private in (SECRET, REFRESH, ACCESS, PRIVATE, PROJECT, CODE):
                self.assertNotIn(private, page)
            self.assertIsNone(self.store.connection())
            self.assertFalse(auth.google_enabled())
            self.client.get(callback, base_url=PUBLIC)
            self.assertEqual(factory.call_count, 2)
        replacement_state = self.begin()
        self.assertNotEqual(replacement_state, state)
        self.assertIsNone(self.store.connection())


if __name__ == "__main__":
    unittest.main()
