"""What the user actually gets to read when tools ran.

The HUD cuts replies at 220 characters (``overlay._fit_reply``). Every tool
result used to be concatenated verbatim into the reply, so a command that
called ``read_screen`` first spent the whole budget on a dump of what was on
screen and the outcome — the part the user was waiting for — fell off the end.

Observed live (logs/azleem.log, the quiz command): four tool results totalling
~900 characters, of which the visible 220 were two screen dumps.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm_agent  # noqa: E402


def hud_text(reply):
    """The reply as the HUD would actually render it."""
    import overlay

    return overlay.Overlay._fit_reply(None, reply)


# The real thing, straight from the log: read the question, search the web for
# it, read the results page, then type the answer.
QUIZ_RUN = [
    ["read_screen", "Which international organization recently announced a new "
                    "climate change action plan for 2026?"],
    ["open_url", "Opened https://www.google.com/search?q=Which+international+"
                 "organization+recently+announced+a+new+climate+change+action+"
                 "plan+for+2026%3F."],
    ["read_screen", "The Union for the Mediterranean (UfM) and the World Bank "
                    "Group both announced major updates or launches regarding "
                    "their climate action plans in mid-2026.\n\nUnion for the "
                    "Mediterranean\n• Announcement Date: July 1, 2026\n• Scope: "
                    "43 member states joining forces for the region's first "
                    "integrated climate action plan.\n• Key Focus: Linking "
                    "national climate strategies with cross-border projects."],
    ["perform_computer_task", "The answer 'Union for the Mediterranean' has "
                              "been typed into the provided field and is ready "
                              "for the user to submit."],
]


class TestSummarise(unittest.TestCase):
    def test_read_results_are_dropped_once_something_was_done(self):
        summary = llm_agent._summarise(QUIZ_RUN)
        self.assertNotIn("43 member states", summary)
        self.assertIn("Union for the Mediterranean", summary)

    def test_the_outcome_survives_hud_truncation(self):
        """The whole point: what Azleem *did* must still be on screen."""
        summary = llm_agent._summarise(QUIZ_RUN)
        self.assertIn("typed", hud_text(summary))

    def test_the_old_concatenation_would_have_failed_that(self):
        """Anchors the test above to the behaviour it is guarding against."""
        old = " ".join(r for _, r in QUIZ_RUN)
        self.assertNotIn("typed", hud_text(old))

    def test_a_lone_read_is_still_reported(self):
        """'What does my screen say?' has only a read to report."""
        summary = llm_agent._summarise([["read_screen", "Build failed: missing semicolon."]])
        self.assertIn("missing semicolon", summary)

    def test_a_screenshot_alone_is_still_reported(self):
        summary = llm_agent._summarise([["take_screenshot", "Screenshot saved to C:/shot.png."]])
        self.assertIn("shot.png", summary)

    def test_each_result_is_clipped(self):
        summary = llm_agent._summarise([["open_application", "x" * 500]])
        self.assertLessEqual(len(summary), llm_agent._PER_RESULT_CHARS)

    def test_the_whole_summary_fits_the_hud(self):
        many = [[f"open_application{i}", f"Launched application number {i}. " * 5]
                for i in range(8)]
        summary = llm_agent._summarise(many)
        self.assertLessEqual(len(summary), llm_agent._SUMMARY_CHARS)
        self.assertEqual(
            summary, hud_text(summary),
            "A summary within budget must reach the HUD unaltered.",
        )

    def test_newlines_are_collapsed(self):
        summary = llm_agent._summarise([["write_note", "line one\n\nline two"]])
        self.assertNotIn("\n", summary)

    def test_no_tools_no_summary(self):
        self.assertEqual(llm_agent._summarise([]), "")

    def test_full_detail_still_reaches_the_log(self):
        """Trimming is for the HUD only — the log keeps everything."""
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            llm_agent._summarise(QUIZ_RUN)
        self.assertIn("43 member states", buf.getvalue())


class TestWithActions(unittest.TestCase):
    """API failures must not hide work that already completed."""

    def test_failure_message_alone_when_nothing_ran(self):
        self.assertEqual(
            llm_agent._with_actions("Gemini hit a rate limit.", []),
            "Gemini hit a rate limit.",
        )

    def test_outcome_leads_and_the_failure_is_a_footnote(self):
        reply = llm_agent._with_actions("Gemini hit a rate limit.", QUIZ_RUN)
        self.assertTrue(
            reply.startswith("Opened"),
            f"The outcome should lead, got: {reply!r}",
        )
        self.assertIn("rate limit", reply)

    def test_the_reply_stays_readable_on_the_hud(self):
        reply = llm_agent._with_actions("Gemini hit a rate limit.", QUIZ_RUN)
        self.assertIn("typed", hud_text(reply))


if __name__ == "__main__":
    unittest.main(verbosity=2)
