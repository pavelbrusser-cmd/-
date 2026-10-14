from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calendar_digest import preview
from calendar_digest.calendar_collection import CalendarResource, Collection
from calendar_digest.preview import PreviewStage, create_preview
from calendar_digest.readonly_caldav import CalendarConnectionError


def collection():
    resource = CalendarResource(
        source_id="icloud", calendar_id="private-calendar-id", calendar_name="Private calendar name",
        account_email="private-account@example.test",
        data="BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
             "UID:private-event-id\r\nDTSTART:20261015T090000Z\r\n"
             "DURATION:PT1H\r\nSUMMARY:Private event title\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
    )
    return Collection([resource], 1)


class PreviewProgressTests(unittest.TestCase):
    def test_fixed_stages_precede_work_and_completion_follows_saved_files(self):
        stages = []
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def progress(stage):
                self.assertIsInstance(stage, PreviewStage)
                stages.append(stage)
                if stage is PreviewStage.DONE:
                    self.assertTrue((directory / "preview-2026-10-15.json").is_file())
                    self.assertTrue((directory / "calendar-state.json").is_file())

            def guarded(function, stage):
                def run(*args, **kwargs):
                    self.assertEqual(stages[-1], stage)
                    return function(*args, **kwargs)
                return run

            with (
                patch.object(preview, "read_snapshot", side_effect=guarded(preview.read_snapshot, PreviewStage.READ_STATE)),
                patch.object(preview, "collect_all", side_effect=guarded(collection, PreviewStage.COLLECT)),
                patch.object(preview, "advance_snapshot", side_effect=guarded(preview.advance_snapshot, PreviewStage.COMPARE)),
                patch.object(preview, "select_day", side_effect=guarded(preview.select_day, PreviewStage.SELECT)),
                patch.object(preview, "render_markdown", side_effect=guarded(preview.render_markdown, PreviewStage.RENDER)),
                patch.object(preview, "write_private", side_effect=guarded(preview.write_private, PreviewStage.SAVE)),
            ):
                result = create_preview(date(2026, 10, 15), directory, progress=progress)
        self.assertEqual(stages, list(PreviewStage))
        self.assertEqual(result["events"], 1)
        messages = " ".join(stage.value for stage in stages)
        self.assertNotIn("private", messages.casefold())
        self.assertNotIn("icloud", messages.casefold())
        self.assertNotIn("VEVENT", messages)

    def test_collection_failure_stops_progress_before_success(self):
        stages = []
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            preview, "collect_all", side_effect=CalendarConnectionError("Недоступен источник.")
        ):
            directory = Path(temporary)
            with self.assertRaises(CalendarConnectionError):
                create_preview(date(2026, 10, 15), directory, progress=stages.append)
            self.assertFalse((directory / "preview-2026-10-15.json").exists())
        self.assertEqual(stages, [PreviewStage.READ_STATE, PreviewStage.COLLECT])

    def test_observer_errors_do_not_break_preview_or_leak_exception_text(self):
        attempted = []

        def broken_observer(stage):
            attempted.append(stage)
            raise RuntimeError("private observer detail must not appear")

        stdout, stderr = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            preview, "collect_all", side_effect=collection
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            result = create_preview(date(2026, 10, 15), Path(temporary), progress=broken_observer)
        self.assertEqual(result["events"], 1)
        self.assertEqual(attempted, list(PreviewStage))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
