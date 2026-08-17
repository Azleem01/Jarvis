"""The rolling conversation history that makes follow-ups work.

`JarvisAgent` was stateless per command, so "open a site" then "now search it
for something" had nothing to resolve "it" against. History fixes that, and
brings two risks worth guarding:

* a stale antecedent — "do that again" an hour later must not repeat an action
  the user has long moved on from, hence the idle expiry;
* the fast path — commands answered by ``intents`` never touch the model, so if
  they are not recorded the headline follow-up is the one case with no context.

The agent is built with ``__new__`` here, as elsewhere in these tests, so no
real router is constructed.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import llm_agent  # noqa: E402


def _texts(contents):
    """The plain text of each turn, for readable assertions."""
    return [part.text for content in contents for part in content.parts]


def _roles(contents):
    return [content.role for content in contents]


class HistoryTestCase(unittest.TestCase):
    def setUp(self):
        self.agent = llm_agent.JarvisAgent.__new__(llm_agent.JarvisAgent)
        self.agent.router = mock.Mock()
        self.reply("ok")

    def reply(self, text):
        self.agent.router.generate_content.return_value = mock.Mock(text=text)

    def sent(self):
        """The contents of the most recent model request."""
        return self.agent.router.generate_content.call_args.kwargs["contents"]


class TestFollowUps(HistoryTestCase):
    def test_the_first_command_carries_no_history(self):
        # A command that goes to the model (not one intents fast-paths, which
        # would make no request at all — "what is on my screen" now does).
        self.agent.handle("tell me a joke")
        self.assertEqual(_texts(self.sent()), ["tell me a joke"])

    def test_the_second_command_carries_the_first_turn(self):
        self.reply("Opened the site.")
        self.agent.handle("open a site")
        self.reply("Searched it.")
        self.agent.handle("now search it for something")

        self.assertEqual(
            _texts(self.sent()),
            ["open a site", "Opened the site.", "now search it for something"],
        )
        self.assertEqual(_roles(self.sent()), ["user", "model", "user"])

    def test_a_command_never_appears_in_its_own_context(self):
        self.agent.handle("first")
        self.agent.handle("second")
        self.assertEqual(_texts(self.sent()).count("second"), 1)

    def test_history_is_capped_at_the_configured_turns(self):
        for i in range(config.HISTORY_TURNS + 3):
            self.agent.handle(f"command {i}")
        sent = self.sent()
        # HISTORY_TURNS prior turns (two entries each) plus the new command.
        self.assertEqual(len(sent), config.HISTORY_TURNS * 2 + 1)
        self.assertNotIn("command 0", _texts(sent))

    def test_forget_drops_everything(self):
        self.agent.handle("first")
        self.agent.forget()
        self.agent.handle("second")
        self.assertEqual(_texts(self.sent()), ["second"])


class TestIdleExpiry(HistoryTestCase):
    def test_history_survives_inside_the_idle_window(self):
        self.agent.handle("first")
        with mock.patch.object(
            llm_agent.time, "monotonic",
            return_value=self.agent._last_turn_at + config.HISTORY_IDLE_SECONDS - 1,
        ):
            self.agent.handle("second")
        self.assertIn("first", _texts(self.sent()))

    def test_history_is_dropped_after_the_idle_window(self):
        """'Do that again' much later must not resurrect a stale antecedent."""
        self.agent.handle("open something")
        with mock.patch.object(
            llm_agent.time, "monotonic",
            return_value=self.agent._last_turn_at + config.HISTORY_IDLE_SECONDS + 1,
        ):
            self.agent.handle("do that again")
        self.assertEqual(_texts(self.sent()), ["do that again"])

    def test_expiry_drops_the_whole_history_not_just_the_old_turns(self):
        """Half a conversation is a worse antecedent than none."""
        self.agent.handle("first")
        self.agent.handle("second")
        with mock.patch.object(
            llm_agent.time, "monotonic",
            return_value=self.agent._last_turn_at + config.HISTORY_IDLE_SECONDS + 1,
        ):
            self.agent.handle("third")
        self.assertEqual(_texts(self.sent()), ["third"])


class TestFastPathTurnsAreRecorded(HistoryTestCase):
    """The headline follow-up starts with a command the model never sees."""

    def test_a_fast_path_turn_becomes_context_for_the_next_command(self):
        with mock.patch.object(llm_agent, "_tool_by_name") as by_name:
            by_name.return_value = lambda **kw: "Opened the site."
            self.assertEqual(self.agent.handle("open youtube"), "Opened the site.")

        self.agent.handle("now search it for something")
        self.assertEqual(
            _texts(self.sent()),
            ["open youtube", "Opened the site.", "now search it for something"],
        )

    def test_a_locally_answered_turn_is_recorded_too(self):
        """'What time is it' needs no tool at all, but is still a turn."""
        reply = self.agent.handle("what time is it")
        self.agent.handle("and the date")
        self.assertEqual(_texts(self.sent())[:2], ["what time is it", reply])

    def test_a_fast_path_command_still_never_reaches_the_model(self):
        with mock.patch.object(llm_agent, "_tool_by_name") as by_name:
            by_name.return_value = lambda **kw: "Launched notepad."
            self.agent.handle("open notepad")
        self.agent.router.generate_content.assert_not_called()


class TestFailuresAreNotRecorded(HistoryTestCase):
    """An API error is not something a follow-up should refer back to."""

    def test_a_quota_failure_leaves_no_turn_behind(self):
        self.agent.router.generate_content.side_effect = (
            gemini_error := llm_agent.gemini_client.AllModelsUnavailable("spent")
        )
        self.agent.handle("open something")
        self.assertIsInstance(gemini_error, Exception)

        self.agent.router.generate_content.side_effect = None
        self.reply("ok")
        self.agent.handle("next command")
        self.assertEqual(_texts(self.sent()), ["next command"])

    def test_a_network_failure_leaves_no_turn_behind(self):
        self.agent.router.generate_content.side_effect = (
            llm_agent.gemini_client.NetworkUnavailable("offline")
        )
        self.agent.handle("open something")

        self.agent.router.generate_content.side_effect = None
        self.reply("ok")
        self.agent.handle("next command")
        self.assertEqual(_texts(self.sent()), ["next command"])

    def test_an_empty_command_records_nothing(self):
        self.agent.handle("   ")
        self.agent.handle("real command")
        self.assertEqual(_texts(self.sent()), ["real command"])


class TestHistoryIsOptional(unittest.TestCase):
    def test_zero_turns_disables_history_entirely(self):
        agent = llm_agent.JarvisAgent.__new__(llm_agent.JarvisAgent)
        agent.router = mock.Mock()
        agent.router.generate_content.return_value = mock.Mock(text="ok")
        with mock.patch.object(config, "HISTORY_TURNS", 0):
            agent.handle("first")
            agent.handle("second")
        contents = agent.router.generate_content.call_args.kwargs["contents"]
        self.assertEqual(_texts(contents), ["second"])


class TestPersistenceAcrossRestart(unittest.TestCase):
    """History is written to disk so it survives Azleem restarting itself."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        self.path = path
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

    def _agent(self):
        agent = llm_agent.JarvisAgent.__new__(llm_agent.JarvisAgent)
        agent.router = mock.Mock()
        agent.router.generate_content.return_value = mock.Mock(text="ok")
        agent._history = deque(maxlen=max(1, config.HISTORY_TURNS) * 2)
        agent._last_turn_at = 0.0
        agent._history_file = self.path
        return agent

    def test_a_turn_is_saved_and_reloads_into_a_new_agent(self):
        a = self._agent()
        a.handle("tell me a joke")  # goes to the (mocked) model, then persists
        b = self._agent()
        b._load_history()
        self.assertIn("tell me a joke", _texts(list(b._hist)))

    def test_stale_history_is_dropped_on_load(self):
        a = self._agent()
        a.handle("tell me a joke")
        with open(self.path, encoding="utf-8") as fh:
            data = json.load(fh)
        data["saved_at"] = time.time() - config.HISTORY_IDLE_SECONDS - 10
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        b = self._agent()
        b._load_history()
        self.assertEqual(list(b._hist), [])

    def test_a_new_agent_with_no_file_starts_empty(self):
        b = self._agent()
        b._load_history()  # file does not exist yet
        self.assertEqual(list(b._hist), [])


class TestSystemPromptGuardsAgainstReplay(unittest.TestCase):
    def test_the_prompt_says_history_is_context_not_a_work_queue(self):
        """Without this, 'do that again' and re-reading history look alike.

        Gotcha 12 from the other direction: a model handed a transcript of
        completed actions will re-run them unless told the newest message is
        the only instruction.
        """
        prompt = llm_agent._SYSTEM_PROMPT.lower()
        self.assertIn("context, not a", prompt)
        self.assertIn("newest message", prompt)


if __name__ == "__main__":
    unittest.main()
