"""Offline Pushover protocol tests: no sockets, tokens or real notifications."""

import base64
from dataclasses import FrozenInstanceError, replace
import gzip
import hashlib
import hmac
import http.client
import json
import os
import ssl
import traceback
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from calendar_digest.push_transport import (
    PushoverSettings, PushError, PushUncertain, _encrypt_field,
    probe_pushover, send_notification,
)


SETTINGS = PushoverSettings(app_token="a" * 30, user_key="u" * 30, device="test_iphone",
                            encryption_key=bytes(range(32)).hex())
ENVIRONMENT = dict(PUSHOVER_APP_TOKEN=SETTINGS.app_token, PUSHOVER_USER_KEY=SETTINGS.user_key,
                   PUSHOVER_DEVICE=SETTINGS.device, PUSHOVER_ENCRYPTION_KEY=SETTINGS.encryption_key)
VALIDATE_PATH = "/1/users/validate.json"
SEND_PATH = "/1/messages.json"
TITLE = "Контроль поручения"
MESSAGE = "Через 3 часа: подготовить отчёт.\nКонтроль — 15 октября в 17:00."
URL = "https://calendar.example.test/a/private-event-link"
SENSITIVE = "private-token-or-server-response"


class Response:
    def __init__(self, status=200, data=None, *, raw=None, error=None):
        self.status = status
        self.body = json.dumps(data or {"status": 1}).encode() if raw is None else raw
        self.error = error
        self.read_limits = []

    def read(self, limit):
        self.read_limits.append(limit)
        if self.error is not None:
            raise self.error
        return self.body[:limit]


class Connection:
    def __init__(self, *, validation=None, message=None, connect_error=None,
                 request_error=None, close_error=None):
        self.validation = validation or Response(data={"status": 1, "devices": [SETTINGS.device], "licenses": []})
        self.message = message or Response()
        self.connect_error = connect_error
        self.request_error = request_error
        self.close_error = close_error
        self.operations = []
        self.requests = []

    def connect(self):
        self.operations.append("connect")
        if self.connect_error is not None:
            raise self.connect_error

    def request(self, method, path, *, body, headers):
        self.operations.append(path)
        self.requests.append((method, path, body, headers))
        if path == SEND_PATH and self.request_error is not None:
            raise self.request_error

    def getresponse(self):
        result = self.validation if self.requests[-1][1] == VALIDATE_PATH else self.message
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.operations.append("close")
        if self.close_error is not None:
            raise self.close_error


