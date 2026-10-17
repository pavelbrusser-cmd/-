from dataclasses import replace
from datetime import date, datetime, timezone
from email.parser import BytesParser
from email.policy import default
from io import BytesIO
import os
import smtplib
import ssl
import unittest
from unittest.mock import patch

from calendar_digest.selection import DigestEvent, SeriesChangeNotice, SeriesInterval
from calendar_digest.smtp_delivery import (
    MailSettings, SMTPDeliveryError, SMTPDeliveryUncertain,
    make_message, probe_smtp, submit_message,
)


SETTINGS = MailSettings(username="sender@example.test", password="private-app-password",
                        recipient="owner@example.test")
ENVIRONMENT = {"SMTP_USERNAME": SETTINGS.username, "SMTP_PASSWORD": SETTINGS.password,
               "DIGEST_RECIPIENT": SETTINGS.recipient}
DAY = date(2026, 10, 15)
STAMP = datetime(2026, 10, 14, 14, tzinfo=timezone.utc)


def example(**changes):
    values = dict(source_id="mailru_2", calendar_id="fake-calendar", calendar_name="График",
                  uid="fake-uid", recurrence_id=None, title="Встреча", start="2026-10-15T16:00:00+03:00",
                  end="2026-10-15T16:30:00+03:00", all_day=False, event_status="CONFIRMED",
                  participation="ACCEPTED", recurring=False, multi_day=False)
    values.update(changes)
    return DigestEvent(**values)


def series_notice(**changes):
    values = dict(
        source_id="mailru_2", calendar_id="private-calendar-path", calendar_name="График",
        uid="private-series-uid", title="Изменённая серия",
        old_anchor_start="2026-09-01T15:00:00+03:00", old_anchor_end="2026-09-01T15:30:00+03:00",
        new_anchor_start="2026-09-01T16:00:00+03:00", new_anchor_end="2026-09-01T16:30:00+03:00",
        old_recurring=True, new_recurring=True,
        old_intervals=[SeriesInterval(start="2026-10-15T15:00:00+03:00",
                                      end="2026-10-15T15:30:00+03:00", all_day=False)],
        new_intervals=[SeriesInterval(start="2026-10-15T16:00:00+03:00",
                                      end="2026-10-15T16:30:00+03:00", all_day=False)],
    )
    values.update(changes)
    return SeriesChangeNotice(**values)


class FakeSMTP:
    """Record command order and inject failures without creating any sockets."""

    def __init__(self, **overrides):
        self.commands = []
        self.context = None
        self.behavior = overrides

    def result(self, operation, default):
        self.commands.append(operation)
        value = self.behavior.get(operation, default)
        if isinstance(value, Exception):
            raise value
        return value

    def ehlo(self):
        return self.result("ehlo", (250, b"server says hello"))

    def has_extn(self, extension):
        return self.result("has_" + extension, True)

    def starttls(self, *, context):
        self.context = context
        return self.result("starttls", (220, b"ready"))

    def login(self, username, password):
        self.login_arguments = (username, password)
        return self.result("login", (235, b"authenticated"))

    def mail(self, sender):
        self.sender = sender
        return self.result("mail", (250, b"sender accepted"))

    def rcpt(self, recipient):
        self.recipient = recipient
        return self.result("rcpt", (250, b"recipient accepted"))

    def data(self, payload):
        self.payload = payload
        return self.result("data", (250, b"message accepted"))

    def quit(self):
        return self.result("quit", (221, b"bye"))

    def close(self):
        return self.result("close", None)


