"""The quiz loop, driven by scripted model replies and a fake mouse.

Nothing here touches the network or the desktop. The vision call and pyautogui
are both replaced, so the loop's control flow — answer, point, click, verify,
advance — is exercised exactly as it runs live.

The behaviours pinned here are the ones that failed against a real site: only 3
of 16 questions answered, 1 correct, ending in "encountered a loop".
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

import cancellation  # noqa: E402
from tools import quiz  # noqa: E402


def answer(letter="B", text="India", confidence="high", **extra):
    payload = {"question": "Where is the ISA headquartered?",
               "options": {"A": "France", "B": "India", "C": "Kenya"},
               "answer_letter": letter, "answer_text": text,
               "confidence": confidence, "needs_current_info": False,
               "kind": "single_choice", "advance_label": "Next",
               "advance_is_final": False, "finished": False}
    payload.update(extra)
    return json.dumps(payload)


def question(first=False, **extra):
    """The replies one question consumes.

    Only the first question pays for an advance-control lookup; after that the
    box is cached and re-used, which is the whole point of the optimisation.
    """
    replies = [answer(**extra), box()]
    if first:
        replies.append(box(is_final_submit=False))
    return replies


def box(found=True, b=(300, 100, 360, 700), **extra):
    payload = {"found": found, "box": list(b)}
    payload.update(extra)
    return json.dumps(payload)


FINISHED = json.dumps({"finished": True})


class QuizHarness:
    """Feeds scripted replies to the loop and records what it clicked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.clicks = []
        self.frame = 0
        self.waits = []           # every screen.wait_for_change call
        self.wait_results = None  # optional scripted [True, False, ...]
        self.direct_captures = 0  # capture_screen calls NOT made by a settle

    # -- the vision call --------------------------------------------------
    def generate(self, contents, gen_config=None, needs_pointing=False):
        prompt = next((c for c in contents if isinstance(c, str)), "")
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else FINISHED
        return mock.Mock(text=text)

    # -- the screen -------------------------------------------------------
    def capture(self, _internal=False):
        """A fresh frame each call, so 'did the screen change' is always true.

        Tests that need a *stuck* screen override this.
        """
        if not _internal:
            self.direct_captures += 1
        self.frame += 1
        img = Image.new("RGB", (1280, 720), (self.frame * 7 % 255, 30, 30))
        return img, b"jpeg"

    def wait(self, before, timeout=None, region=None, threshold=None, poll=None):
        """Stand-in for screen.wait_for_change — the settle *and* the verify.

        Records the region so tests can assert a click was checked against the
        element it landed on, not the whole screen.
        """
        self.waits.append({"region": region, "threshold": threshold})
        changed = True
        if self.wait_results:
            changed = self.wait_results.pop(0)
        image, jpeg = self.capture(_internal=True)
        return changed, image, jpeg

    def install(self, stack, differ=True):
        stack.enter_context(mock.patch.object(quiz.providers, "vision_generate",
                                              side_effect=self.generate))
        stack.enter_context(mock.patch.object(quiz.screen, "capture_screen",
                                              side_effect=self.capture))
        stack.enter_context(mock.patch.object(quiz.screen, "wait_for_change",
                                              side_effect=self.wait))
        stack.enter_context(mock.patch.object(quiz.screen, "screens_differ",
                                              return_value=differ))
        fake_mouse = mock.Mock()
        fake_mouse.click.side_effect = lambda *a, **k: self.clicks.append("click")
        stack.enter_context(mock.patch.object(quiz, "pyautogui", fake_mouse))
        return self


def run(replies, scope="", differ=True):
    import contextlib

    cancellation.reset()
    harness = QuizHarness(replies)
    with contextlib.ExitStack() as stack:
        harness.install(stack, differ=differ)
        result = quiz.answer_quiz(scope)
    return result, harness


