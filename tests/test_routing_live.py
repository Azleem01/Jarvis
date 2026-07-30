"""Ask the real model what it would do — without letting it do anything.

This is the acceptance test for the MKBHD bug. The offline suite proves the
prompts are clean; only a live call proves the model now routes
"open youtube on my chrome browser" to open_url with the plain home page.

Every tool is replaced by a non-executing twin that keeps the same name,
docstring and type hints (which is all the SDK reads to build the schema) and
simply records how it was called. Nothing is launched, clicked or typed.

    set AZLEEM_LIVE_TESTS=1
    python -m unittest tests.test_routing_live -v

Skipped by default: it spends API quota. The free tier allows ~5 requests per
minute per model, so the cases are paced by AZLEEM_LIVE_PAUSE (default 13s).
"""

import functools
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ENABLED = os.environ.get("AZLEEM_LIVE_TESTS", "").strip() in ("1", "true", "yes")
_PAUSE = float(os.environ.get("AZLEEM_LIVE_PAUSE", "13"))


def _stub(fn, log):
    """A tool twin that records its arguments instead of doing the work."""

    @functools.wraps(fn)          # keeps __name__/__doc__/__annotations__
    def wrapper(*args, **kwargs):
        log.append((fn.__name__, args, kwargs))
        return f"{fn.__name__} completed successfully."

    return wrapper