class SMTPSettingsTests(unittest.TestCase):
    def test_environment_defaults_and_repr_never_reveal_credentials(self):
        with patch.dict(os.environ, ENVIRONMENT, clear=True):
            settings = MailSettings.from_environment()
        self.assertEqual(settings, SETTINGS)
        self.assertEqual((settings.host, settings.port, settings.security), ("smtp.yandex.ru", 465, "ssl"))
        for value in (SETTINGS.username, SETTINGS.password, SETTINGS.recipient):
            self.assertNotIn(value, repr(settings))

    def test_missing_environment_error_lists_names_not_values(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": SETTINGS.password}, clear=True):
            with self.assertRaises(SMTPDeliveryError) as caught:
                MailSettings.from_environment()
        self.assertIn("SMTP_USERNAME", str(caught.exception))
        self.assertIn("DIGEST_RECIPIENT", str(caught.exception))
        self.assertNotIn(SETTINGS.password, str(caught.exception))
        self.assertFalse(caught.exception.retryable)

    def test_accepts_single_ascii_plus_address_but_rejects_header_injection_and_multiple(self):
        replace(SETTINGS, username="from+calendar@example.test", recipient="to+digest@example.test")
        for address in ("name@example.test\r\nBcc: extra@example.test", "one@example.test,two@example.test",
                        "One <one@example.test>", "русский@example.test", "one@example.test;two@example.test",
                        "one@bad domain.test", "one@example.test\n", "one..two@example.test", "one@-bad.test"):
            for field in ("username", "recipient"):
                with self.subTest(address=address, field=field), self.assertRaises(SMTPDeliveryError):
                    replace(SETTINGS, **{field: address})
        for name in ("Sender\r\nBcc: leak@example.test", "", "x\x00y"):
            with self.assertRaises(SMTPDeliveryError):
                replace(SETTINGS, sender_name=name)

    def test_plaintext_wrong_port_and_url_hosts_are_rejected(self):
        for changes in ({"security": "none"}, {"port": 25}, {"security": "starttls"},
                        {"security": "ssl", "port": 587}, {"host": "https://smtp.yandex.ru"},
                        {"host": "smtp.yandex.ru:465"}, {"port": True}):
            with self.subTest(changes=changes), self.assertRaises(SMTPDeliveryError):
                replace(SETTINGS, **changes)
        with patch.dict(os.environ, ENVIRONMENT | {"SMTP_PORT": "private-non-number"}, clear=True):
            with self.assertRaises(SMTPDeliveryError) as caught:
                MailSettings.from_environment()
            self.assertNotIn("private-non-number", str(caught.exception))

    def test_environment_rejects_newline_addresses_and_invalid_application_password(self):
        for variable, value in (("SMTP_USERNAME", "sender@example.test\r\n"),
                                ("DIGEST_RECIPIENT", "owner@example.test\n"),
                                ("SMTP_PASSWORD", "пароль"),
                                ("SMTP_PASSWORD", "password\n")):
            with patch.dict(os.environ, ENVIRONMENT | {variable: value}, clear=True):
                with self.assertRaises(SMTPDeliveryError) as caught:
                    MailSettings.from_environment()
            self.assertNotIn(value, str(caught.exception))
            self.assertFalse(caught.exception.retryable)


class SMTPProtocolTests(unittest.TestCase):
    def test_probe_authenticates_over_verified_tls_without_sending_any_envelope(self):
        client = FakeSMTP()
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client) as factory:
            result = probe_smtp(SETTINGS)
        self.assertEqual(client.commands, ["ehlo", "login", "quit", "close"])
        self.assertEqual(client.login_arguments, (SETTINGS.username, SETTINGS.password))
        self.assertEqual(result, {"smtp": "ok", "security": "ssl", "authenticated": True, "sent": False})
        context = factory.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertEqual(factory.call_args.args, ("smtp.yandex.ru", 465))
        self.assertGreater(factory.call_args.kwargs["timeout"], 0)
        self.assertNotIn(SETTINGS.username, str(result))

    def test_starttls_requires_encryption_and_fresh_ehlo_before_authentication(self):
        client = FakeSMTP()
        settings = replace(SETTINGS, security="starttls", port=587)
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP", return_value=client):
            result = probe_smtp(settings)
        self.assertEqual(client.commands, ["ehlo", "has_starttls", "starttls", "ehlo", "login", "quit", "close"])
        self.assertTrue(client.context.check_hostname)
        self.assertEqual(client.context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(result["security"], "starttls")

    def test_missing_starttls_and_bad_certificate_never_fall_back_to_plain_login(self):
        settings = replace(SETTINGS, security="starttls", port=587)
        for behavior in ({"has_starttls": False}, {"starttls": ssl.SSLCertVerificationError("private server response")}):
            client = FakeSMTP(**behavior)
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP", return_value=client):
                with self.assertRaises(SMTPDeliveryError) as caught:
                    probe_smtp(settings)
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("private server response", str(caught.exception))
            self.assertNotIn("login", client.commands)
            self.assertIn("close", client.commands)

    def test_implicit_tls_certificate_failure_has_no_plaintext_fallback(self):
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", side_effect=ssl.SSLCertVerificationError("secret")):
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP") as plaintext:
                with self.assertRaises(SMTPDeliveryError) as caught:
                    probe_smtp(SETTINGS)
                plaintext.assert_not_called()
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("secret", str(caught.exception))

    def test_login_rejection_is_safe_and_permanent_but_temporary_auth_failure_can_retry(self):
        for code, retryable in ((535, False), (454, True)):
            client = FakeSMTP(login=smtplib.SMTPAuthenticationError(code, b"private-auth-response"))
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
                with self.assertRaises(SMTPDeliveryError) as caught:
                    probe_smtp(SETTINGS)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn("private-auth-response", str(caught.exception))
            self.assertNotIn("mail", client.commands)
            self.assertIn("close", client.commands)

    def test_ehlo_rejection_prevents_login(self):
        client = FakeSMTP(ehlo=(550, b"private details"))
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
            with self.assertRaises(SMTPDeliveryError) as caught:
                probe_smtp(SETTINGS)
        self.assertEqual(client.commands, ["ehlo", "close"])
        self.assertNotIn("private details", str(caught.exception))

    def test_envelope_is_explicit_and_durable_callback_precedes_data(self):
        client = FakeSMTP()
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
            submit_message(SETTINGS, b"From: test\r\n\r\nbody\r\n", before_data=lambda: client.commands.append("checkpoint"))
        self.assertEqual(client.commands, ["ehlo", "login", "mail", "rcpt", "checkpoint", "data", "quit", "close"])
        self.assertEqual(client.sender, SETTINGS.username)
        self.assertEqual(client.recipient, SETTINGS.recipient)
        self.assertEqual(client.payload, b"From: test\r\n\r\nbody\r\n")

    def test_checkpoint_exception_propagates_unchanged_and_no_data_is_sent(self):
        client = FakeSMTP()
        error = OSError("Cannot durably save intent")
        def checkpoint():
            raise error
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
            with self.assertRaises(OSError) as caught:
                submit_message(SETTINGS, b"body", before_data=checkpoint)
        self.assertIs(caught.exception, error)
        self.assertNotIn("data", client.commands)
        self.assertEqual(client.commands[-2:], ["quit", "close"])

    def test_envelope_rejections_and_connection_loss_before_data_are_known_failures(self):
        for operation, failure, retryable in (("mail", (550, b"private"), False),
                                               ("rcpt", (450, b"private"), True),
                                               ("rcpt", (550, b"private"), False),
                                               ("rcpt", smtplib.SMTPServerDisconnected("private"), True)):
            client = FakeSMTP(**{operation: failure})
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
                with self.assertRaises(SMTPDeliveryError) as caught:
                    submit_message(SETTINGS, b"body", before_data=lambda: client.commands.append("checkpoint"))
            self.assertNotIsInstance(caught.exception, SMTPDeliveryUncertain)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn("checkpoint", client.commands)
            self.assertNotIn("data", client.commands)

    def test_explicit_data_rejection_is_known_and_only_transient_response_is_retryable(self):
        for failure, retryable in (((450, b"private"), True), ((550, b"private"), False),
                                   (smtplib.SMTPDataError(451, b"private"), True),
                                   (smtplib.SMTPDataError(554, b"private"), False)):
            client = FakeSMTP(data=failure)
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
                with self.assertRaises(SMTPDeliveryError) as caught:
                    submit_message(SETTINGS, b"body", before_data=lambda: client.commands.append("checkpoint"))
            self.assertNotIsInstance(caught.exception, SMTPDeliveryUncertain)
            self.assertEqual(caught.exception.retryable, retryable)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(client.commands.count("data"), 1)

    def test_data_connection_loss_or_unexpected_reply_is_uncertain_without_retry(self):
        for failure in (smtplib.SMTPServerDisconnected("private"), TimeoutError("private"),
                        (251, b"unexpected"), (354, b"unexpected"), smtplib.SMTPDataError(354, b"private")):
            client = FakeSMTP(data=failure)
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client) as factory:
                with self.assertRaises(SMTPDeliveryUncertain) as caught:
                    submit_message(SETTINGS, b"body", before_data=lambda: client.commands.append("checkpoint"))
            self.assertFalse(caught.exception.retryable)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(client.commands.count("data"), 1)
            self.assertNotIn("quit", client.commands)
            factory.assert_called_once()

    def test_quit_failure_after_acceptance_does_not_trigger_failure_or_resend(self):
        client = FakeSMTP(quit=smtplib.SMTPServerDisconnected("already accepted"))
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client) as factory:
            submit_message(SETTINGS, b"body", before_data=lambda: None)
        self.assertEqual(client.commands.count("data"), 1)
        self.assertEqual(client.commands[-1], "close")
        factory.assert_called_once()


