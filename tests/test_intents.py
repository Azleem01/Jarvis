"""The local fast path, and — mostly — what it must refuse to touch.

Matching a command locally removes a 1-3 s model round trip and one request
from a 20/day quota. The danger is obvious: a wrong instant answer is worse
than a right slow one, because the user never gets the chance to be routed
properly.

So the majority of these tests assert that ambiguous commands return None and
fall through to Gemini. The fast path is only allowed to be certain.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import intents  # noqa: E402


class TestMatches(unittest.TestCase):
    """Commands that are genuinely unambiguous."""

    def assertRoutes(self, text, tool, **kwargs):
        got = intents.match(text)
        self.assertIsNotNone(got, f"{text!r} should fast-path")
        name, payload = got
        self.assertEqual(name, tool, f"{text!r} -> {name}")
        for key, value in kwargs.items():
            self.assertEqual(str(payload.get(key, "")).lower(), value.lower())

    def test_screenshots(self):
        for text in ("take a screenshot", "take screenshot", "screenshot",
                     "grab a screen shot", "capture the screenshot"):
            self.assertRoutes(text, "take_screenshot")

    def test_local_apps(self):
        self.assertRoutes("open notepad", "open_application", app_name="notepad")
        self.assertRoutes("launch calculator", "open_application",
                          app_name="calculator")
        self.assertRoutes("start file explorer", "open_application",
                          app_name="file explorer")

    def test_mangled_app_names_still_fast_path(self):
        """The noun is passed through; os_tools' alias table repairs it.

        What must never be guessed is the *intent*, and "open X" is not
        ambiguous however badly X was transcribed.
        """
        self.assertRoutes("open Nutspad", "open_application", app_name="nutspad")

    def test_known_sites_go_to_open_url(self):
        self.assertRoutes("open youtube", "open_url", url="youtube")
        self.assertRoutes("go to github", "open_url", url="github")

    def test_a_bare_site_does_not_pin_a_browser(self):
        """No browser named means the default one — not an empty string."""
        _, payload = intents.match("open youtube")
        self.assertNotIn("browser", payload)

    def test_a_site_with_a_named_browser_routes_to_open_url(self):
        """The reported bug: this went to open_application as an *app name*.

        `cmd /c start "" "youtube on my chrome browser"` opened nothing and
        Windows said so in a dialog, while the HUD reported "Launched".
        """
        self.assertRoutes("open youtube on my chrome browser", "open_url",
                          url="youtube", browser="chrome")
        self.assertRoutes("open youtube in chrome", "open_url",
                          url="youtube", browser="chrome")
        self.assertRoutes("open gmail in edge", "open_url",
                          url="gmail", browser="edge")
        self.assertRoutes("go to github in firefox", "open_url",
                          url="github", browser="firefox")
        self.assertRoutes("open youtube on my google chrome browser", "open_url",
                          url="youtube", browser="google chrome")

    def test_multi_word_app_names_still_fast_path(self):
        """The guard against phrases must not catch real names."""
        for text, app in (("open windows media player", "windows media player"),
                          ("open file explorer", "file explorer"),
                          ("open google chrome", "google chrome"),
                          ("open microsoft edge", "microsoft edge")):
            self.assertRoutes(text, "open_application", app_name=app)

    def test_politeness_and_filler_are_stripped(self):
        for text in ("hey azleem, open notepad please",
                     "could you open notepad", "ok so open notepad",
                     "open notepad, thanks"):
            self.assertRoutes(text, "open_application", app_name="notepad")

    def test_time_and_date_need_no_tool_at_all(self):
        for text in ("what's the time", "what time is it", "time"):
            name, reply = intents.match(text)
            self.assertIsNone(name)
            self.assertRegex(reply, r"It's \d{2}:\d{2}\.")
        name, reply = intents.match("what's the date")
        self.assertIsNone(name)
        self.assertIn("It's", reply)


class TestRefusals(unittest.TestCase):
    """Everything the fast path must hand to the model instead of guessing."""

    def assertFallsThrough(self, text):
        self.assertIsNone(
            intents.match(text),
            f"{text!r} matched locally but is not unambiguous — it must go to "
            f"the model.",
        )

    def test_a_second_clause_disqualifies_it(self):
        """The exact failure mode: half a command executed instantly."""
        for text in ("open notepad and write my shopping list",
                     "open youtube then search for something",
                     "take a screenshot and send it to sam"):
            self.assertFallsThrough(text)

    def test_anything_involving_the_screen(self):
        for text in ("solve the quiz question on my screen",
                     "answer the quiz on my screen and move to the next one",
                     "read my screen", "what's on my screen"):
            self.assertFallsThrough(text)

    def test_site_searches_are_not_plain_opens(self):
        for text in ("open youtube and play music", "search youtube for jazz",
                     "open google and search for the weather"):
            self.assertFallsThrough(text)

    def test_tools_with_real_arguments_are_never_fast_pathed(self):
        for text in ("set an alarm for 7am", "message sam on whatsapp",
                     "add lunch to my calendar tomorrow at noon",
                     "write a note about the meeting",
                     "compute the first 100 primes", "find my CV"):
            self.assertFallsThrough(text)

    def test_questions_are_left_to_the_model(self):
        for text in ("what is the capital of Australia",
                     "how do I open notepad", "why is the sky blue",
                     "when is my next meeting"):
            self.assertFallsThrough(text)

    def test_empty_and_junk(self):
        for text in ("", "   ", "uh", "hey", "...", None):
            self.assertFallsThrough(text)

    def test_an_open_with_no_target_is_not_a_command(self):
        for text in ("open", "open up", "launch"):
            self.assertFallsThrough(text)

    def test_an_absurdly_long_target_is_not_an_app_name(self):
        self.assertFallsThrough("open " + "x" * 60)

    def test_a_prepositional_phrase_is_not_an_app_name(self):
        """The app pattern's capture class contains a space, so it captures
        *phrases*. A preposition or pronoun inside the target means this is a
        sentence, and only the model can route a sentence.

        The length cap was the sole thing limiting the original bug: at 28
        characters "youtube on my chrome browser" fit inside {2,30} and was
        launched, while the 35-character "...my google chrome browser" fell
        through — the failure was length-sensitive, not intent-sensitive.
        """
        for text in ("open my downloads folder",
                     "open the file on my desktop",
                     "open notepad with admin rights",
                     "open the report in word",
                     "launch chrome in incognito mode"):
            self.assertFallsThrough(text)

    def test_an_unlisted_site_with_a_browser_is_left_to_the_model(self):
        """Both halves of the fast rule are closed sets. An unknown site is a
        guess, and the model has the whole alias table plus judgement."""
        for text in ("open coursera in chrome", "open my bank site in edge"):
            self.assertFallsThrough(text)


class TestMachineControl(unittest.TestCase):
    """Volume, lock, battery and the power confirmation."""

    def assertRoutes(self, text, tool, **kwargs):
        got = intents.match(text)
        self.assertIsNotNone(got, f"{text!r} should fast-path")
        name, payload = got
        self.assertEqual(name, tool, f"{text!r} -> {name}")
        for key, value in kwargs.items():
            self.assertEqual(payload.get(key), value, f"{text!r} {key}")

    def test_volume_verbs(self):
        for text in ("mute", "mute the speakers", "mute the sound"):
            self.assertRoutes(text, "control_volume", action="mute")
        for text in ("unmute", "unmute the speakers"):
            self.assertRoutes(text, "control_volume", action="unmute")
        for text in ("volume up", "turn it up", "louder",
                     "turn the volume up"):
            self.assertRoutes(text, "control_volume", action="up")
        for text in ("volume down", "turn it down", "quieter",
                     "turn the sound down"):
            self.assertRoutes(text, "control_volume", action="down")

    def test_an_explicit_volume_level(self):
        for text in ("set volume to 30", "set the volume to 30", "volume 30"):
            self.assertRoutes(text, "control_volume", action="set", level=30)
        self.assertRoutes("volume 45 percent", "control_volume",
                          action="set", level=45)

    def test_lock_and_battery(self):
        for text in ("lock", "lock the screen", "lock my computer"):
            self.assertRoutes(text, "power_action", action="lock")
        for text in ("what's my battery", "how is my battery",
                     "battery level", "battery"):
            self.assertRoutes(text, "system_status")

    def test_minimise_everything(self):
        for text in ("minimise everything", "minimize all windows",
                     "show the desktop"):
            self.assertRoutes(text, "arrange_window", action="minimise_all")

    def test_confirming_a_power_action(self):
        """Safe to match locally only because the tool refuses unless it armed
        the same action moments ago — see test_system.TestPowerConfirmation."""
        self.assertRoutes("confirm shutdown", "power_action",
                          action="shutdown", confirm=True)
        self.assertRoutes("confirm sleep", "power_action",
                          action="sleep", confirm=True)

    def test_confirm_shut_down_survives_the_two_word_spelling(self):
        """'shut down' must not arrive at the tool as 'shut'."""
        _, payload = intents.match("confirm shut down")
        self.assertEqual(payload["action"], "shut down")


class TestMachineControlRefusals(unittest.TestCase):
    """The half of the fast path that matters more than the matching half."""

    def assertFallsThrough(self, text):
        self.assertIsNone(
            intents.match(text),
            f"{text!r} matched locally but is not unambiguous — it must go to "
            f"the model.",
        )

    def test_a_bare_power_word_never_fast_paths(self):
        """The arming reply is composed by the model; more importantly, a
        misheard 'shut down' must never reach a tool without a round trip."""
        for text in ("shutdown", "shut down", "sleep", "restart", "reboot",
                     "power off", "turn it off"):
            self.assertFallsThrough(text)

    def test_confirming_something_that_is_not_a_power_action(self):
        for text in ("confirm", "confirm delete", "confirm the order",
                     "confirm my booking"):
            self.assertFallsThrough(text)

    def test_an_open_capture_is_left_to_the_model(self):
        """gotcha 21: 'switch to X' and 'close X' capture phrases, not names.

        These are exactly the shape that launched 'youtube on my chrome
        browser' as an application, so they are deliberately not fast-pathed
        however obvious they look.
        """
        for text in ("switch to chrome", "switch to my editor",
                     "close this window", "close the browser",
                     "minimise this window", "put this on the left"):
            self.assertFallsThrough(text)

    def test_a_volume_level_outside_the_range_is_a_mishearing(self):
        for text in ("set volume to 300", "volume 900", "volume 1000"):
            self.assertFallsThrough(text)

    def test_muting_something_that_is_not_the_speakers(self):
        """The mic and a playing video are different targets entirely."""
        for text in ("mute my mic", "mute the microphone", "mute the video",
                     "mute the tab"):
            self.assertFallsThrough(text)

    def test_locking_something_that_is_not_the_screen(self):
        for text in ("lock the door", "lock my phone", "lock the file"):
            self.assertFallsThrough(text)

    def test_brightness_is_left_to_the_model(self):
        """Not fast-pathed: 'turn down the brightness' shares a verb with the
        volume rules, and the model can tell them apart."""
        for text in ("turn down the brightness", "set brightness to 50",
                     "dim the screen"):
            self.assertFallsThrough(text)

    def test_a_second_clause_still_disqualifies_a_machine_command(self):
        for text in ("mute and open notepad", "turn it up and play something",
                     "lock the screen and go to sleep"):
            self.assertFallsThrough(text)

    def test_a_question_about_battery_with_more_in_it(self):
        for text in ("what is my battery and how much storage do I have",
                     "is my battery bad"):
            self.assertFallsThrough(text)


class TestAgentIntegration(unittest.TestCase):
    """The wiring in JarvisAgent, without constructing a real router."""

    def setUp(self):
        import llm_agent

        self.agent = llm_agent.JarvisAgent.__new__(llm_agent.JarvisAgent)

    def test_a_fast_path_command_never_reaches_the_model(self):
        from unittest import mock

        import llm_agent

        with mock.patch.object(llm_agent, "_tool_by_name") as by_name:
            by_name.return_value = lambda **kw: "Launched notepad."
            reply = self.agent.handle("open notepad")
        self.assertEqual(reply, "Launched notepad.")
        by_name.assert_called_once_with("open_application")

    def test_a_normal_command_still_routes_to_the_model(self):
        from unittest import mock

        import llm_agent

        self.agent.router = mock.Mock()
        self.agent.router.generate_content.return_value = mock.Mock(text="did it")
        reply = self.agent.handle("solve the quiz question on my screen")
        self.assertEqual(reply, "did it")
        self.agent.router.generate_content.assert_called_once()

    def test_a_broken_pattern_falls_back_instead_of_failing(self):
        """A fast-path bug must degrade to slow, never to broken."""
        from unittest import mock

        import llm_agent

        self.agent.router = mock.Mock()
        self.agent.router.generate_content.return_value = mock.Mock(text="ok")
        with mock.patch.object(llm_agent.intents, "match",
                               side_effect=RuntimeError("bad regex")):
            self.assertEqual(self.agent.handle("open notepad"), "ok")

    def test_a_failing_tool_falls_back_to_the_model(self):
        from unittest import mock

        import llm_agent

        self.agent.router = mock.Mock()
        self.agent.router.generate_content.return_value = mock.Mock(text="recovered")
        with mock.patch.object(llm_agent, "_tool_by_name") as by_name:
            by_name.return_value = mock.Mock(side_effect=OSError("no such app"))
            self.assertEqual(self.agent.handle("open notepad"), "recovered")


if __name__ == "__main__":
    unittest.main(verbosity=2)