class TestAnsweringLoop(unittest.TestCase):
    def test_answers_several_questions_in_one_run(self):
        replies = question(first=True) + question() + question() + [FINISHED]
        result, h = run(replies)
        self.assertIn("Answered 3 questions", result)
        # Three answers plus three Next clicks.
        self.assertEqual(len(h.clicks), 6)

    def test_the_advance_control_is_located_once_not_per_question(self):
        """The optimisation, pinned.

        The Next button does not move between questions, so re-locating it every
        time spent a third of the run's model calls — 16 of 48 on a
        16-question quiz — for an answer already known. Measured before this
        change: 3.00 model calls per question. After: 2.00.
        """
        replies = question(first=True) + [r for _ in range(7) for r in question()]
        replies.append(FINISHED)
        _, h = run(replies)

        advance_lookups = [p for p in h.prompts if "is_final_submit" in p]
        self.assertEqual(
            len(advance_lookups), 1,
            f"The advance control should be located once and cached; it was "
            f"looked up {len(advance_lookups)} times.",
        )
        answers = [p for p in h.prompts if "answer_letter" in p]
        self.assertEqual(len(h.prompts) / len(answers), 2.0,
                         "should settle at 2 model calls per question")

    def test_the_next_question_frame_is_not_photographed_twice(self):
        """Advancing already waited for the new question to paint.

        Capturing again at the top of the loop shoots the same pixels a second
        time — 15 wasted screenshots on a 16-question quiz.
        """
        replies = question(first=True) + [r for _ in range(3) for r in question()]
        replies.append(FINISHED)
        _, h = run(replies)
        # 4 questions: one capture for the first frame, then the loop runs on
        # frames handed back by wait_for_change.
        self.assertLessEqual(
            h.direct_captures, 2,
            f"{h.direct_captures} direct captures; the loop should reuse the "
            f"frame the advance settle returned.",
        )

    def test_the_answer_call_never_asks_for_a_box(self):
        """The measured regression: bundling answer+box halved click accuracy.

        One call producing question, options, answer and coordinates landed on
        the right option 2 times in 5. Asked separately: 3 of 3.
        """
        _, h = run([answer(), box(), box(), FINISHED])
        answer_prompts = [p for p in h.prompts if "answer_letter" in p]
        self.assertTrue(answer_prompts)
        for prompt in answer_prompts:
            self.assertIn("Do NOT include bounding boxes", prompt)

    def test_the_option_is_located_by_its_verbatim_text(self):
        """Anchoring to text is what makes this work on an unseen layout."""
        _, h = run([answer(text="Canberra"), box(), box(), FINISHED])
        pointing = [p for p in h.prompts if "ELEMENT:" in p]
        self.assertTrue(any('"Canberra"' in p for p in pointing), pointing)

    def test_stops_when_the_quiz_reports_finished(self):
        result, _ = run([FINISHED])
        self.assertIn("didn't answer anything", result)


class TestSubmitGuard(unittest.TestCase):
    def test_a_final_submit_is_never_clicked(self):
        result, h = run([answer(), box(),
                         box(is_final_submit=True, label="Submit Quiz"),
                         FINISHED])
        self.assertIn("yours to click", result)
        self.assertEqual(len(h.clicks), 1, "only the answer should be clicked")

    def test_a_plain_next_is_clicked(self):
        replies = (question(first=True)
                   + [answer(advance_is_final=True), box()] + [FINISHED])
        result, h = run(replies)
        self.assertEqual(len(h.clicks), 3)   # answer, next, answer
        self.assertIn("Answered 2 questions", result)

    def test_a_cached_next_still_notices_it_became_submit(self):
        """The safety hole the caching optimisation could have opened.

        With the box cached, nothing re-asks the model where the control is —
        so if that were the only signal, a Next button that turns into Submit
        on the final question would be clicked blindly. The answering call
        reports the label every question, which is what closes it.
        """
        replies = (question(first=True)
                   + [answer(advance_label="Submit Quiz", advance_is_final=True),
                      box()]
                   + [FINISHED])
        result, h = run(replies)
        self.assertIn("yours to click", result)
        # Q1's answer + Q1's Next + Q2's answer. Never the Submit.
        self.assertEqual(len(h.clicks), 3)

    def test_a_changed_advance_label_drops_the_cached_box(self):
        """'Next' becoming 'Continue' means the control may have moved too."""
        replies = (question(first=True)
                   + [answer(advance_label="Continue"), box(), box()]
                   + [FINISHED])
        _, h = run(replies)
        lookups = [p for p in h.prompts if "is_final_submit" in p]
        self.assertEqual(len(lookups), 2,
                         "a relabelled control must be located again")


class TestConfidence(unittest.TestCase):
    def test_low_confidence_questions_are_flagged(self):
        result, _ = run([answer(confidence="low"), box(), box(is_final_submit=True),
                         FINISHED])
        self.assertIn("not confident", result)
        self.assertIn("Q1", result)

    def test_current_affairs_questions_are_flagged_even_when_confident(self):
        """A model cannot know what was 'recently announced'.

        This is the exact class of question the user's quiz asks, so a
        confident-sounding answer must still be marked for review.
        """
        result, _ = run([answer(confidence="high", needs_current_info=True),
                         box(), box(is_final_submit=True), FINISHED])
        self.assertIn("not confident", result)

    def test_confident_answers_are_not_flagged(self):
        result, _ = run([answer(confidence="high"), box(),
                         box(is_final_submit=True), FINISHED])
        self.assertNotIn("not confident", result)


