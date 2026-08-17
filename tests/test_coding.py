"""The action-capable code tool: accomplish_with_code.

Unlike solve_with_python (compute only), this one writes a program that may act
on the machine. Two properties matter enough to guard:

  * a cancel must actually STOP the running program — the whole reason it runs in
    a subprocess rather than an in-process exec, which could not be interrupted;
  * a failing script is reported honestly, never silently "repaired" and re-run,
    because a re-run could redo a side effect the first attempt already caused.

Nothing here touches the network: the code-generation call is mocked, and the
script that "runs" is a trivial local one.
"""

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cancellation  # noqa: E402
import llm_agent  # noqa: E402
from tools import coding  # noqa: E402


class TestAccomplishWithCode(unittest.TestCase):
    def setUp(self):
        cancellation.reset()
        self.addCleanup(cancellation.reset)
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self._folder = mock.patch.object(
            coding, "_solution_folder", return_value=self.tmp)
        self._folder.start()
        self.addCleanup(self._folder.stop)

    def _generates(self, code):
        return mock.patch.object(
            coding.gemini_client, "generate_content",
            return_value=mock.Mock(text=code))

    def test_runs_generated_code_and_reports_its_output(self):
        with self._generates("print('did the thing')"):
            result = coding.accomplish_with_code("do the thing")
        self.assertTrue(result.startswith("Done"), result)
        self.assertIn("did the thing", result)
        # The program is saved for the user to inspect.
        self.assertTrue((self.tmp / "action.py").exists())
        self.assertTrue((self.tmp / "output.txt").exists())

    def test_a_cancel_before_running_aborts(self):
        cancellation.request_cancel()
        with self._generates("print('should not run')") as gen:
            result = coding.accomplish_with_code("do the thing")
        self.assertIn("cancel", result.lower())
        gen.assert_called_once()

    def test_a_failing_script_is_reported_not_auto_repaired(self):
        with self._generates("raise SystemExit(2)") as gen:
            result = coding.accomplish_with_code("do the thing")
        # Generated exactly once — no second "repair" round, because re-running a
        # script that may have acted on the system could redo the side effect.
        gen.assert_called_once()
        self.assertIn("didn't fully succeed", result)


class TestRunCancellable(unittest.TestCase):
    """The subprocess runner is what makes Esc/corner cancel real."""

    def setUp(self):
        cancellation.reset()
        self.addCleanup(cancellation.reset)

    def test_cancel_stops_a_long_running_script_promptly(self):
        folder = pathlib.Path(tempfile.mkdtemp())
        script = folder / "slow.py"
        script.write_text("import time\nfor _ in range(300):\n    time.sleep(0.1)\n",
                          encoding="utf-8")
        cancellation.request_cancel()  # already cancelled: the poll must catch it
        start = time.monotonic()
        ok, output = coding._run_cancellable(script, timeout=30)
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertLess(
            elapsed, 5.0,
            "cancel did not terminate the running child promptly — an "
            "uninterruptible run defeats the whole point of the subprocess.")
        self.assertIn("cancel", output.lower())

    def test_timeout_stops_a_runaway_script(self):
        folder = pathlib.Path(tempfile.mkdtemp())
        script = folder / "slow.py"
        script.write_text("import time\nfor _ in range(300):\n    time.sleep(0.1)\n",
                          encoding="utf-8")
        ok, output = coding._run_cancellable(script, timeout=1)
        self.assertFalse(ok)
        self.assertIn("time limit", output.lower())


class TestRegistered(unittest.TestCase):
    def test_tool_is_in_the_registry(self):
        names = {fn.__name__ for fn in llm_agent._TOOLS}
        self.assertIn("accomplish_with_code", names)
        # The safe compute tool stays alongside it, not replaced by it.
        self.assertIn("solve_with_python", names)


if __name__ == "__main__":
    unittest.main()
