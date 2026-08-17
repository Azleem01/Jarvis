"""Self-extension: writing a new tool, gating it behind confirm, restarting.

The risk this guards is specific and serious — Azleem editing its own codebase
and relaunching. So the tests pin the safety properties directly: arming never
restarts, confirming without arming never restarts, a tool that fails its checks
is rolled back and never installed, and only a clean tool reaches the restart.
The restart itself and the Gemini call are stubbed; what's under test is the
decision logic around them.
"""

import json
import os
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cancellation  # noqa: E402
import config  # noqa: E402
import self_restart  # noqa: E402
from tools import self_extend  # noqa: E402
from tools import generated  # noqa: E402

# A minimal module that satisfies the generated-tool convention.
_CANNED = (
    "def send_email(to: str, body: str) -> str:\n"
    '    """Send an email to a person."""\n'
    "    return 'sent'\n"
    "\n"
    "TOOL = send_email\n"
    'ROUTING = "- send_email: email a person."\n'
)


class SelfExtendTestCase(unittest.TestCase):
    def setUp(self):
        cancellation.reset()
        self_extend._reset_for_tests()
        self.tmp = Path(mkdtemp())
        self.addCleanup(rmtree, self.tmp, True)
        # Write generated tools into a throwaway dir, never the real package.
        p = mock.patch.object(self_extend, "_GENERATED_DIR", self.tmp)
        p.start()
        self.addCleanup(p.stop)
        # Keep self-extension enabled by default for the arm/confirm tests.
        p2 = mock.patch.object(config, "SELF_EXTEND_ENABLED", True)
        p2.start()
        self.addCleanup(p2.stop)

    def _arm(self, task="email my brother"):
        with mock.patch.object(self_extend, "_generate", return_value=_CANNED):
            return self_extend.add_capability(task)


class TestArming(SelfExtendTestCase):
    def test_arm_writes_the_tool_but_does_not_restart(self):
        with mock.patch.object(self_restart, "spawn_restart") as restart:
            reply = self._arm()
        self.assertIn("confirm", reply.lower())
        self.assertTrue((self.tmp / "send_email.py").exists(),
                        "Arming must stage the candidate tool on disk.")
        self.assertIsNotNone(self_extend._staged)
        restart.assert_not_called()

    def test_disabled_refuses_without_writing_anything(self):
        with mock.patch.object(config, "SELF_EXTEND_ENABLED", False), \
             mock.patch.object(self_extend, "_generate") as gen:
            reply = self_extend.add_capability("do a new thing")
        self.assertIn("off", reply.lower())
        gen.assert_not_called()
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_a_tool_it_cannot_parse_is_refused(self):
        with mock.patch.object(self_extend, "_generate", return_value="not python {{{"):
            reply = self_extend.add_capability("something")
        self.assertIn("valid tool", reply.lower())
        self.assertIsNone(self_extend._staged)

    def test_a_future_annotations_tool_is_refused(self):
        bad = "from __future__ import annotations\n" + _CANNED
        with mock.patch.object(self_extend, "_generate", return_value=bad):
            reply = self_extend.add_capability("something")
        self.assertIn("won't install", reply.lower())
        self.assertIsNone(self_extend._staged)


class TestConfirmGate(SelfExtendTestCase):
    def test_confirm_without_arming_never_restarts(self):
        """The mutation guard: confirm=true with nothing staged must be inert."""
        self_extend._reset_for_tests()
        with mock.patch.object(self_restart, "spawn_restart") as restart, \
             mock.patch.object(self_restart, "write_pending") as pending:
            reply = self_extend.add_capability("email my brother", confirm=True)
        restart.assert_not_called()
        pending.assert_not_called()
        self.assertIn("nothing", reply.lower())

    def test_confirm_installs_and_restarts_when_valid(self):
        self._arm(task="email my brother")
        with mock.patch.object(self_extend, "_validate", return_value=(True, "")), \
             mock.patch.object(self_restart, "spawn_restart", return_value=True) as restart, \
             mock.patch.object(self_restart, "write_pending") as pending:
            reply = self_extend.add_capability("confirm", confirm=True)
        restart.assert_called_once()
        # The ORIGINAL task is carried across the restart, not the word "confirm".
        pending.assert_called_once_with("email my brother")
        self.assertIn("restart", reply.lower())
        self.assertIsNone(self_extend._staged, "The stage must be cleared after use.")

    def test_validation_failure_rolls_back_and_does_not_restart(self):
        self._arm()
        staged_path = self_extend._staged["path"]
        self.assertTrue(staged_path.exists())
        with mock.patch.object(self_extend, "_validate", return_value=(False, "boom")), \
             mock.patch.object(self_restart, "spawn_restart") as restart:
            reply = self_extend.add_capability("confirm", confirm=True)
        restart.assert_not_called()
        self.assertFalse(staged_path.exists(),
                         "A tool that fails its checks must be rolled back off disk.")
        self.assertIn("didn't", reply.lower())

    def test_a_failed_restart_spawn_rolls_back(self):
        self._arm()
        staged_path = self_extend._staged["path"]
        with mock.patch.object(self_extend, "_validate", return_value=(True, "")), \
             mock.patch.object(self_restart, "spawn_restart", return_value=False):
            reply = self_extend.add_capability("confirm", confirm=True)
        self.assertFalse(staged_path.exists())
        self.assertIn("roll", reply.lower())


