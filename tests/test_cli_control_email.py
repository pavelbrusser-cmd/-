"""CLI routing and privacy checks for participant-email workflows."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from io import StringIO
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from calendar_digest import cli
from calendar_digest.control_email_automation import ControlEmailCycleError
from calendar_digest.control_email_preview import ControlEmailPreviewError, ControlEmailPreviewStage
from calendar_digest.control_email_queue import ControlEmailQueueError
from calendar_digest.calendar_operation_lock import CalendarBusy


class ControlEmailCLITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def invoke(self, arguments):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = cli.main(arguments)
        return result, output.getvalue(), errors.getvalue()

    def test_preview_routes_date_alias_days_and_fixed_progress(self):
        summary = {"reminders_total": 5, "recipients_total": 2, "sent": False, "activated": False}
        def preview(start, directory, *, days, progress):
            self.assertEqual(start, date(2026, 10, 19))
            self.assertEqual(directory, Path("synthetic-private-directory"))
            self.assertEqual(days, 7)
            for stage in ControlEmailPreviewStage:
                progress(stage)
            return summary
        with patch.object(cli, "create_control_email_preview", side_effect=preview):
            code, output, errors = self.invoke([
                "preview-control-email", "--start-date", "2026-10-19", "--days", "7",
                "--directory", "synthetic-private-directory",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), summary)
        self.assertEqual(errors.splitlines(), [stage.value for stage in ControlEmailPreviewStage])

    def test_default_preview_date_uses_moscow_and_state_directory_environment(self):
        now = datetime(2026, 10, 19, 1, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        with patch.object(cli, "datetime") as clock, \
                patch.object(cli, "create_control_email_preview", return_value={"sent": False}) as preview, \
                patch.dict(os.environ, {"DIGEST_STATE_DIR": "configured-private-state"}):
            clock.now.return_value = now
            code, output, errors = self.invoke(["preview-control-email"])
        self.assertEqual(code, 0)
        clock.now.assert_called_once_with(ZoneInfo("Europe/Moscow"))
        preview.assert_called_once_with(date(2026, 10, 19), Path("configured-private-state"),
                                        days=7, progress=cli._control_email_progress)
        self.assertEqual(errors, "")

    def test_invalid_days_fail_before_work(self):
        with patch.object(cli, "create_control_email_preview") as preview:
            for value in ("0", "32", "-1"):
                with self.subTest(days=value), redirect_stderr(StringIO()), self.assertRaises(SystemExit) as failure:
                    cli.main(["preview-control-email", "--days", value])
                self.assertEqual(failure.exception.code, 2)
            preview.assert_not_called()

    def test_prepare_and_status_route_to_independent_workflows(self):
        with patch("calendar_digest.control_email_automation.prepare_control_email",
                   return_value={"mode": "prepare", "sent": False, "queue": {"pending": 2}}) as prepare, \
                patch("calendar_digest.control_email_automation.run_control_email_cycle") as cycle:
            code, output, errors = self.invoke(["prepare-control-email", "--directory", "state"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["sent"])
        self.assertIn("Отправка и включение расписания не выполняются", errors)
        prepare.assert_called_once_with(Path("state"))
        cycle.assert_not_called()
        status = {"enabled": False, "running": False, "queue": {"pending": 2}}
        with patch("calendar_digest.control_email_automation.automatic_control_email_status", return_value=status) as inspect:
            code, output, errors = self.invoke(["control-email-status", "--directory", "state"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), status)
        self.assertEqual(errors, "")
        inspect.assert_called_once_with(Path("state"))

    def test_internal_cycle_maps_partial_failure_to_nonzero_exit(self):
        for counts, expected in (({"accepted": 1, "failed": 0, "uncertain": 0}, 0),
                                 ({"accepted": 0, "failed": 1, "uncertain": 0}, 1),
                                 ({"accepted": 0, "failed": 0, "uncertain": 1}, 1)):
            with self.subTest(counts=counts), patch(
                "calendar_digest.control_email_automation.run_control_email_cycle", return_value=counts,
            ) as cycle:
                code, output, errors = self.invoke(["control-email-cycle", "--directory", "state"])
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(output), counts)
            self.assertEqual(errors, "")
            cycle.assert_called_once_with(Path("state"))

    def test_disabled_cycle_cannot_send_or_activate(self):
        with patch("calendar_digest.control_email_automation._cycle") as cycle:
            code, output, errors = self.invoke(["control-email-cycle", "--directory", "state"])
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("CONTROL_EMAIL_ENABLED=false", errors)
        cycle.assert_not_called()

    def test_expected_failures_safe_unknown_failure_redacted_and_busy_preserved(self):
        for error, expected_code, expected_text in (
            (ControlEmailPreviewError("Безопасное сообщение"), 1, "Безопасное сообщение"),
            (ControlEmailCycleError("Настройка отключена"), 1, "Настройка отключена"),
            (ControlEmailQueueError("Очередь недоступна"), 1, "Очередь недоступна"),
            (CalendarBusy("Сбор уже выполняется"), 75, "Сбор уже выполняется"),
            (RuntimeError("private-recipient@example.test provider body"), 1, "требуется диагностика адаптера"),
        ):
            with self.subTest(error=type(error).__name__), patch.object(cli, "create_control_email_preview", side_effect=error):
                code, output, errors = self.invoke(["preview-control-email", "--date", "2026-10-19"])
            self.assertEqual(code, expected_code)
            self.assertEqual(output, "")
            self.assertIn(expected_text, errors)
            self.assertNotIn("private-recipient", errors)
            self.assertNotIn("Traceback", errors)

    def test_progress_accepts_only_fixed_enum_and_flushes(self):
        errors = StringIO()
        with redirect_stderr(errors), patch.object(cli, "print", wraps=print, create=True) as output:
            cli._control_email_progress("private-recipient@example.test")
            cli._control_email_progress(ControlEmailPreviewStage.COLLECT)
        self.assertEqual(errors.getvalue(), ControlEmailPreviewStage.COLLECT.value + "\n")
        self.assertEqual(output.call_count, 1)
        self.assertTrue(output.call_args.kwargs["flush"])


if __name__ == "__main__":
    unittest.main()