class SMTPWireTests(unittest.TestCase):
    """Exercise stdlib SMTP.data framing and replies, still without sockets."""

    @staticmethod
    def client(data_replies):
        class Socket:
            def __init__(self):
                self.writes = []
                self.closed = False

            def sendall(self, data):
                self.writes.append(data)

            def close(self):
                self.closed = True

        client = smtplib.SMTP(local_hostname="fixture.example.test")
        socket = Socket()
        client.sock = socket
        client.file = BytesIO(b"250-fixture.example.test\r\n250 AUTH PLAIN\r\n"
                              b"235 authenticated\r\n250 sender\r\n250 recipient\r\n" + data_replies)
        return client, socket

    def test_real_smtp_data_stuffs_dots_and_sends_once_after_checkpoint(self):
        client, socket = self.client(b"354 continue\r\n250 accepted\r\n221 bye\r\n")
        checkpoint_writes = []
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
            submit_message(SETTINGS, b"Subject: test\r\n\r\n.line\r\n.\r\n",
                           before_data=lambda: checkpoint_writes.extend(socket.writes))
        self.assertFalse(any(write.startswith(b"data") or write.startswith(b"Subject:") for write in checkpoint_writes))
        self.assertEqual(socket.writes[-2], b"Subject: test\r\n\r\n..line\r\n..\r\n.\r\n")
        self.assertEqual(socket.writes[-1], b"quit\r\n")
        self.assertTrue(socket.closed)

    def test_eof_after_real_payload_is_uncertain_and_does_not_append_quit_or_repeat(self):
        client, socket = self.client(b"354 continue\r\n")
        with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
            with self.assertRaises(SMTPDeliveryUncertain):
                submit_message(SETTINGS, b"Subject: test\r\n\r\nbody", before_data=lambda: None)
        self.assertEqual(sum(write.startswith(b"Subject:") for write in socket.writes), 1)
        self.assertFalse(any(write.startswith(b"quit") for write in socket.writes))
        self.assertTrue(socket.closed)

    def test_real_rejection_before_body_and_after_body_are_known_failures(self):
        for replies, bodies in ((b"554 refused\r\n", 0), (b"354 continue\r\n554 refused\r\n", 1)):
            client, socket = self.client(replies)
            with patch("calendar_digest.smtp_delivery.smtplib.SMTP_SSL", return_value=client):
                with self.assertRaises(SMTPDeliveryError) as caught:
                    submit_message(SETTINGS, b"Subject: test\r\n\r\nbody", before_data=lambda: None)
            self.assertNotIsInstance(caught.exception, SMTPDeliveryUncertain)
            self.assertFalse(caught.exception.retryable)
            self.assertEqual(sum(write.startswith(b"Subject:") for write in socket.writes), bodies)