@unittest.skipUnless(_ENABLED, "set AZLEEM_LIVE_TESTS=1 to run (uses API quota)")
class TestLiveRouting(unittest.TestCase):
    """What tool does the model actually pick, and with what arguments?"""

    @classmethod
    def setUpClass(cls):
        import config
        config.validate()
        cls._first = True

    def route(self, command):
        """Send one command through the real agent with inert tools."""
        import datetime
        from google.genai import types
        import config
        import gemini_client
        import llm_agent

        if not self._first:
            time.sleep(_PAUSE)          # stay under the free-tier rate limit
        type(self)._first = False

        calls = []
        now = datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M")
        gen_config = types.GenerateContentConfig(
            system_instruction=(
                f"{llm_agent._SYSTEM_PROMPT}\nCurrent date and time: {now}."
            ),
            tools=[_stub(fn, calls) for fn in llm_agent._TOOLS],
            temperature=0.2,
        )
        router = gemini_client.get_router()
        router.generate_content(contents=command, gen_config=gen_config)
        print(f"\n  {command!r}\n    -> {calls}")
        return calls

    def assertToolUsed(self, calls, name):
        used = [c[0] for c in calls]
        self.assertIn(name, used, f"expected {name}, model used {used or 'no tool'}")
        return next(c for c in calls if c[0] == name)

    def kwargs_of(self, call):
        """Tool args as a dict, whether the SDK passed them positionally."""
        import inspect
        import llm_agent
        name, args, kwargs = call
        fn = next(f for f in llm_agent._TOOLS if f.__name__ == name)
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
        return dict(bound.arguments)

    # -- the original bug report --------------------------------------------
    def test_open_youtube_in_chrome_goes_to_the_home_page(self):
        calls = self.route("open youtube on my chrome browser")

        used = [c[0] for c in calls]
        self.assertNotIn(
            "perform_computer_task", used,
            "Opening a website must not fall into the multi-step vision loop.",
        )
        call = self.assertToolUsed(calls, "open_url")
        kw = self.kwargs_of(call)

        url = str(kw.get("url", "")).lower()
        self.assertIn("youtube", url)

        # The actual regression: no channel, no video, no invented search.
        self.assertFalse(
            str(kw.get("search_query", "")).strip(),
            f"Model invented a search query: {kw.get('search_query')!r}. "
            f"'open youtube' means the home page.",
        )
        for marker in ("@", "/c/", "/channel/", "/watch", "results?"):
            self.assertNotIn(
                marker, url,
                f"URL {url!r} points at a specific channel/video rather than "
                f"the YouTube home page.",
            )

    def test_only_one_tool_call_for_a_simple_site_open(self):
        calls = self.route("open youtube in chrome")
        self.assertEqual(
            len(calls), 1,
            f"Expected a single fast call; got {[c[0] for c in calls]}.",
        )

    # -- neighbouring routes stay correct ------------------------------------
    def test_site_search_uses_search_query(self):
        calls = self.route("search youtube for lofi hip hop")
        call = self.assertToolUsed(calls, "open_url")
        kw = self.kwargs_of(call)
        self.assertIn("lofi", str(kw.get("search_query", "")).lower())

    def test_local_app_still_uses_open_application(self):
        calls = self.route("open notepad")
        self.assertToolUsed(calls, "open_application")

    def test_mangled_app_name_is_understood(self):
        calls = self.route("open Nutspad")
        call = self.assertToolUsed(calls, "open_application")
        self.assertIn("notepad", str(self.kwargs_of(call)).lower())

    def test_screenshot_routes_directly(self):
        calls = self.route("take a screenshot")
        self.assertToolUsed(calls, "take_screenshot")

    # -- the quiz bug --------------------------------------------------------
    def test_screen_question_is_not_looked_up_on_the_web(self):
        """Live acceptance for the logged failure.

        Asked to answer a question on screen and move on, the model searched
        Google for the question — which navigated away from the quiz, so every
        later step acted on the wrong window — and then dropped the second
        clause entirely.
        """
        calls = self.route(
            "solve the quiz question on my screen and move to the next one"
        )
        used = [c[0] for c in calls]

        self.assertNotIn(
            "open_url", used,
            "Searching the web for an on-screen question navigates away from "
            f"the screen the task is about. Model used {used}.",
        )
        call = self.assertToolUsed(calls, "perform_computer_task")

        task = str(self.kwargs_of(call).get("task", "")).lower()
        self.assertTrue(
            any(word in task for word in ("next", "advance", "following")),
            f"The second clause was dropped on the way into the task string: "
            f"{task!r}",
        )

    def test_a_quiz_uses_the_dedicated_tool(self):
        """The generic loop answered 3 of 16 and got 1 right; quizzes moved out."""
        calls = self.route("answer the quiz on my screen")
        used = [c[0] for c in calls]
        self.assertIn(
            "answer_quiz", used,
            f"A quiz must go to answer_quiz, not the step-by-step loop. "
            f"Model used {used}.",
        )
        self.assertNotIn("perform_computer_task", used)

    # -- machine control -----------------------------------------------------
    # Six tools were added at once, taking the registry from 13 to 19. A wider
    # decision space is the main risk in that change, and these are what would
    # catch the router drifting — particularly onto perform_computer_task,
    # which can do all of this the slow way and looks plausible to a model.

    def test_volume_uses_the_dedicated_tool(self):
        calls = self.route("turn the volume down a bit")
        self.assertToolUsed(calls, "control_volume")
        self.assertNotIn("perform_computer_task", [c[0] for c in calls])

    def test_battery_does_not_open_settings(self):
        """A vision loop through the settings app is the wrong answer here."""
        calls = self.route("how much battery have I got left")
        used = [c[0] for c in calls]
        self.assertIn("system_status", used, f"Model used {used}.")
        for slow in ("perform_computer_task", "open_application", "open_url"):
            self.assertNotIn(slow, used)

    def test_switching_window_does_not_relaunch_the_app(self):
        """'Switch to' means an app already running; open_application would
        start a second copy instead of bringing the first forward."""
        calls = self.route("switch to the file explorer window")
        used = [c[0] for c in calls]
        self.assertIn("focus_window", used, f"Model used {used}.")
        self.assertNotIn("open_application", used)

    def test_snapping_this_window_targets_the_foreground(self):
        calls = self.route("put this window on the left half of the screen")
        call = self.assertToolUsed(calls, "arrange_window")
        action = str(self.kwargs_of(call).get("action", "")).lower()
        self.assertIn("left", action)

    def test_shutdown_is_armed_and_never_confirmed_by_the_model(self):
        """The confirmation exists to make a mishearing survivable. A model
        that passes confirm=true on a first request defeats it entirely."""
        calls = self.route("shut down the computer")
        call = self.assertToolUsed(calls, "power_action")
        kwargs = self.kwargs_of(call)
        self.assertNotEqual(
            kwargs.get("confirm"), True,
            "The model confirmed a power action on the user's behalf — the "
            "two-utterance guard is bypassed and a misheard 'shut down' acts.",
        )

    def test_locking_the_screen_is_not_a_screen_loop(self):
        calls = self.route("lock my screen")
        call = self.assertToolUsed(calls, "power_action")
        self.assertIn("lock", str(self.kwargs_of(call).get("action", "")).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