def decrypted(value):
    packet = base64.b64decode(value, validate=True)
    iv, ciphertext, mac = packet[:16], packet[16:-32], packet[-32:]
    key = bytes.fromhex(SETTINGS.encryption_key)
    if not hmac.compare_digest(mac, hmac.new(key, iv + ciphertext, hashlib.sha256).digest()):
        raise AssertionError("authentication failed")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return gzip.decompress(unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


class PushSettingsTests(unittest.TestCase):
    def test_environment_is_frozen_and_repr_omits_all_values(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True):
            settings = PushoverSettings.from_environment()
        self.assertEqual(settings, SETTINGS)
        self.assertEqual(repr(settings), "PushoverSettings()")
        with self.assertRaises(FrozenInstanceError):
            settings.device = "elsewhere"

    def test_missing_values_only_reveal_names(self):
        with patch.dict(os.environ, {"PUSHOVER_APP_TOKEN": SETTINGS.app_token}, clear=True):
            with self.assertRaises(PushError) as caught:
                PushoverSettings.from_environment()
        self.assertIn("PUSHOVER_ENCRYPTION_KEY", str(caught.exception))
        self.assertNotIn(SETTINGS.app_token, str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_only_single_device_and_well_formed_secrets_are_accepted(self):
        values = {
            "app_token": ["", "a" * 29, "a" * 31, "а" * 30, "a" * 29 + "\n", None],
            "user_key": ["u" * 30 + "," + "x" * 30, " " + "u" * 30, "\n" + "u" * 29],
            "device": ["", "first,second", "a" * 26, "iphone/name", "phone\n", "айфон"],
            "encryption_key": ["", "g" * 64, "0" * 63, "0" * 64 + "\n", None],
        }
        for name, invalid in values.items():
            for value in invalid:
                with self.subTest(name=name, value=value), self.assertRaises(PushError):
                    replace(SETTINGS, **{name: value})
        replace(SETTINGS, device="iPhone-1_x", encryption_key=SETTINGS.encryption_key.upper())


class PushEncryptionTests(unittest.TestCase):
    def test_matches_independent_openssl_aes_cbc_hmac_vector(self):
        # Generated with OpenSSL enc -aes-256-cbc, not this implementation.
        compressed = bytes.fromhex("1f8b0800000000000203bb30ff62c3851d17365dd87ab14947213320233f2f55110047fc429b15000000")
        expected = ("AAECAwQFBgcICQoLDA0OD8fOcFLkCTXjv/+sVP5Oa1OI8Uy6zARGMZWGk6CWl1j5"
                    "rtrrMWXlRHGydcBSLR1F03xQqfMNkzkhV5tNdYqpGJTabUzJs5g0fuFiitHHeL+y")
        with patch("calendar_digest.push_transport.secrets.token_bytes", return_value=bytes(range(16))), \
                patch("calendar_digest.push_transport.gzip.compress", return_value=compressed):
            encoded = _encrypt_field("Привет, iPhone!", bytes(range(32)))
        self.assertEqual(encoded, expected)
        self.assertEqual(decrypted(encoded), "Привет, iPhone!")

    def test_uses_fresh_iv_for_every_encrypted_field(self):
        packets = [_encrypt_field("Одинаковый текст", bytes(range(32))) for _ in range(4)]
        self.assertEqual(len(set(packets)), 4)
        for packet in packets:
            self.assertEqual(decrypted(packet), "Одинаковый текст")

    def test_no_plaintext_fallback_when_crypto_import_fails(self):
        import builtins
        actual = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError(SENSITIVE)
            return actual(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=unavailable), \
                patch("calendar_digest.push_transport.http.client.HTTPSConnection") as factory:
            with self.assertRaises(PushError) as caught:
                send_notification(SETTINGS, title=TITLE, message=MESSAGE, before_send=lambda: None)
        factory.assert_not_called()
        self.assertNotIn(SENSITIVE, str(caught.exception))

    def test_rejects_invalid_content_before_any_connection(self):
        cases = [dict(title="x" * 251), dict(title="secret\nheader"), dict(message="x" * 1025),
                 dict(message=" "), dict(message="\ud800"), dict(message="bad\x00text"),
                 dict(url="http://example.test"), dict(url="https://user:password@example.test"),
                 dict(url="https://example.test:bad"), dict(url="https://example.test/ bad"),
                 dict(url="https://example.test/" + "x" * 512)]
        for changes in cases:
            with self.subTest(changes=changes), \
                    patch("calendar_digest.push_transport.http.client.HTTPSConnection") as factory:
                with self.assertRaises(PushError):
                    send_notification(SETTINGS, **(dict(title=TITLE, message=MESSAGE, url=URL) | changes),
                                      before_send=lambda: None)
                factory.assert_not_called()


class PushProtocolTests(unittest.TestCase):
    def perform(self, connection, *, checkpoint=None, **contents):
        if checkpoint is None:
            checkpoint = lambda: connection.operations.append("checkpoint")
        with patch("calendar_digest.push_transport.http.client.HTTPSConnection", return_value=connection) as factory:
            result = send_notification(SETTINGS, **(dict(title=TITLE, message=MESSAGE, url=URL) | contents),
                                       before_send=checkpoint)
        return result, factory

    def assert_private(self, error):
        for secret in [*ENVIRONMENT.values(), TITLE, MESSAGE, URL, SENSITIVE]:
            self.assertNotIn(secret, str(error))
            self.assertNotIn(secret, "".join(traceback.format_exception(error)))

    def test_encrypts_title_body_url_and_checks_device_before_checkpoint(self):
        connection = Connection()
        result, factory = self.perform(connection)
        self.assertEqual(result, {"provider": "pushover", "state": "accepted", "encrypted": True})
        self.assertEqual(connection.operations, ["connect", VALIDATE_PATH, "checkpoint", SEND_PATH, "close"])
        self.assertEqual(factory.call_args.args, ("api.pushover.net", 443))
        self.assertEqual(factory.call_args.kwargs["timeout"], 30)
        context = factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        method, _, body, headers = connection.requests[1]
        self.assertEqual(method, "POST")
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded")
        data = parse_qs(body.decode("ascii"))
        self.assertEqual(data["encrypted"], ["1"])
        self.assertEqual(data["priority"], ["0"])
        self.assertEqual(data["device"], [SETTINGS.device])
        self.assertEqual(decrypted(data["title"][0]), TITLE)
        self.assertEqual(decrypted(data["message"][0]), MESSAGE)
        self.assertEqual(decrypted(data["url"][0]), URL)
        self.assertNotIn(SETTINGS.encryption_key, body.decode("ascii"))
        for plain in (TITLE, MESSAGE, URL):
            self.assertNotIn(plain.encode("utf-8"), body)
        self.assertEqual(set(data), {"token", "user", "device", "encrypted", "priority", "title", "message", "url"})

    def test_unicode_limits_count_characters_and_optional_url_is_omitted(self):
        connection = Connection()
        self.perform(connection, title="я" * 250, message="🎂" * 1024, url=None)
        data = parse_qs(connection.requests[1][2].decode())
        self.assertEqual(decrypted(data["message"][0]), "🎂" * 1024)
        self.assertNotIn("url", data)

    def test_probe_only_validates_with_no_notification_or_encryption_key_sent(self):
        connection = Connection()
        with patch("calendar_digest.push_transport.http.client.HTTPSConnection", return_value=connection):
            result = probe_pushover(SETTINGS)
        self.assertEqual(result, {"pushover": "ok", "device_valid": True, "sent": False})
        self.assertEqual(connection.operations, ["connect", VALIDATE_PATH, "close"])
        params = parse_qs(connection.requests[0][2].decode())
        self.assertEqual(set(params), {"token", "user", "device"})
        self.assertEqual(params["device"], [SETTINGS.device])

    def test_invalid_or_missing_target_device_prevents_notification(self):
        for devices in ([], ["other-phone"], None, SETTINGS.device, [SETTINGS.device, 12]):
            connection = Connection(validation=Response(data={"status": 1, "devices": devices}))
            with self.subTest(devices=devices), self.assertRaises(PushError) as caught:
                self.perform(connection)
            self.assertNotIn("checkpoint", connection.operations)
            self.assertEqual(len(connection.requests), 1)
            self.assertFalse(caught.exception.retryable)
            self.assert_private(caught.exception)

    def test_checkpoint_failure_prevents_notification(self):
        connection = Connection()

        def checkpoint():
            raise RuntimeError("outbox checkpoint failed")

        with self.assertRaisesRegex(RuntimeError, "outbox checkpoint failed"):
            self.perform(connection, checkpoint=checkpoint)
        self.assertEqual(len(connection.requests), 1)
        self.assertEqual(connection.operations[-1], "close")

    def test_connection_timeout_is_retryable_without_any_request(self):
        connection = Connection(connect_error=TimeoutError(SENSITIVE))
        with self.assertRaises(PushError) as caught:
            self.perform(connection)
        self.assertNotIsInstance(caught.exception, PushUncertain)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(connection.requests, [])
        self.assertEqual(connection.operations[-1], "close")
        self.assert_private(caught.exception)

    def test_invalid_tls_is_not_retried_or_downgraded(self):
        connection = Connection(connect_error=ssl.SSLCertVerificationError(SENSITIVE))
        with self.assertRaises(PushError) as caught:
            self.perform(connection)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(connection.requests, [])
        self.assert_private(caught.exception)

    def test_validation_reply_failure_is_safe_to_retry_and_does_not_send(self):
        for response in (TimeoutError(SENSITIVE), Response(status=503), Response(raw=b"not json"),
                         Response(error=OSError(SENSITIVE))):
            connection = Connection(validation=response)
            with self.subTest(response=response), self.assertRaises(PushError) as caught:
                self.perform(connection)
            self.assertNotIsInstance(caught.exception, PushUncertain)
            self.assertTrue(caught.exception.retryable)
            self.assertNotIn("checkpoint", connection.operations)
            self.assert_private(caught.exception)

    def test_explicit_rejection_is_terminal_without_retries(self):
        for response in (Response(status=400), Response(status=401), Response(status=429),
                         Response(data={"status": 0, "errors": [SENSITIVE]})):
            connection = Connection(message=response)
            with self.subTest(response=response), self.assertRaises(PushError) as caught:
                self.perform(connection)
            self.assertNotIsInstance(caught.exception, PushUncertain)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(connection.requests), 2)
            self.assert_private(caught.exception)

    def test_redirect_is_never_followed(self):
        for status in (301, 302, 303, 307, 308):
            connection = Connection(message=Response(status=status))
            with self.subTest(status=status), self.assertRaises(PushError) as caught:
                self.perform(connection)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(connection.requests), 2)
            self.assert_private(caught.exception)

    def test_lost_or_invalid_reply_after_post_is_uncertain_and_never_retried(self):
        replies = [TimeoutError(SENSITIVE), http.client.RemoteDisconnected(SENSITIVE),
                   Response(status=500), Response(status=502), Response(raw=b"not json"),
                   Response(raw=b'{"status":false}'), Response(raw=b'{"status":true}'),
                   Response(raw=b'{"status":0,"status":1}'), Response(raw=b'{"status":2}'),
                   Response(raw=b'[]'), Response(raw=b'{}'), Response(raw=b'\xff'),
                   Response(raw=b" " * (32 * 1024 + 1)), Response(error=OSError(SENSITIVE))]
        for response in replies:
            connection = Connection(message=response)
            with self.subTest(response=response), self.assertRaises(PushUncertain) as caught:
                self.perform(connection)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(len(connection.requests), 2)
            self.assertEqual(connection.operations[-1], "close")
            self.assert_private(caught.exception)

    def test_failed_request_write_after_checkpoint_is_uncertain(self):
        connection = Connection(request_error=BrokenPipeError(SENSITIVE))
        with self.assertRaises(PushUncertain) as caught:
            self.perform(connection)
        self.assertFalse(caught.exception.retryable)
        self.assertIn("checkpoint", connection.operations)
        self.assertEqual(len(connection.requests), 2)
        self.assert_private(caught.exception)

    def test_close_failure_does_not_undo_provider_acceptance(self):
        result, _ = self.perform(Connection(close_error=OSError(SENSITIVE)))
        self.assertEqual(result["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