class TestRobustness(unittest.TestCase):
    def test_unparseable_reply_stops_cleanly(self):
        result, _ = run(["not json at all"])
        self.assertIn("couldn't read the screen", result)

    def test_multi_select_is_declined_not_guessed(self):
        result, h = run([answer(kind="multi_select")])
        self.assertIn("multi select", result)
        self.assertEqual(h.clicks, [], "nothing should be clicked")

    def test_a_missing_option_box_stops_cleanly(self):
        result, h = run([answer(text="Canberra"), box(found=False)])
        self.assertIn("couldn't find", result)
        self.assertEqual(h.clicks, [])

    def test_a_click_that_does_not_register_is_retried(self):
        """A silent no-op is what convinced the agent to change its answer."""
        import contextlib

        cancellation.reset()
        h = QuizHarness([answer(), box(), box(b=(300, 100, 360, 700)),
                         box(is_final_submit=True), FINISHED])
        h.wait_results = [False] + [True] * 20  # first verify fails
        with contextlib.ExitStack() as stack:
            h.install(stack)
            quiz.answer_quiz("")
        pointing = [p for p in h.prompts if "ELEMENT:" in p]
        self.assertGreaterEqual(len(pointing), 2,
                                "a failed click must trigger a fresh locate")

    def test_click_verification_is_scoped_to_the_option(self):
        """Checking the whole screen would accept the wrong evidence.

        A page-wide animation, a lazy-loading image or a ticking clock all
        change the screen without the click having selected anything. The
        verification must look at the option's own rectangle.
        """
        option = (300, 100, 360, 700)
        _, h = run([answer(), box(b=option), box(is_final_submit=True), FINISHED])
        scoped = [w for w in h.waits if w["region"]]
        self.assertTrue(scoped, "the click check must pass a region")
        self.assertEqual(list(scoped[0]["region"]), list(option))

    def test_esc_stops_the_run(self):
        import contextlib

        cancellation.reset()
        h = QuizHarness([answer(), box(), box(), answer(), box(), box(), FINISHED])
        with contextlib.ExitStack() as stack:
            h.install(stack)
            cancellation.request_cancel()
            result = quiz.answer_quiz("")
        cancellation.reset()
        self.assertIn("Esc", result)
        self.assertEqual(h.clicks, [])

    def test_scope_can_limit_it_to_one_question(self):
        result, h = run([answer(), box(), FINISHED], scope="just this one")
        self.assertIn("Answered 1 question", result)
        self.assertEqual(len(h.clicks), 1, "must not advance when asked for one")

    def test_question_cap_is_honoured(self):
        replies = question(first=True) + [r for _ in range(60) for r in question()]
        with mock.patch.object(quiz.config, "QUIZ_MAX_QUESTIONS", 4):
            result, h = run(replies)
        self.assertIn("Answered 4 questions", result)
        self.assertIn("limit", result)


class TestOptionLabelStripping(unittest.TestCase):
    """Found by the live run: the model returns "C. Canberra", not "Canberra".

    answer_text is handed to the locator as the exact string to find on screen,
    so a leading option label makes the match worse — and on a page where the
    letters are drawn separately from the text, it can fail to match at all.
    """

    def test_letter_prefixes_are_removed(self):
        for raw in ("C. Canberra", "C) Canberra", "(C) Canberra", "C - Canberra"):
            self.assertEqual(quiz._strip_option_label(raw), "Canberra", raw)

    def test_number_prefixes_are_removed(self):
        self.assertEqual(quiz._strip_option_label("3) Saturn"), "Saturn")
        self.assertEqual(quiz._strip_option_label("12. Saturn"), "Saturn")

    def test_plain_text_is_untouched(self):
        self.assertEqual(quiz._strip_option_label("Canberra"), "Canberra")

    def test_a_real_answer_is_never_emptied(self):
        """A one-letter answer must survive; the locator needs something."""
        for raw in ("A", "B.", "42"):
            self.assertTrue(quiz._strip_option_label(raw))

    def test_sentences_keep_their_words(self):
        """'True' and prose answers must not lose a leading word."""
        self.assertEqual(quiz._strip_option_label("True"), "True")
        self.assertEqual(
            quiz._strip_option_label("Water is a universal solvent"),
            "Water is a universal solvent")

    def test_the_loop_locates_using_the_stripped_text(self):
        _, h = run([answer(text="C. Canberra"), box(), box(is_final_submit=True),
                    FINISHED])
        pointing = [p for p in h.prompts if "ELEMENT:" in p]
        self.assertTrue(any('"Canberra"' in p for p in pointing), pointing)
        self.assertFalse(any('"C. Canberra"' in p for p in pointing))


class TestReport(unittest.TestCase):
    def test_nothing_answered_reads_honestly(self):
        self.assertIn("didn't answer", quiz._report([], [], "the quiz is finished"))

    def test_singular_plural(self):
        self.assertIn("1 question.", quiz._report([("q", "a", "high")], [], ""))
        self.assertIn("2 questions", quiz._report([("q", "a", "high")] * 2, [], ""))

    def test_many_uncertain_questions_are_summarised(self):
        text = quiz._report([("q", "a", "low")] * 9, list(range(1, 10)), "")
        self.assertIn("and 4 more", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
