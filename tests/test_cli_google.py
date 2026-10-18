"""Google connection diagnostics expose counts, never calendar contents."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from calendar_digest.cli import main
from calendar_digest.readonly_caldav import CalendarConnectionError


class GoogleCommandTests(unittest.TestCase):
    def test_probe_only_reads_and_reports_counts(self):
        output, errors = io.StringIO(), io.StringIO()
        collection = SimpleNamespace(calendar_count=1, resources=["private-event-content"])
        with patch("calendar_digest.google_calendar.collect_google", return_value=collection) as collect, \
                patch("calendar_digest.google_auth.google_enabled", return_value=True), \
                redirect_stdout(output), redirect_stderr(errors):
            result = main(["probe-google"])
        self.assertEqual(result, 0)
        collect.assert_called_once_with()
        self.assertEqual(json.loads(output.getvalue()), {
            "google": "ok", "account_verified": True, "enabled": True,
            "calendars": 1, "resources": 1, "sent": False,
        })
        self.assertNotIn("private-event-content", output.getvalue() + errors.getvalue())

    def test_authentication_failure_does_not_report_connected(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("calendar_digest.google_calendar.collect_google", side_effect=CalendarConnectionError("Нужен вход Google.")), \
                redirect_stdout(output), redirect_stderr(errors):
            result = main(["probe-google"])
        self.assertNotEqual(result, 0)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Нужен вход Google.", errors.getvalue())
