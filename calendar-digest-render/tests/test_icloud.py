"""Exercise the real CalDAV parser through a network-free HTTP transport."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import base64
import os
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

import niquests

from calendar_digest.cli import main
from calendar_digest.icloud import (
    CalendarInfo,
    Credentials,
    ICloudError,
    ICloudReadSession,
    discover_calendars,
)


USERNAME = "imaginary-account@example.invalid"
PASSWORD = "fictional-app-password-never-valid"
ROOT = "https://caldav.icloud.com/"
PRINCIPAL = "https://caldav.icloud.com/123/principal/"
CALENDAR_HOME = "https://p42-caldav.icloud.com/123/calendars/"


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


class ICloudDiscoveryTests(unittest.TestCase):
    def setUp(self):
        # Every test fails immediately if its fixture lacks an HTTP response.
        self.transport = self.enterContext(patch.object(niquests.Session, "send"))
        self.transport.side_effect = AssertionError("Unexpected HTTP request")
        self.credentials = Credentials(USERNAME, PASSWORD)

    def install_discovery(self, calendars, home=CALENDAR_HOME):
        responses = [
            (ROOT, "0", multistatus(dav_response(
                "/", "<d:current-user-principal><d:href>/123/principal/</d:href>"
                "</d:current-user-principal>",
            ))),
            (PRINCIPAL, "0", multistatus(dav_response(
                "/123/principal/",
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
            # niquests resolves True to the environment's trusted CA bundle.
            self.assertTrue(kwargs["verify"])
            self.assertGreater(kwargs["timeout"], 0)
            return response_for(request, body)

        self.transport.side_effect = send
        return responses

    def test_discovers_named_calendars_and_ignores_noncalendars_and_failed_properties(self):
        body = multistatus(
            dav_response("/123/calendars/", calendar_properties("Root", calendar=False)),
            dav_response("/123/calendars/home/", calendar_properties("Дом & семья")),
            dav_response("/123/calendars/work/", calendar_properties("Работа")),
            dav_response("/123/calendars/inbox/", calendar_properties("Inbox", calendar=False)),
            dav_response("/123/calendars/private/", calendar_properties("Unavailable"), "404 Not Found"),
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
            "/123/calendars/", calendar_properties("Root", calendar=False),
        )))
        self.assertEqual(discover_calendars(self.credentials), [])
        self.assertEqual(remaining, [])

    def test_untrusted_home_is_rejected_before_authenticated_request(self):
        self.install_discovery(multistatus(), home="https://outside.example.invalid/private/")
        with self.assertRaisesRegex(ICloudError, "неподдерживаемый адрес"):
            discover_calendars(self.credentials)
        self.assertEqual(self.transport.call_count, 2)

    def test_only_trusted_https_hosts_and_read_methods_reach_transport(self):
        with ICloudReadSession() as session:
            for url in (
                "http://caldav.icloud.com/",
                "https://outside.example.invalid/",
                "https://caldav.icloud.com.example.invalid/",
                "https://p42-caldav.icloud.com.example.invalid/",
                "https://caldav.icloud.com:8443/",
                "https://name:password@caldav.icloud.com/",
                "https://name@caldav.icloud.com/",
            ):
                with self.subTest(url=url), self.assertRaises(ICloudError):
                    session.send(niquests.Request("PROPFIND", url).prepare())
            for method in ("POST", "PUT", "PATCH", "DELETE", "MKCALENDAR"):
                with self.subTest(method=method), self.assertRaises(ICloudError):
                    session.send(niquests.Request(method, ROOT).prepare())
            self.transport.assert_not_called()

            self.transport.side_effect = lambda request, **kw: response_for(request, "")
            for method in ("PROPFIND", "GET", "OPTIONS", "REPORT"):
                session.send(niquests.Request(method, CALENDAR_HOME).prepare())
            self.assertEqual(self.transport.call_count, 4)
            self.assertEqual(session.last_host, "p42-caldav.icloud.com")

    def test_redirect_auth_is_restored_only_after_destination_validation(self):
        auth = Mock(wraps=niquests.auth.HTTPBasicAuth(USERNAME, PASSWORD))
        encoded = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()

        def send(request, **kwargs):
            auth.assert_called_once_with(request)
            self.assertEqual(request.headers["Authorization"], f"Basic {encoded}")
            return response_for(request, "")

        self.transport.side_effect = send
        with ICloudReadSession() as session:
            session.auth = auth
            redirected = niquests.Request("PROPFIND", CALENDAR_HOME).prepare()
            self.assertNotIn("Authorization", redirected.headers)
            session.send(redirected)
            self.transport.assert_called_once()

            auth.reset_mock()
            self.transport.reset_mock()
            untrusted = niquests.Request(
                "PROPFIND", "https://outside.example.invalid/",
            ).prepare()
            with self.assertRaises(ICloudError):
                session.send(untrusted)
            self.assertNotIn("Authorization", untrusted.headers)
            auth.assert_not_called()
            self.transport.assert_not_called()

    def test_missing_credentials_fail_without_network_or_secret_values(self):
        with patch.dict(os.environ, {"ICLOUD_USERNAME": USERNAME, "ICLOUD_APP_PASSWORD": " "}, clear=True):
            with self.assertRaises(ICloudError) as error:
                Credentials.from_environment()
        self.assertIn("ICLOUD_APP_PASSWORD", str(error.exception))
        self.assertNotIn(USERNAME, str(error.exception))
        self.assertNotIn(USERNAME, repr(self.credentials))
        self.assertNotIn(PASSWORD, repr(self.credentials))
        self.transport.assert_not_called()

    def test_cli_reports_auth_proxy_tls_timeout_and_malformed_errors_without_private_data(self):
        private_body = f"private-calendar-url {USERNAME} {PASSWORD}"
        scenarios = [
            ("unauthorized", 401, private_body, "отказал в доступе"),
            ("forbidden", 403, private_body, "отказал в доступе"),
            ("proxy", niquests.exceptions.ProxyError(private_body), None, "Прокси"),
            ("tls", niquests.exceptions.SSLError(private_body), None, "TLS"),
            ("timeout", niquests.exceptions.Timeout(private_body), None, "отведённое время"),
            ("malformed", 207, "<d:multistatus>" + private_body, "корректный ответ CalDAV"),
        ]
        for label, result, body, expected in scenarios:
            with self.subTest(scenario=label):
                if isinstance(result, Exception):
                    self.transport.side_effect = result
                else:
                    self.transport.side_effect = lambda request, _body=body, _status=result, **kw: response_for(request, _body, _status)
                stdout, stderr = StringIO(), StringIO()
                with patch.dict(os.environ, {"ICLOUD_USERNAME": USERNAME, "ICLOUD_APP_PASSWORD": PASSWORD}), redirect_stdout(stdout), redirect_stderr(stderr):
                    code = main(["discover-icloud"])
                self.assertEqual(code, 1)
                self.assertEqual(stdout.getvalue(), "")
                diagnostic = stderr.getvalue()
                self.assertIn(expected, diagnostic)
                for private in (USERNAME, PASSWORD, "private-calendar-url", "Traceback"):
                    self.assertNotIn(private, diagnostic)


if __name__ == "__main__":
    unittest.main()
