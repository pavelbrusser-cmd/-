"""Run Mail.ru discovery through the real DAVClient without network access."""

import base64
import os
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

import niquests

from calendar_digest.icloud import CalendarInfo
from calendar_digest.mailru import MailRuCredentials, MailRuError, discover_calendars


USERNAME = "imaginary-mailru-account@example.invalid"
PASSWORD = "fictional-mailru-password-never-valid"
ROOT = "https://calendar.mail.ru/principals/"
PRINCIPAL = "https://calendar.mail.ru/principals/users/example/"
CALENDAR_HOME = "https://calendar.mail.ru/calendars/users/example/"


def multistatus(*responses):
    return (
        '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
        + "".join(responses)
        + "</d:multistatus>"
    )


def dav_response(href, properties, status="200 OK"):
    return (
        f"<d:response><d:href>{escape(href)}</d:href><d:propstat>"
        f"<d:prop>{properties}</d:prop>"
        f"<d:status>HTTP/1.1 {status}</d:status>"
        "</d:propstat></d:response>"
    )


def calendar_properties(name, calendar=True):
    resource = "<c:calendar/>" if calendar else ""
    return (
        f"<d:resourcetype><d:collection/>{resource}</d:resourcetype>"
        f"<d:displayname>{escape(name)}</d:displayname>"
    )


def response_for(request, body, status=207):
    response = niquests.Response()
    response.status_code = status
    response.headers["Content-Type"] = "application/xml; charset=utf-8"
    response._content = body.encode("utf-8")
    response._content_consumed = True
    response.encoding = "utf-8"
    response.url = request.url
    response.request = request
    response.reason = "Multi-Status" if status == 207 else "Error"
    return response


class MailRuDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.transport = self.enterContext(patch.object(niquests.Session, "send"))
        self.transport.side_effect = AssertionError("Unexpected HTTP request")
        self.credentials = MailRuCredentials(USERNAME, PASSWORD)

    def install_discovery(self, calendars, home=CALENDAR_HOME):
        responses = [
            (ROOT, "0", multistatus(dav_response(
                "/principals/",
                "<d:current-user-principal><d:href>/principals/users/example/</d:href>"
                "</d:current-user-principal>",
            ))),
            (PRINCIPAL, "0", multistatus(dav_response(
                "/principals/users/example/",
                f"<c:calendar-home-set><d:href>{escape(home)}</d:href>"
                "</c:calendar-home-set>",
            ))),
            (home, "1", calendars),
        ]

        def send(request, **kwargs):
            self.assertTrue(responses, "Unexpected extra discovery request")
            url, depth, body = responses.pop(0)
            self.assertEqual(request.url, url)
            self.assertEqual(request.method, "PROPFIND")
            self.assertEqual(request.headers["Depth"], depth)
            encoded = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
            self.assertEqual(request.headers["Authorization"], f"Basic {encoded}")
            self.assertTrue(kwargs["verify"])
            self.assertEqual(kwargs["timeout"], 20)
            return response_for(request, body)

        self.transport.side_effect = send
        return responses

    def test_discovers_named_calendars_and_ignores_noncalendars_and_failed_properties(self):
        body = multistatus(
            dav_response("/calendars/users/example/", calendar_properties("Root", False)),
            dav_response("/calendars/users/example/home/", calendar_properties("Дом & семья")),
            dav_response("/calendars/users/example/work/", calendar_properties("Работа")),
            dav_response("/calendars/users/example/inbox/", calendar_properties("Inbox", False)),
            dav_response("/calendars/users/example/private/", calendar_properties("Private"), "404 Not Found"),
        )
        remaining = self.install_discovery(body)
        self.assertEqual(discover_calendars(self.credentials), [
            CalendarInfo(id="home", name="Дом & семья"),
            CalendarInfo(id="work", name="Работа"),
        ])
        self.assertEqual(remaining, [])
        self.assertEqual(self.transport.call_count, 3)

    def test_empty_calendar_list_is_success(self):
        remaining = self.install_discovery(multistatus(dav_response(
            "/calendars/users/example/", calendar_properties("Root", False),
        )))
        self.assertEqual(discover_calendars(self.credentials), [])
        self.assertEqual(remaining, [])

    def test_untrusted_calendar_home_is_rejected_before_authenticated_request(self):
        for home in (
            "https://outside.example.invalid/private/",
            "https://calendar.mail.ru.example.invalid/private/",
            "https://caldav.icloud.com/private/",
        ):
            with self.subTest(home=home):
                self.transport.reset_mock()
                self.install_discovery(multistatus(), home=home)
                with self.assertRaisesRegex(MailRuError, "неподдерживаемый адрес"):
                    discover_calendars(self.credentials)
                self.assertEqual(self.transport.call_count, 2)

    def test_auth_failure_hides_credentials_and_response_body(self):
        body = f"private-calendar-url {USERNAME} {PASSWORD}"
        for status in (401, 403):
            with self.subTest(status=status):
                self.transport.side_effect = (
                    lambda request, _status=status, **kw: response_for(request, body, _status)
                )
                with self.assertRaisesRegex(MailRuError, "отказал в доступе") as error:
                    discover_calendars(self.credentials)
                for private in (USERNAME, PASSWORD, "private-calendar-url"):
                    self.assertNotIn(private, str(error.exception))

    def test_missing_credentials_name_only_missing_variables(self):
        with patch.dict(os.environ, {"MAILRU_1_USERNAME": USERNAME}, clear=True):
            with self.assertRaises(MailRuError) as error:
                MailRuCredentials.from_environment()
        self.assertIn("MAILRU_1_APP_PASSWORD", str(error.exception))
        self.assertNotIn("MAILRU_1_USERNAME", str(error.exception))
        self.assertNotIn(USERNAME, str(error.exception))
        self.transport.assert_not_called()

    def test_blank_credentials_are_missing(self):
        with patch.dict(os.environ, {
            "MAILRU_1_USERNAME": " ", "MAILRU_1_APP_PASSWORD": "\t",
        }, clear=True):
            with self.assertRaises(MailRuError) as error:
                MailRuCredentials.from_environment()
        self.assertIn("MAILRU_1_USERNAME", str(error.exception))
        self.assertIn("MAILRU_1_APP_PASSWORD", str(error.exception))
        self.transport.assert_not_called()

    def test_each_account_uses_its_own_environment_variables(self):
        values = {}
        for account in (1, 2, 3):
            values[f"MAILRU_{account}_USERNAME"] = f" account-{account}@example.invalid "
            values[f"MAILRU_{account}_APP_PASSWORD"] = f" fictional-password-{account} "
        with patch.dict(os.environ, values, clear=True):
            for account in (1, 2, 3):
                with self.subTest(account=account):
                    credentials = MailRuCredentials.from_environment(account)
                    self.assertEqual(credentials.username, f"account-{account}@example.invalid")
                    self.assertEqual(credentials.app_password, f" fictional-password-{account} ")
                    self.assertNotIn(credentials.username, repr(credentials))
                    self.assertNotIn(credentials.app_password, repr(credentials))
        self.transport.assert_not_called()

    def test_invalid_account_is_rejected_without_network(self):
        for account in (0, -1, 4, "1", True, None):
            with self.subTest(account=account), self.assertRaisesRegex(MailRuError, "1, 2 или 3"):
                MailRuCredentials.from_environment(account)
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