class MIMEMessageTests(unittest.TestCase):
    def make(self, events=(), html="<html><body>Проверка</body></html>", **kwargs):
        return make_message(DAY, events, html, SETTINGS, message_id="<stable-id@calendar-digest.local>",
                            generated_at=STAMP, **kwargs)

    def test_multipart_alternatives_preserve_html_and_use_safe_headers_and_stable_id(self):
        html = '<!doctype html>\n<html><body><a href="https://example.test/a/opaque">Подтвердить</a></body></html>\n'
        raw = self.make([example()], html)
        message = BytesParser(policy=default).parsebytes(raw)
        self.assertEqual(message.get_content_type(), "multipart/alternative")
        self.assertEqual([part.get_content_type() for part in message.iter_parts()], ["text/plain", "text/html"])
        self.assertEqual(message.get_body("html").get_content().replace("\r\n", "\n"), html)
        self.assertEqual(message["From"].addresses[0].addr_spec, SETTINGS.username)
        self.assertEqual(message["From"].addresses[0].display_name, "Ваш календарь")
        self.assertEqual(message["To"].addresses[0].addr_spec, SETTINGS.recipient)
        self.assertEqual(message["Date"].datetime, STAMP)
        self.assertEqual(str(message["Message-ID"]), "<stable-id@calendar-digest.local>")
        self.assertEqual(str(message["Subject"]), "Ваш календарь на 15 октября 2026")
        self.assertIsNone(message["Bcc"])
        self.assertNotIn(SETTINGS.password.encode(), raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))

    def test_plain_text_contains_cancellation_move_invitation_and_multiday_dates_without_markdown(self):
        moved = example(title="Перенесённая встреча", event_status="CANCELLED", participation="DECLINED",
                        original_start="2026-10-15T15:00:00+03:00", original_end="2026-10-15T15:30:00+03:00",
                        recurring=True, location="Переговорная", timezone_assumed=True)
        all_day = example(title="Праздник", start="2026-10-14", end="2026-10-17", all_day=True,
                          multi_day=True, event_status="TENTATIVE", participation="NEEDS-ACTION")
        message = BytesParser(policy=default).parsebytes(self.make([moved, all_day]))
        plain = message.get_body("plain").get_content()
        for fragment in ("Отменено", "Ваш ответ: отклонено", "Перенесено", "Было: 15 октября 2026, 15:00–15:30",
                         "Стало: 15 октября 2026, 16:00–16:30", "Повторяющееся событие", "Место: Переговорная",
                         "14 октября 2026 — 16 октября 2026", "Ваш ответ: ожидает ответа", "Предварительно",
                         "Несколько дней", "Mail.ru · 2 / График", "время показано по Москве"):
            self.assertIn(fragment, plain)
        self.assertNotIn("**", plain)
        self.assertFalse(any(line.startswith("#") for line in plain.splitlines()))

    def test_empty_day_and_test_message_are_explicit(self):
        message = BytesParser(policy=default).parsebytes(self.make(test=True))
        self.assertTrue(str(message["Subject"]).startswith("[Проверка] "))
        self.assertIn("На этот день событий нет.", message.get_body("plain").get_content())
        self.assertIn("Проверочное письмо.", message.get_body("plain").get_content())
        self.assertNotIn("Изменения повторяющихся событий", message.get_body("plain").get_content())

    def test_old_only_series_notice_has_independent_saved_schedule_and_separate_anchor(self):
        notice = series_notice(new_anchor_start="2026-10-16T16:00:00+03:00",
                               new_anchor_end="2026-10-16T16:30:00+03:00",
                               new_recurring=False, new_intervals=[])
        message = BytesParser(policy=default).parsebytes(self.make(series_changes=(notice,)))
        plain = message.get_body("plain").get_content().replace("\r\n", "\n")
        section = plain.split("Изменения повторяющихся событий\n", 1)[1]
        self.assertIn("Изменённая серия\nMail.ru · 2 / График", section)
        self.assertIn("Отдельные повторения между списками не сопоставляются.", section)
        self.assertIn("В сохранённом расписании:\n"
                      "Начало серии (с повторением): 1 сентября 2026, 15:00–15:30\n"
                      "На этот день:\n15 октября 2026, 15:00–15:30", section)
        self.assertIn("Сейчас:\nНачало события (без повторения): 16 октября 2026, 16:00–16:30\n"
                      "На этот день событий нет.", section)
        self.assertNotIn("Перенесено", section)
        self.assertNotIn("Стало:", section)
        self.assertNotIn("сегодня", section.lower())

    def test_notice_renders_mixed_all_day_and_timed_intervals_without_pairing_them(self):
        notice = series_notice(
            old_anchor_start="2026-09-01", old_anchor_end="2026-09-03",
            old_intervals=[SeriesInterval(start="2026-10-14", end="2026-10-17", all_day=True)],
            new_intervals=[SeriesInterval(start="2026-10-15T16:00:00+03:00",
                                          end="2026-10-15T16:30:00+03:00", all_day=False),
                           SeriesInterval(start="2026-10-15T18:00:00+03:00",
                                          end="2026-10-15T18:30:00+03:00", all_day=False)],
        )
        html = "<html><body>HTML уже сформирован</body></html>\n"
        message = BytesParser(policy=default).parsebytes(
            self.make([example(title="Обычная встреча")], html,
                      series_changes=(item for item in (notice,))))
        plain = message.get_body("plain").get_content().replace("\r\n", "\n")
        self.assertLess(plain.index("Обычная встреча"), plain.index("Изменения повторяющихся событий"))
        self.assertIn("Начало серии (с повторением): 1 сентября 2026 — 2 сентября 2026", plain)
        self.assertIn("14 октября 2026 — 16 октября 2026 · На весь день", plain)
        self.assertIn("На этот день:\n15 октября 2026, 16:00–16:30\n15 октября 2026, 18:00–18:30", plain)
        self.assertEqual(message.get_body("html").get_content().replace("\r\n", "\n"), html)

    def test_notice_metadata_remains_in_body_and_private_identifiers_are_omitted(self):
        notice = series_notice(title="Заголовок\r\nBcc: unintended@example.test",
                               calendar_name="Календарь\r\nX-Private-Header: injected")
        raw = self.make(series_changes=(notice,))
        message = BytesParser(policy=default).parsebytes(raw)
        plain = message.get_body("plain").get_content()
        self.assertIn("Bcc: unintended@example.test", plain)
        self.assertIn("X-Private-Header: injected", plain)
        for part in message.walk():
            self.assertIsNone(part["Bcc"])
            self.assertIsNone(part["X-Private-Header"])
        self.assertEqual(str(message["Subject"]), "Ваш календарь на 15 октября 2026")
        for private in (notice.calendar_id, notice.uid, SETTINGS.password):
            self.assertNotIn(private, plain)
            self.assertNotIn(private.encode(), raw)

    def test_naive_date_or_injected_message_id_is_rejected(self):
        for arguments in ({"message_id": "<ok@example.test>\r\nBcc: leak@example.test", "generated_at": STAMP},
                          {"message_id": "<ok@example.test>", "generated_at": datetime(2026, 10, 14, 14)}):
            with self.assertRaises(SMTPDeliveryError):
                make_message(DAY, [], "<p>test</p>", SETTINGS, **arguments)


if __name__ == "__main__":
    unittest.main()
