from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from calendar_digest import control_email_scheduler as scheduler


def moment(clock="12:00:00"):
    return datetime.fromisoformat(f"2026-10-15T{clock}+03:00")


class AdvancingStop:
    """A deterministic clock and event; never sleep during scheduler tests."""

    def __init__(self, now, end):
        self.now = now
        self.end = end
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        if delay <= 0:
            raise AssertionError("Scheduler must not busy-loop")
        self.waits.append(delay)
        self.now = min(self.end, self.now + timedelta(seconds=delay))
        if self.now >= self.end:
            self.set()
        return self.stopped


class ControlEmailSchedulerPolicyTests(unittest.TestCase):
    def test_refresh_and_due_boundaries_are_timezone_aware(self):
        now = moment()
        self.assertEqual(scheduler.next_run(now, {"next_due_at": None}), moment("12:05:00"))
        self.assertEqual(scheduler.next_run(now, {"next_due_at": "2026-10-15T09:01:15+00:00"}),
                         moment("12:01:15"))
        self.assertEqual(scheduler.next_run(now, {"next_due_at": moment("12:10:00").isoformat()}),
                         moment("12:05:00"))
        self.assertEqual(scheduler.next_run(now, {"next_due_at": moment("12:00:01").isoformat()}),
                         moment("12:00:01"))

    def test_already_due_item_never_causes_busy_loop(self):
        for due in (moment("11:00:00"), moment()):
            self.assertEqual(scheduler.next_run(moment(), {"next_due_at": due.isoformat()}),
                             moment("12:00:30"))

    def test_night_due_times_wait_for_seven_moscow_while_refresh_continues(self):
        for due in ('2026-10-15T02:00:00+00:00', '2026-10-15T06:59:59+03:00',
                    '2026-10-15T07:00:00+03:00'):
            with self.subTest(due=due):
                self.assertEqual(scheduler.next_run(moment('06:59:00').astimezone(timezone.utc),
                                                    {'next_due_at': due}), moment('07:00:00'))
        self.assertEqual(scheduler.next_run(moment('03:00:00'),
                                           {'next_due_at': moment('05:00:00').isoformat()}),
                         moment('03:05:00'))
        self.assertEqual(scheduler.next_run(moment('07:00:00'),
                                           {'next_due_at': moment('07:00:00').isoformat()}),
                         moment('07:00:30'))

    def test_evening_overdue_item_does_not_trigger_thirty_second_night_refreshes(self):
        self.assertEqual(scheduler.next_run(moment('00:01:00'),
                                           {'next_due_at': '2026-10-14T23:59:00+03:00'}),
                         moment('00:06:00'))
        self.assertEqual(scheduler.next_run(moment('23:59:50'),
                                           {'next_due_at': moment('23:59:50').isoformat()}),
                         datetime.fromisoformat('2026-10-16T00:04:50+03:00'))

    def test_malformed_due_time_fails_without_echoing_values(self):
        for value in ("private", "2026-10-15T12:00:00", {}, 42, []):
            with self.subTest(value=value), self.assertRaises(scheduler.ControlEmailSchedulerError) as error:
                scheduler.next_run(moment(), {"next_due_at": value})
            self.assertNotIn("private", str(error.exception))
        with self.assertRaises(scheduler.ControlEmailSchedulerError):
            scheduler.next_run(datetime(2026, 10, 15), {})

    def test_singleton_lock_private_file_and_provider_directory_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            directory.chmod(0o2775)
            with scheduler.scheduler_lock(directory):
                with self.assertRaises(scheduler.ControlEmailSchedulerError):
                    with scheduler.scheduler_lock(directory):
                        self.fail("Second participant-email scheduler acquired the same lock")
            with scheduler.scheduler_lock(directory):
                pass
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o2775)
            self.assertEqual(stat.S_IMODE((directory / ".control-email-scheduler.lock").stat().st_mode), 0o600)

    def test_lock_rejects_symlinks_and_hardlinks(self):
        for linked in ("symlink", "hardlink"):
            with self.subTest(linked=linked), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                target = directory / "target"
                target.write_text("private")
                target.chmod(0o640)
                lock = directory / ".control-email-scheduler.lock"
                if linked == "symlink":
                    lock.symlink_to(target)
                else:
                    os.link(target, lock)
                with self.assertRaises(scheduler.ControlEmailSchedulerError):
                    with scheduler.scheduler_lock(directory):
                        self.fail("Unsafe lock accepted")
                self.assertEqual(target.read_text(), "private")
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_heartbeat_reports_recent_activity_without_delivery_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            scheduler.write_heartbeat(directory, activity="syncing", now=moment())
            self.assertEqual(stat.S_IMODE((directory / scheduler.HEARTBEAT_NAME).stat().st_mode), 0o600)
            report = scheduler.control_email_scheduler_status(directory, now=moment("12:02:00"))
            self.assertEqual(report, {
                "heartbeat_at": moment().isoformat(), "heartbeat_recent": True,
                "activity": "syncing", "timezone": "Europe/Moscow",
            })
            for now in (moment("12:02:01"), moment("11:59:59")):
                self.assertFalse(scheduler.control_email_scheduler_status(directory, now=now)["heartbeat_recent"])
            scheduler.write_heartbeat(directory, activity="stopped", now=moment())
            self.assertFalse(scheduler.control_email_scheduler_status(directory, now=moment())["heartbeat_recent"])
            scheduler.write_heartbeat(directory, activity="error", now=moment())
            report = scheduler.control_email_scheduler_status(directory, now=moment())
            self.assertTrue(report["heartbeat_recent"])
            self.assertEqual(report["activity"], "error")

    def test_malformed_heartbeat_never_exposes_file_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            path = directory / scheduler.HEARTBEAT_NAME
            self.assertFalse(scheduler.control_email_scheduler_status(directory, now=moment())["heartbeat_recent"])
            for content in ("private", "[]", "x" * 8193, json.dumps({"updated_at": "private"}),
                            json.dumps({"updated_at": moment().isoformat(), "activity": ["private"]}),
                            json.dumps({"updated_at": moment().isoformat(), "activity": "private"})):
                path.write_text(content)
                report = scheduler.control_email_scheduler_status(directory, now=moment())
                self.assertFalse(report["heartbeat_recent"])
                self.assertNotIn("private", json.dumps(report))

    def test_disabled_main_has_no_filesystem_or_smtp_configuration_access(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(scheduler, "scheduler_lock") as lock, \
                patch.object(scheduler, "serve") as serve, redirect_stdout(StringIO()):
            self.assertEqual(scheduler.main(), 0)
        lock.assert_not_called()
        serve.assert_not_called()

    def test_missing_smtp_credentials_do_not_stop_scheduler_at_startup(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"CONTROL_EMAIL_ENABLED": "true", "DIGEST_STATE_DIR": temporary}, clear=True), \
                    patch.object(scheduler, "serve") as serve:
                self.assertEqual(scheduler.main(), 0)
            serve.assert_called_once()
            self.assertEqual(scheduler.control_email_scheduler_status(Path(temporary))["activity"], "stopped")

    def test_main_handles_sigterm_and_restores_handlers(self):
        before = signal.getsignal(signal.SIGTERM)

        def stop_loop(*_, stop, **__):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(stop.is_set())

        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"CONTROL_EMAIL_ENABLED": "true", "DIGEST_STATE_DIR": temporary}, clear=True), \
                patch.object(scheduler, "serve", side_effect=stop_loop):
            self.assertEqual(scheduler.main(), 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_imports_are_lightweight_in_fresh_process(self):
        script = (
            "import sys\nimport calendar_digest.runtime, calendar_digest.control_email_scheduler\n"
            "assert not any(name.split('.')[0] in "
            "{'caldav','icalendar','flask','cryptography','requests'} for name in sys.modules)\n"
            "assert 'calendar_digest.smtp_delivery' not in sys.modules\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class ControlEmailSchedulerProcessTests(unittest.TestCase):
    def test_timeout_terminates_then_kills_and_does_not_create_new_process_group(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("private", 5), 0]
        with patch.object(scheduler.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(scheduler.time, "monotonic", side_effect=[0, 901]):
            pulse = Mock()
            result = scheduler.run_job(directory=Path("/var/data"), stop=threading.Event(), heartbeat=pulse)
        self.assertEqual(result, 1)
        self.assertEqual(spawn.call_args.args[0][-3:], ["control-email-cycle", "--directory", "/var/data"])
        self.assertNotIn("start_new_session", spawn.call_args.kwargs)
        self.assertEqual(spawn.call_args.kwargs["stdout"], subprocess.DEVNULL)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        pulse.assert_called_once()

    def test_stop_cleans_running_job_and_already_stopped_loop_spawns_nothing(self):
        process = Mock()
        process.poll.return_value = None
        stop = threading.Event()
        with patch.object(scheduler.subprocess, "Popen", return_value=process):
            result = scheduler.run_job(directory=Path("/tmp"), stop=stop, heartbeat=stop.set)
        self.assertEqual(result, 1)
        process.terminate.assert_called_once()
        with patch.object(scheduler.subprocess, "Popen") as spawn:
            self.assertEqual(scheduler.run_job(directory=Path("/tmp"), stop=stop, heartbeat=Mock()), 1)
        spawn.assert_not_called()

    def test_active_job_pulses_every_fifteen_seconds_and_returns_exit_code(self):
        process = Mock()
        process.poll.side_effect = [None, None, None, None, None, 7, 7]
        with patch.object(scheduler.subprocess, "Popen", return_value=process), \
                patch.object(scheduler.time, "monotonic", side_effect=[0, 0, 1, 14, 15, 29, 30]):
            pulse = Mock()
            result = scheduler.run_job(directory=Path("/tmp"), stop=Mock(is_set=lambda: False), heartbeat=pulse)
        self.assertEqual(result, 7)
        self.assertEqual(pulse.call_count, 3)
        process.terminate.assert_not_called()
        process.wait.assert_called_once_with(timeout=2)

    def test_cycles_run_at_startup_next_due_and_five_minute_refresh(self):
        stop = AdvancingStop(moment(), moment("12:07:01"))
        starts = []
        summaries = [{"next_due_at": moment("12:02:00").isoformat()}, {"next_due_at": None},
                     {"next_due_at": None}]

        def run(**kwargs):
            starts.append(stop.now)
            kwargs["heartbeat"]()
            return 0

        with tempfile.TemporaryDirectory() as temporary, patch.object(scheduler, "run_job", side_effect=run), \
                patch.object(scheduler, "_queue_summary", side_effect=summaries):
            scheduler.serve(Path(temporary), stop=stop, clock=lambda: stop.now)
        self.assertEqual(starts, [moment(), moment("12:02:00"), moment("12:07:00")])
        self.assertTrue(all(delay <= 30 for delay in stop.waits))

    def test_cycle_failure_retries_in_minute_without_reading_queue(self):
        stop = AdvancingStop(moment(), moment("12:01:01"))
        starts = []

        def run(**kwargs):
            starts.append(stop.now)
            kwargs["heartbeat"]()
            return 1

        with tempfile.TemporaryDirectory() as temporary, patch.object(scheduler, "run_job", side_effect=run), \
                patch.object(scheduler, "_queue_summary") as summary, redirect_stderr(StringIO()):
            scheduler.serve(Path(temporary), stop=stop, clock=lambda: stop.now)
            report = scheduler.control_email_scheduler_status(Path(temporary), now=stop.now)
        self.assertEqual(starts, [moment(), moment("12:01:00")])
        self.assertEqual(report["activity"], "error")
        self.assertTrue(report["heartbeat_recent"])
        summary.assert_not_called()

    def test_restart_before_seven_refreshes_immediately_and_runs_at_seven(self):
        stop = AdvancingStop(moment('06:59:00'), moment('07:00:01'))
        starts = []
        summaries = [{'next_due_at': '2026-10-15T02:00:00+00:00'}, {'next_due_at': None}]

        def run(**kwargs):
            starts.append(stop.now)
            kwargs['heartbeat']()
            return 0

        with tempfile.TemporaryDirectory() as temporary, patch.object(scheduler, 'run_job', side_effect=run), \
                patch.object(scheduler, '_queue_summary', side_effect=summaries):
            scheduler.serve(Path(temporary), stop=stop, clock=lambda: stop.now.astimezone(timezone.utc))
        self.assertEqual(starts, [moment('06:59:00'), moment('07:00:00')])
        self.assertTrue(all(delay <= 30 for delay in stop.waits))

    def test_queue_read_failure_is_private_and_retried_in_minute(self):
        stop = AdvancingStop(moment(), moment("12:01:01"))
        with tempfile.TemporaryDirectory() as temporary, patch.object(scheduler, "run_job", return_value=0) as run, \
                patch.object(scheduler, "_queue_summary", side_effect=RuntimeError("private-key")), \
                redirect_stderr(StringIO()) as stderr:
            scheduler.serve(Path(temporary), stop=stop, clock=lambda: stop.now)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("private-key", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
