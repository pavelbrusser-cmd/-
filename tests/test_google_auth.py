from pathlib import Path
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from calendar_digest import google_auth as auth


CLIENT = "synthetic-client.apps.googleusercontent.com"
SECRET = "synthetic-client-secret"
REFRESH = "synthetic-refresh-token"
EMAIL = "owner@example.invalid"
CALLBACK = "https://digest.example/settings/google/callback"
KEY = "synthetic-signing-key" * 3


class GoogleAuthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GOOGLE_")}
        environment.update(DIGEST_STATE_DIR=str(self.directory), DIGEST_ACTION_SECRET=KEY)
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = auth.GoogleConnectionStore()

    def begin(self, binding="browser"):
        url = self.store.begin(CLIENT, SECRET, EMAIL, redirect_uri=CALLBACK, browser_binding=binding)
        return parse_qs(urlsplit(url).query)

    def consume(self, query):
        return self.store.consume(query["state"][0], redirect_uri=CALLBACK, browser_binding="browser")

    def complete(self, flow, *, email=EMAIL, refresh=REFRESH):
        result = {"access_token": "access-secret", "refresh_token": refresh, "token_type": "Bearer", "scope": auth.SCOPE}
        with patch.object(auth, "_json_request", side_effect=[result, {"id": email}]) as request:
            self.store.complete(flow, "single-use-code")
        return request

    def test_disabled_installation_needs_no_google_settings_or_disk_creation(self):
        self.assertFalse(auth.google_enabled())
        self.assertFalse((self.directory / "google").exists())
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_ENABLED": "true"}):
            self.assertTrue(auth.google_enabled())
            with self.assertRaises(auth.GoogleAuthError):
                auth.GoogleCredentials.from_environment()
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_ENABLED": "maybe"}):
            with self.assertRaises(auth.GoogleAuthError):
                auth.google_enabled()

    def test_pkce_offline_readonly_state_and_private_storage(self):
        query = self.begin()
        self.assertEqual(query["scope"], [auth.SCOPE])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["redirect_uri"], [CALLBACK])
        self.assertEqual(len(query["code_challenge"][0]), 43)
        self.assertNotIn("client_secret", query)
        self.assertFalse(auth.google_enabled())
        content = (self.directory / "google" / "connection.enc").read_bytes()
        for private in (SECRET, EMAIL, query["state"][0], "browser"):
            self.assertNotIn(private.encode(), content)
        self.assertEqual(stat.S_IMODE((self.directory / "google").stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.directory / "google" / "connection.enc").stat().st_mode), 0o600)

    def test_state_wrong_browser_redirect_expiry_and_replay_blocked(self):
        query = self.begin()
        for binding, uri in (("other", CALLBACK), ("browser", "https://evil.example/callback")):
            with self.assertRaises(auth.GoogleAuthError):
                self.store.consume(query["state"][0], browser_binding=binding, redirect_uri=uri)
        self.consume(query)
        with self.assertRaises(auth.GoogleAuthError):
            self.consume(query)
        query = self.begin()
        with patch.object(auth.time, "time", return_value=auth.time.time() + 601):
            with self.assertRaises(auth.GoogleAuthError):
                self.consume(query)

    def test_only_verified_expected_account_activates_and_survives_restart(self):
        query = self.begin()
        flow = self.consume(query)
        with self.assertRaisesRegex(auth.GoogleAuthError, "другой аккаунт"):
            self.complete(flow, email="another@example.invalid")
        self.assertFalse(auth.google_enabled())
        request = self.complete(self.consume(self.begin()), email=EMAIL.upper())
        self.assertEqual(request.call_args_list[0].args, ("oauth2.googleapis.com", "/token"))
        self.assertEqual(request.call_args_list[1].args, ("www.googleapis.com", "/calendar/v3/calendars/primary"))
        self.assertTrue(auth.google_enabled())
        restarted = auth.GoogleCredentials.from_environment()
        self.assertEqual(restarted.expected_email, EMAIL)
        for secret in (SECRET, REFRESH, CLIENT):
            self.assertNotIn(secret, repr(restarted))
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_ENABLED": "false"}):
            self.assertFalse(auth.google_enabled())

    def test_missing_offline_grant_or_readonly_scope_cannot_activate(self):
        for result in ({"access_token": "access", "token_type": "Bearer"},
                       {"access_token": "access", "token_type": "Bearer", "refresh_token": REFRESH, "scope": "openid"}):
            flow = self.consume(self.begin())
            with patch.object(auth, "_json_request", return_value=result):
                with self.assertRaises(auth.GoogleAuthError):
                    self.store.complete(flow, "code")
            self.assertFalse(auth.google_enabled())

    def test_failed_or_superseded_reconnection_preserves_working_account(self):
        self.complete(self.consume(self.begin()))
        old = self.consume(self.begin())
        self.begin()
        with self.assertRaises(auth.GoogleAuthError):
            self.complete(old, refresh="new-refresh")
        self.assertEqual(self.store.connection()["refresh_token"], REFRESH)

    def test_environment_account_is_enforced_before_authorization_and_callback(self):
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_EMAIL": "other@example.invalid"}):
            with self.assertRaises(auth.GoogleAuthError):
                self.begin()
        flow = self.consume(self.begin())
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_EMAIL": "other@example.invalid"}):
            with patch.object(auth, "_json_request") as request:
                with self.assertRaises(auth.GoogleAuthError):
                    self.store.complete(flow, "code")
            request.assert_not_called()
        self.assertIsNone(self.store.connection())

    def test_refresh_rotation_saved_and_environment_account_cannot_silently_change(self):
        self.complete(self.consume(self.begin()))
        credential = auth.GoogleCredentials.from_environment()
        with patch.object(auth, "_json_request", return_value={"token_type": "Bearer", "access_token": "new-access", "refresh_token": "rotated"}) as request:
            self.assertEqual(credential.access_token(), "new-access")
        self.assertEqual(request.call_args.kwargs["form"]["refresh_token"], REFRESH)
        self.assertEqual(self.store.connection()["refresh_token"], "rotated")
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_EMAIL": "other@example.invalid"}):
            with self.assertRaises(auth.GoogleAuthError):
                auth.GoogleCredentials.from_environment()

    def test_direct_credentials_require_complete_configuration_and_hide_secrets(self):
        with patch.dict(os.environ, {"GOOGLE_REFRESH_TOKEN": REFRESH}):
            with self.assertRaises(auth.GoogleAuthError):
                auth.GoogleCredentials.from_environment()
        with patch.dict(os.environ, {"GOOGLE_CLIENT_ID": CLIENT, "GOOGLE_CLIENT_SECRET": SECRET,
                                    "GOOGLE_REFRESH_TOKEN": REFRESH, "GOOGLE_CALENDAR_EMAIL": EMAIL}):
            self.assertFalse(auth.google_enabled())
            credential = auth.GoogleCredentials.from_environment()
            self.assertEqual(credential.expected_email, EMAIL)
            self.assertNotIn(SECRET, repr(credential))

    def test_direct_mode_does_not_inherit_old_browser_activation(self):
        self.complete(self.consume(self.begin()))
        self.assertTrue(auth.google_enabled())
        with patch.dict(os.environ, {"GOOGLE_REFRESH_TOKEN": "direct-refresh"}):
            self.assertFalse(auth.google_enabled())
            with patch.dict(os.environ, {"GOOGLE_CALENDAR_ENABLED": "true"}):
                self.assertTrue(auth.google_enabled())

    def test_private_file_rejects_symlinks_wide_permissions_and_wrong_key(self):
        self.begin()
        filename = self.directory / "google" / "connection.enc"
        filename.chmod(0o644)
        with self.assertRaises(auth.GoogleAuthError):
            self.store.connection()
        filename.chmod(0o600)
        with self.assertRaises(auth.GoogleAuthError):
            auth.GoogleConnectionStore(signing_key="other-secret" * 4).connection()
        filename.unlink()
        filename.symlink_to(self.directory / "outside")
        with self.assertRaises(auth.GoogleAuthError):
            self.store.connection()

    def test_https_transport_is_bounded_does_not_follow_redirect_and_redacts_errors(self):
        for status, body in ((302, b"secret-location"), (200, b"x" * (auth._MAX_BYTES + 1)),
                             (400, b'{"error_description":"secret"}'), (200, b"not-json")):
            connection = MagicMock()
            connection.getresponse.return_value.status = status
            connection.getresponse.return_value.read.return_value = body
            with patch.object(auth.http.client, "HTTPSConnection", return_value=connection) as constructor:
                with self.assertRaises(auth.GoogleAuthError) as result:
                    auth._json_request("oauth2.googleapis.com", "/token", form={"secret": SECRET})
            self.assertNotIn(SECRET, str(result.exception))
            self.assertNotIn("secret-location", str(result.exception))
            self.assertEqual(constructor.call_count, 1)
            self.assertEqual(connection.request.call_count, 1)
            connection.close.assert_called_once()
            connection.getresponse.return_value.read.assert_called_once_with(auth._MAX_BYTES + 1)


if __name__ == "__main__":
    unittest.main()