class TestParsing(unittest.TestCase):
    def test_extracts_name_and_description(self):
        name, desc = self_extend._parse(_CANNED)
        self.assertEqual(name, "send_email")
        self.assertEqual(desc, "Send an email to a person.")

    def test_requires_a_routing_line(self):
        no_routing = _CANNED.replace('ROUTING = "- send_email: email a person."\n', "")
        self.assertIsNone(self_extend._parse(no_routing))

    def test_rejects_a_non_identifier_name(self):
        bad = _CANNED.replace("send_email", "SendEmail")
        # CamelCase fails the snake_case rule the loader/SDK expect.
        self.assertIsNone(self_extend._parse(bad))

    def test_rejects_a_syntax_error(self):
        self.assertIsNone(self_extend._parse("def broken(:\n  pass"))


class TestGeneratedLoader(unittest.TestCase):
    """The package loader must skip a broken module, never let it sink the rest."""

    def test_collects_good_skips_broken_and_underscored(self):
        tmp = Path(mkdtemp())
        self.addCleanup(rmtree, tmp, True)
        for stem in ("good", "bad", "notool", "_private"):
            (tmp / f"{stem}.py").write_text("x = 1\n", encoding="utf-8")

        good_fn = lambda: "ok"  # noqa: E731

        def importer(fullname):
            stem = fullname.rsplit(".", 1)[1]
            if stem == "bad":
                raise ValueError("boom")
            if stem == "good":
                return types.SimpleNamespace(TOOL=good_fn, ROUTING="- good: use it.")
            if stem == "notool":
                return types.SimpleNamespace()  # no TOOL attribute
            raise AssertionError(f"unexpected import of {stem}")

        tools, block = generated._collect(tmp, "fakepkg", importer)
        self.assertEqual(tools, [good_fn], "Only the well-formed module should load.")
        self.assertIn("- good: use it.", block)
        self.assertNotIn("bad", block)

    def test_no_tools_yields_empty_routing(self):
        tmp = Path(mkdtemp())
        self.addCleanup(rmtree, tmp, True)
        tools, block = generated._collect(tmp, "fakepkg", lambda name: types.SimpleNamespace())
        self.assertEqual(tools, [])
        self.assertEqual(block, "")


class TestPendingCommand(unittest.TestCase):
    """The request that triggered a restart is carried across it, exactly once."""

    def setUp(self):
        self.tmp = Path(mkdtemp())
        self.addCleanup(rmtree, self.tmp, True)
        p = mock.patch.object(self_restart, "_PENDING_FILE", self.tmp / "pending.json")
        p.start()
        self.addCleanup(p.stop)

    def test_round_trips_and_is_consumed(self):
        self_restart.write_pending("email my brother")
        self.assertEqual(self_restart.take_pending(), "email my brother")
        # Consumed: a second read finds nothing.
        self.assertIsNone(self_restart.take_pending())

    def test_a_stale_command_is_discarded(self):
        stale = {"text": "email my brother", "at": time.time() - 10_000}
        (self.tmp / "pending.json").write_text(json.dumps(stale), encoding="utf-8")
        self.assertIsNone(self_restart.take_pending())

    def test_missing_file_is_none(self):
        self.assertIsNone(self_restart.take_pending())


def mkdtemp():
    import tempfile
    return tempfile.mkdtemp(prefix="azleem-selfext-")


def rmtree(path, ignore_errors=False):
    import shutil
    shutil.rmtree(path, ignore_errors=ignore_errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
