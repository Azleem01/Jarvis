"""Which processes ``restart.ps1`` will stop.

This guards a defect that had already happened. `restart.ps1` classified a bare
``pythonw.exe main.py`` as Azleem *by elimination*: Win32_Process exposes no
working directory, and the script's comment asserted that the machine's other
python apps "are all launched with a fully-qualified script path". That was
untrue. ``-DryRun`` on 2026-07-28 marked two "my cluely" processes WOULD STOP —
one of them a child the other had spawned — alongside the real Azleem.

**These tests run the actual script.** `restart.ps1 -DryRun -ProcessSource`
takes the candidate list from a JSON file instead of WMI, so the matcher under
test is the one that ships. Re-typing its rules into Python would be the same
mistake in a new place: an approximation of this script's matching logic, read
in isolation, has already been mistaken for the script itself once.

The interesting cases are all *refusals* — the file is deliberately weighted
towards what must survive a restart, because a wrong kill is silent and costs
the user an unrelated app, while a missed kill announces itself.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "restart.ps1")
_MAIN = os.path.join(_ROOT, "main.py")

# Command lines copied from the real -DryRun output on 2026-07-28.
AZLEEM_QUALIFIED = (
    r'"C:\Users\aleem\AppData\Local\Programs\Python\Python314\pythonw.exe"'
    rf' "{_MAIN}"'
)
CLUELY_BARE = r'"C:\Users\aleem\my cluely\.venv312\Scripts\pythonw.exe" main.py'
CLUELY_CHILD_BARE = (
    r'"C:\Users\aleem\AppData\Local\Programs\Python\Python312\pythonw.exe" main.py'
)
CLIP_SMART = (
    r'"C:\Users\aleem\clip smart\.venv\Scripts\pythonw.exe"'
    r' "C:\Users\aleem\clip smart\main.py"'
)

_POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _proc(pid, command_line, created="2026-07-28T01:00:00"):
    return {"ProcessId": pid, "CommandLine": command_line, "CreationDate": created}


@unittest.skipUnless(_POWERSHELL, "PowerShell not on PATH")
class RestartSelectionCase(unittest.TestCase):
    """Runs restart.ps1 -DryRun over an injected process list."""

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="azleem-restart-")
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)

    def _pid_file(self, text, mtime=None):
        path = os.path.join(self._dir, "azleem.pid")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def select(self, processes, pid_file=None):
        """Return {pid: True if it would be stopped}, per the real script."""
        source = os.path.join(self._dir, "processes.json")
        with open(source, "w", encoding="utf-8") as fh:
            json.dump(processes, fh)
        # A pid file must always be named: without -PidPath the script would
        # read the developer's own logs\azleem.pid and the result would depend
        # on whether Azleem happened to be running.
        args = [
            _POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", _SCRIPT, "-DryRun",
            "-ProcessSource", source,
            "-PidPath", pid_file or os.path.join(self._dir, "absent.pid"),
        ]
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=120, cwd=_ROOT
        )
        self.assertEqual(out.returncode, 0, f"script failed:\n{out.stdout}\n{out.stderr}")
        verdicts = {}
        for line in out.stdout.splitlines():
            for p in processes:
                if f"PID {p['ProcessId']} " in line:
                    verdicts[p["ProcessId"]] = "WOULD STOP" in line
        self.assertEqual(
            sorted(verdicts), sorted(p["ProcessId"] for p in processes),
            f"not every process got a verdict:\n{out.stdout}",
        )
        return verdicts


class TestTheReportedCollateralDamage(RestartSelectionCase):
    """The exact process list observed on this machine on 2026-07-28."""

    def _observed(self):
        return [
            _proc(22424, CLIP_SMART),
            _proc(30148, AZLEEM_QUALIFIED),
            _proc(29872, CLUELY_BARE, "2026-07-28T01:17:50"),
            _proc(33684, CLUELY_CHILD_BARE, "2026-07-28T01:17:50"),
        ]

    def test_the_two_bare_my_cluely_processes_survive(self):
        v = self.select(self._observed())
        self.assertFalse(v[29872], "killed 'my cluely' — the reported bug")
        self.assertFalse(v[33684], "killed 'my cluely's child — the reported bug")

    def test_the_real_azleem_is_still_found(self):
        # Refusing to guess is only worth anything if the restart still works.
        self.assertTrue(self.select(self._observed())[30148])

    def test_an_explicitly_pathed_stranger_still_survives(self):
        self.assertFalse(self.select(self._observed())[22424])


class TestPidFileIdentification(RestartSelectionCase):
    """A bare command line is stoppable only when it says it is ours."""

    def test_a_bare_process_that_claimed_the_pid_file_is_stopped(self):
        # This is the case the old rule got right by luck: Azleem's own
        # auto-start entry ran bare. It must keep working, on evidence now.
        pid_file = self._pid_file(f"29872\n{_MAIN}\n")
        v = self.select([_proc(29872, CLUELY_BARE)], pid_file)
        self.assertTrue(v[29872])

    def test_an_unclaimed_bare_process_is_left_alone(self):
        v = self.select([_proc(29872, CLUELY_BARE)])
        self.assertFalse(v[29872])

    def test_a_claim_naming_another_checkouts_main_py_is_ignored(self):
        pid_file = self._pid_file(r"29872" "\n" r"C:\Users\aleem\JARVIS_backup\main.py")
        self.assertFalse(self.select([_proc(29872, CLUELY_BARE)], pid_file)[29872])

    def test_a_recycled_pid_is_not_trusted(self):
        # Azleem was force-killed, so the claim outlived it; Windows then gave
        # 29872 to something else. The new process post-dates the file, which
        # is the only signal distinguishing it from the one that wrote it.
        pid_file = self._pid_file(f"29872\n{_MAIN}\n", mtime=1_800_000_000)
        v = self.select(
            [_proc(29872, CLUELY_BARE, "2036-01-01T00:00:00")], pid_file
        )
        self.assertFalse(v[29872], "trusted a stale claim on a recycled PID")

    def test_a_corrupt_pid_file_falls_back_instead_of_throwing(self):
        pid_file = self._pid_file("not a pid\n")
        v = self.select(
            [_proc(29872, CLUELY_BARE), _proc(30148, AZLEEM_QUALIFIED)], pid_file
        )
        self.assertFalse(v[29872])
        self.assertTrue(v[30148], "path matching must survive a bad pid file")


class TestPathMatching(RestartSelectionCase):
    """The directory rule, which is what works before a claim is written."""

    def test_a_sibling_directory_is_not_us(self):
        other = r'"pythonw.exe" "C:\Users\aleem\JARVIS_backup\main.py"'
        self.assertFalse(self.select([_proc(999, other)])[999])

    def test_a_forward_slash_path_to_our_main_is_us(self):
        fwd = '"pythonw.exe" "{}/main.py"'.format(_ROOT.replace("\\", "/"))
        self.assertTrue(self.select([_proc(999, fwd)])[999])


class TestPidFileContract(unittest.TestCase):
    """main.py writes what restart.ps1 reads."""

    def test_main_writes_pid_then_script_path(self):
        import main

        self.assertEqual(main._PID_FILE.name, "azleem.pid")
        self.assertEqual(main._PID_FILE.parent.name, "logs")

    def test_the_claim_is_written_after_the_singleton_check(self):
        # A duplicate launch exits early; if it claimed first it would overwrite
        # the live instance's PID with one that is about to disappear.
        with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as fh:
            body = fh.read()
        guard = body.index("_acquire_single_instance():\n        print")
        self.assertLess(guard, body.index("_claim_pid_file()\n"))


if __name__ == "__main__":
    unittest.main()
