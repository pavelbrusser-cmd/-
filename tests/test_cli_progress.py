from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from calendar_digest.cli import _preview_progress, main
from calendar_digest.preview import PreviewStage


class CLIProgressTests(unittest.TestCase):
    def test_preview_progress_is_flushed_to_stderr_and_stdout_stays_json(self):
        summary = {"date": "2026-10-15", "events": 2, "calendars": 11}

        def create(day, directory, *, progress):
            self.assertEqual(day, date(2026, 10, 15))
            self.assertEqual(directory, Path("synthetic-directory"))
            for stage in PreviewStage:
                progress(stage)
            return summary

        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch(
            "calendar_digest.cli.create_preview", side_effect=create
        ), patch("calendar_digest.cli.print", wraps=print, create=True) as printer:
            code = main(["preview", "--date", "2026-10-15", "--directory", "synthetic-directory"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), summary)
        self.assertEqual(stderr.getvalue().splitlines(), [stage.value for stage in PreviewStage])
        progress_calls = [call for call in printer.call_args_list if call.kwargs.get("file") is stderr]
        self.assertEqual(len(progress_calls), len(PreviewStage))
        self.assertTrue(all(call.kwargs.get("flush") is True for call in progress_calls))

    def test_raw_preview_error_remains_private_after_progress(self):
        def fail(day, directory, *, progress):
            progress(PreviewStage.COLLECT)
            raise RuntimeError("private event title, login and provider response")

        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), patch(
            "calendar_digest.cli.create_preview", side_effect=fail
        ):
            code = main(["preview", "--date", "2026-10-15", "--directory", "synthetic-directory"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(PreviewStage.COLLECT.value, stderr.getvalue())
        self.assertNotIn(PreviewStage.DONE.value, stderr.getvalue())
        self.assertNotIn("private", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertIn("требуется диагностика адаптера", stderr.getvalue())

    def test_non_preview_command_keeps_stderr_quiet(self):
        stdout, stderr = StringIO(), StringIO()
        summary = {"events": 1, "calendar_changes": False}
        with redirect_stdout(stdout), redirect_stderr(stderr), patch(
            "calendar_digest.cli.render_saved_preview", return_value=summary
        ):
            code = main(["render-preview", "--date", "2026-10-15", "--directory", "synthetic-directory"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), summary)
        self.assertEqual(stderr.getvalue(), "")

    def test_progress_printer_ignores_values_outside_fixed_stage_enum(self):
        stderr = StringIO()
        with redirect_stderr(stderr):
            _preview_progress("private calendar title")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
