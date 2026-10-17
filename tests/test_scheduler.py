from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from calendar_digest import scheduler


def moment(clock, day="2026-10-14"):
    clock = clock if len(clock) == 8 else clock + ":00"
    return datetime.fromisoformat(f"{day}T{clock}+03:00")


def row(state, *, retryable=False, day="2026-10-15", kind="daily"):
    return {"date": day, "kind": kind, "state": state, "retryable": retryable}


class SchedulerPolicyTests(unittest.TestCase):
    def test_prepare_send_and_cutoff_boundaries(self):
        cases = [("16:39:59", None, None), ("16:40", None, "prepare-delivery"),
                 ("16:40", row("ready"), None), ("16:59:59", row("ready"), None),
                 ("17:00", row("ready"), "send-ready"), ("17:29:59", row("ready"), "send-ready"),
                 ("17:30", row("ready"), None), ("17:30", None, None), ("23:59", None, None)]
        for clock, delivery, expected in cases:
            with self.subTest(clock=clock, delivery=delivery):
                self.assertEqual(scheduler.next_action(moment(clock), delivery), expected)

    def test_restart_catches_up_only_today_and_never_replays_submitted(self):
        now = moment("17:10")
        self.assertEqual(scheduler.next_action(now, None), "prepare-delivery")
        self.assertEqual(scheduler.next_action(now, row("preparing", retryable=True)), "prepare-delivery")
        self.assertEqual(scheduler.next_action(now, row("failed", retryable=True)), "prepare-delivery")
        for state in ("accepted", "uncertain", "submitting", "failed", "preparing", "unknown"):
            self.assertIsNone(scheduler.next_action(now, row(state)))
        self.assertIsNone(scheduler.next_action(now, row("ready", day="2026-10-14")))
        self.assertIsNone(scheduler.next_action(now, row("ready", kind="test")))

    def test_five_minute_retry_and_tomorrow_use_moscow_across_new_year(self):
        now = moment("17:00")
        retry = now + timedelta(minutes=5)
        self.assertIsNone(scheduler.next_action(moment("17:04"), row("failed", retryable=True), retry_after=retry))
        self.assertEqual(scheduler.next_action(moment("17:05"), row("failed", retryable=True), retry_after=retry),
                         "prepare-delivery")
        self.assertEqual(scheduler.digest_day(datetime(2026, 12, 31, 21, tzinfo=timezone.utc)), date(2027, 1, 2))
        self.assertEqual(scheduler.digest_day(moment("17:00", "2026-12-31")), date(2027, 1, 1))
        self.assertIsNone(scheduler.next_action(moment("00:00", "2027-01-01"), None))
        for day in ("2026-03-29", "2026-10-25"):
            utc = datetime.fromisoformat(f"{day}T14:00:00+00:00")
            ready = row("ready", day=(utc.date() + timedelta(days=1)).isoformat())
            self.assertEqual(scheduler.next_action(utc, ready), "send-ready")

    def test_naive_clock_is_rejected(self):
        with self.assertRaises(scheduler.SchedulerError):
            scheduler.next_action(datetime(2026, 10, 14, 17), None)

    def test_singleton_lock_and_private_heartbeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with scheduler.scheduler_lock(directory):
                with self.assertRaises(scheduler.SchedulerError):
                    with scheduler.scheduler_lock(directory):
                        self.fail("Second scheduler acquired the same lock")
                scheduler.write_heartbeat(directory, activity="preparing", now=moment("17:00"))
            with scheduler.scheduler_lock(directory):
                pass
            self.assertEqual(stat.S_IMODE((directory / scheduler.HEARTBEAT_NAME).stat().st_mode), 0o600)
            report = scheduler.scheduler_status(directory, now=moment("17:01"))
            self.assertTrue(report["heartbeat_recent"])
            self.assertEqual(report["activity"], "preparing")
            self.assertFalse(scheduler.scheduler_status(directory, now=moment("17:03"))["heartbeat_recent"])
            self.assertFalse(scheduler.scheduler_status(directory, now=moment("16:59"))["heartbeat_recent"])
            scheduler.write_heartbeat(directory, activity="stopped", now=moment("17:01"))
            self.assertFalse(scheduler.scheduler_status(directory, now=moment("17:01"))["heartbeat_recent"])

    def test_malformed_status_fails_closed_without_echoing_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / scheduler.HEARTBEAT_NAME
            for content in ("private-password", "[]", json.dumps({"updated_at": "private"}), "x" * 8193):
                path.write_text(content)
                report = scheduler.scheduler_status(directory, now=moment("17:00"))
                self.assertFalse(report["heartbeat_recent"])
                self.assertNotIn("private", json.dumps(report))

    def test_disabled_main_does_not_touch_smtp_or_filesystem(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("calendar_digest.runtime.validate_smtp_settings") as validate, \
                patch.object(scheduler, "scheduler_lock") as lock, redirect_stdout(StringIO()):
            self.assertEqual(scheduler.main(), 0)
        validate.assert_not_called()
        lock.assert_not_called()

    def test_imports_are_lightweight_in_a_fresh_process(self):
        script = (
            "import sys\n"
            "import calendar_digest.scheduler, calendar_digest.runtime\n"
            "from calendar_digest.delivery import delivery_status\n"
            "assert not any(name.split('.')[0] in {'caldav','icalendar','flask'} for name in sys.modules)\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class SchedulerProcessTests(unittest.TestCase):
    def test_early_preparation_remains_fresh_and_waits_until_seventeen_to_send(self):
        from calendar_digest import delivery
        from calendar_digest.smtp_delivery import MailSettings

        # Exercise the real outbox's expiry and before-DATA checks for both the
        # oldest possible ready message and a complete 15-minute preparation.
        for duration in (timedelta(0), timedelta(minutes=15)):
            with self.subTest(preparation_duration=duration):
                now = moment("16:39:59")
                stop = Mock()
                stop.is_set.return_value = False
                jobs = []
                submissions = []
                settings = MailSettings("sender@example.invalid", "synthetic-password",
                                        "recipient@example.invalid")

                def wait(seconds):
                    nonlocal now
                    now += timedelta(seconds=seconds)
                    self.assertLessEqual(now, moment("17:00"))

                stop.wait.side_effect = wait

                def preview(day, directory, **kwargs):
                    nonlocal now
                    now += duration
                    (directory / f"preview-{day.isoformat()}.json").write_text(json.dumps({
                        "date": day.isoformat(), "timezone": "Europe/Moscow", "events": [],
                    }), encoding="utf-8")

                def submit(settings, message, *, before_data):
                    before_data()
                    submissions.append(now)
                    stop.is_set.return_value = True

                def job(command, *, day, directory, timeout, **kwargs):
                    jobs.append((command, now))
                    self.assertEqual(timeout, 900)
                    if command == "prepare-delivery":
                        result = delivery.prepare_delivery(day, directory)
                    else:
                        result = delivery.submit_delivery(day, directory)
                    return 0 if result["state"] in {"ready", "accepted"} else 1

                with tempfile.TemporaryDirectory() as temporary, \
                        patch.dict(os.environ, {}, clear=True), \
                        patch.object(scheduler, "run_job", side_effect=job), \
                        patch.object(delivery.time, "time", side_effect=lambda: now.timestamp()), \
                        patch.object(delivery, "_utc_now", side_effect=lambda: now.astimezone(timezone.utc)), \
                        patch.object(delivery.MailSettings, "from_environment", return_value=settings), \
                        patch("calendar_digest.preview.create_preview", side_effect=preview), \
                        patch("calendar_digest.action_links.compose_action_email",
                              return_value=("<html>Example</html>", {"action_links": 0})), \
                        patch.object(delivery, "submit_message", side_effect=submit), \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    directory = Path(temporary)
                    scheduler.serve(directory, stop=stop, clock=lambda: now)
                    self.assertEqual(delivery.delivery_status(directory)["deliveries"][0]["state"], "accepted")
                self.assertEqual(jobs, [("prepare-delivery", moment("16:40")),
                                        ("send-ready", moment("17:00"))])
                self.assertEqual(submissions, [moment("17:00")])

    def test_job_timeout_terminates_then_kills_stuck_child_and_reaps(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("private", 5), 0]
        with patch.object(scheduler.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(scheduler.time, "monotonic", side_effect=[0, 901]):
            pulse = Mock()
            result = scheduler.run_job("prepare-delivery", day=date(2026, 10, 15), directory=Path("/tmp"),
                                       stop=threading.Event(), heartbeat=pulse)
        self.assertEqual(result, 1)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertNotIn("start_new_session", spawn.call_args.kwargs)
        self.assertEqual(spawn.call_args.args[0][-2:], ["--kind", "daily"])
        pulse.assert_called_once()

    def test_shutdown_stops_active_job_and_disabled_stop_does_not_spawn(self):
        stop = threading.Event()
        process = Mock()
        process.poll.return_value = None
        with patch.object(scheduler.subprocess, "Popen", return_value=process):
            result = scheduler.run_job("send-ready", day=date(2026, 10, 15), directory=Path("/tmp"),
                                       stop=stop, heartbeat=stop.set)
        self.assertEqual(result, 1)
        process.terminate.assert_called_once()
        with patch.object(scheduler.subprocess, "Popen") as spawn:
            scheduler.run_job("send-ready", day=date(2026, 10, 15), directory=Path("/tmp"),
                              stop=stop, heartbeat=Mock())
        spawn.assert_not_called()

    def test_restart_and_successful_preparation_send_once_without_overlap(self):
        stop = threading.Event()
        current = None
        calls = []

        def status(*_):
            return current

        def job(command, **kwargs):
            nonlocal current
            calls.append(command)
            kwargs["heartbeat"]()
            if command == "prepare-delivery":
                current = row("ready")
            else:
                current = row("accepted")
                stop.set()
            return 0

        with tempfile.TemporaryDirectory() as temporary, patch.object(scheduler, "_daily_status", side_effect=status), \
                patch.object(scheduler, "run_job", side_effect=job), redirect_stdout(StringIO()):
            scheduler.serve(Path(temporary), stop=stop, clock=lambda: moment("17:03"))
        self.assertEqual(calls, ["prepare-delivery", "send-ready"])

    def test_slow_prepare_past_cutoff_never_starts_send(self):
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.side_effect = lambda _: stop.is_set.configure_mock(return_value=True)
        now = moment("17:29")

        def job(command, **kwargs):
            nonlocal now
            self.assertEqual(command, "prepare-delivery")
            self.assertEqual(kwargs["timeout"], 60)
            now = moment("17:31")
            return 0

        with tempfile.TemporaryDirectory() as temporary, patch.object(scheduler, "_daily_status", return_value=None), \
                patch.object(scheduler, "run_job", side_effect=job) as run, redirect_stdout(StringIO()):
            scheduler.serve(Path(temporary), stop=stop, clock=lambda: now)
        self.assertEqual(run.call_count, 1)

    def test_calendar_busy_retries_after_thirty_seconds_other_failures_after_five_minutes(self):
        for first_code, delay in ((75, timedelta(seconds=30)), (1, timedelta(minutes=5))):
            with self.subTest(first_code=first_code):
                stop = Mock()
                stop.is_set.return_value = False
                now = moment("16:40")
                started = []

                def wait(seconds):
                    nonlocal now
                    self.assertGreater(seconds, 0)
                    now += timedelta(seconds=seconds)

                stop.wait.side_effect = wait

                def job(command, **kwargs):
                    self.assertEqual(command, "prepare-delivery")
                    started.append(now)
                    if len(started) == 1:
                        return first_code
                    stop.is_set.return_value = True
                    return 0

                with tempfile.TemporaryDirectory() as temporary, \
                        patch.object(scheduler, "_daily_status", return_value=None), \
                        patch.object(scheduler, "run_job", side_effect=job), \
                        redirect_stdout(StringIO()), redirect_stderr(StringIO()) as stderr:
                    scheduler.serve(Path(temporary), stop=stop, clock=lambda: now)
                self.assertEqual(started, [moment("16:40"), moment("16:40") + delay])
                if first_code == 75:
                    self.assertIn("30 секунд", stderr.getvalue())
                else:
                    self.assertNotIn("30 секунд", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
