from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from calendar_digest import runtime


class RuntimeTests(unittest.TestCase):
    def test_schedule_is_disabled_by_default_and_rejects_ambiguous_values(self):
        self.assertFalse(runtime.schedule_enabled({}))
        self.assertFalse(runtime.schedule_enabled({"DIGEST_SCHEDULE_ENABLED": "false"}))
        self.assertTrue(runtime.schedule_enabled({"DIGEST_SCHEDULE_ENABLED": "true"}))
        for value in ("1", "yes", "TRUE", " true", "", "secret-value"):
            with self.subTest(value=value), self.assertRaises(runtime.RuntimeConfigurationError) as error:
                runtime.schedule_enabled({"DIGEST_SCHEDULE_ENABLED": value})
            self.assertEqual(str(error.exception), "DIGEST_SCHEDULE_ENABLED должен быть true или false.")

    def test_port_is_a_valid_tcp_port_and_never_echoes_bad_input(self):
        self.assertEqual(runtime.service_port({}), 10000)
        self.assertEqual(runtime.service_port({"PORT": "65535"}), 65535)
        for value in ("0", "65536", "-1", "1;private", "１", "1.5"):
            with self.subTest(value=value), self.assertRaises(runtime.RuntimeConfigurationError) as error:
                runtime.service_port({"PORT": value})
            self.assertNotIn("private", str(error.exception))

    def test_disabled_main_only_starts_web_and_never_reads_smtp(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime, "validate_smtp_settings") as validate, \
                patch.object(runtime, "supervise", return_value=0) as supervisor, redirect_stdout(StringIO()):
            self.assertEqual(runtime.main(), 0)
        validate.assert_not_called()
        commands = supervisor.call_args.args[0]
        self.assertEqual(len(commands), 1)
        self.assertIn("calendar_digest.action_web:create_app()", commands[0])
        self.assertEqual(commands[0][commands[0].index("--workers") + 1], "1")
        self.assertEqual(commands[0][commands[0].index("--threads") + 1], "4")

    def test_enabled_configuration_checked_before_start_and_error_is_private(self):
        stderr = StringIO()
        with patch.dict(os.environ, {"DIGEST_SCHEDULE_ENABLED": "true"}, clear=True), \
                patch.object(runtime, "validate_smtp_settings", side_effect=RuntimeError("private-password")), \
                patch.object(runtime, "supervise") as supervisor, redirect_stderr(stderr):
            self.assertEqual(runtime.main(), 1)
        supervisor.assert_not_called()
        self.assertNotIn("private-password", stderr.getvalue())

    def test_main_handles_sigterm_and_restores_previous_handlers(self):
        before = signal.getsignal(signal.SIGTERM)

        def stop_supervisor(*_, stop, **__):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(stop.is_set())
            return 0

        with patch.dict(os.environ, {}, clear=True), \
                patch.object(runtime, "supervise", side_effect=stop_supervisor), redirect_stdout(StringIO()):
            self.assertEqual(runtime.main(), 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_exited_child_stops_every_group_and_signals_dead_leader_too(self):
        child1, child2 = Mock(pid=101), Mock(pid=102)
        child1.poll.return_value = 1
        child2.poll.return_value = 0
        with patch.object(runtime.subprocess, "Popen", side_effect=[child1, child2]) as spawn, \
                patch.object(runtime.os, "killpg") as kill, redirect_stderr(StringIO()):
            result = runtime.supervise([["web"], ["scheduler"]], root=Path("/tmp"), stop=threading.Event())
        self.assertEqual(result, 1)
        self.assertTrue(all(call.kwargs["start_new_session"] for call in spawn.call_args_list))
        self.assertEqual(kill.call_args_list[0].args, (101, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[1].args, (102, signal.SIGTERM))
        self.assertEqual(kill.call_args_list[-1].args, (102, signal.SIGKILL))
        child1.wait.assert_called_once()
        child2.wait.assert_called_once()

    def test_second_spawn_failure_still_cleans_first_child(self):
        child = Mock(pid=101)
        child.poll.return_value = 0
        with patch.object(runtime.subprocess, "Popen", side_effect=[child, OSError("private")]), \
                patch.object(runtime.os, "killpg") as kill, redirect_stderr(StringIO()) as stderr:
            result = runtime.supervise([["web"], ["scheduler"]], root=Path("/tmp"), stop=threading.Event())
        self.assertEqual(result, 1)
        self.assertEqual(kill.call_count, 2)
        self.assertNotIn("private", stderr.getvalue())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Render uses Linux process groups")
    def test_real_scheduler_death_also_stops_its_running_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            web_pid, scheduler_pid, job_pid = [directory / name for name in ("web", "scheduler", "job")]
            web = f"import os,time;open({str(web_pid)!r},'w').write(str(os.getpid()));time.sleep(60)"
            job = f"import os,time;open({str(job_pid)!r},'w').write(str(os.getpid()));time.sleep(60)"
            scheduler = (
                "import os,subprocess,sys,time\n"
                f"open({str(scheduler_pid)!r},'w').write(str(os.getpid()))\n"
                f"subprocess.Popen([sys.executable,'-c',{job!r}])\n"
                f"while not os.path.exists({str(job_pid)!r}): time.sleep(0.01)\n"
                "os._exit(7)\n"
            )
            supervisor = (
                "from pathlib import Path\nimport sys,threading\n"
                "from calendar_digest.runtime import supervise\n"
                f"raise SystemExit(supervise([[sys.executable,'-c',{web!r}],"
                f"[sys.executable,'-c',{scheduler!r}]],root=Path({temporary!r}),stop=threading.Event()))"
            )
            try:
                result = subprocess.run([sys.executable, "-c", supervisor], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stderr.decode())
                for path in (web_pid, job_pid):
                    self.assertTrue(path.exists())
                    pid = int(path.read_text())
                    state = Path(f"/proc/{pid}/stat")
                    self.assertTrue(not state.exists() or state.read_text().split(") ", 1)[1].startswith("Z"),
                                    f"Process {pid} is still running")
            finally:
                for path in (web_pid, scheduler_pid):
                    if path.exists():
                        try:
                            os.killpg(int(path.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
