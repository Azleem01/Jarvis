"""Does the quiz stack actually work? Ask the real models.

The offline suite proves the loop's control flow. Only a live call proves the
models can read a quiz page, know the answer, and point at the right option.

Quiz pages are generated here at known coordinates, so click accuracy is
*measured* against ground truth rather than eyeballed. Five layouts, because
the point is that it works on a site nobody has seen — a single layout would
prove only that it works on that layout.

    set AZLEEM_LIVE_TESTS=1
    python -m unittest tests.test_quiz_live -v

Skipped by default: it spends API quota. OpenRouter's free tier is ~50
requests/day, and this suite uses about a dozen.
"""

import io
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ENABLED = os.environ.get("AZLEEM_LIVE_TESTS", "").strip() in ("1", "true", "yes")
_PAUSE = float(os.environ.get("AZLEEM_LIVE_PAUSE", "4"))

W, H = 1280, 720


def _font(size, bold=False):
    from PIL import ImageFont

    try:
        return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


# Each builder draws a page and returns {option_text: (ymin, xmin, ymax, xmax)}
# — the option's real clickable rectangle in the normalised 0-1000 space.
#
# Rectangles, not centres, because "would this click select the right answer?"
# is the only question that matters. An earlier version compared against a
# presumed centre and reported the numbered-row layout as off by 344/1000: the
# model had boxed the option's *text* while the test assumed the middle of a
# full-width row. The click would have landed correctly; the assertion was
# wrong. Containment cannot make that mistake.
def radio_list(d, question, options):
    F, FB = _font(24), _font(29, True)
    d.rectangle([0, 0, W, 70], fill="#1a3a6b")
    d.text((60, 140), question, font=FB, fill="#111")
    truth = {}
    for i, (letter, text) in enumerate(zip("ABCD", options)):
        y = 240 + i * 72
        d.rounded_rectangle([110, y, 900, y + 52], radius=8, outline="#c3c7d1",
                            width=2, fill="white")
        d.ellipse([128, y + 15, 150, y + 37], outline="#7a7f8a", width=2)
        d.text((170, y + 14), f"{letter}.  {text}", font=F, fill="#111")
        truth[text] = (y / H * 1000, 110 / W * 1000,
                       (y + 52) / H * 1000, 900 / W * 1000)
    d.rounded_rectangle([1050, 620, 1230, 675], radius=8, fill="#1a73e8")
    d.text((1108, 637), "Next", font=F, fill="white")
    return truth


def card_grid(d, question, options):
    F, FB = _font(24), _font(29, True)
    d.rectangle([0, 0, W, 70], fill="#2d2d44")
    d.text((60, 130), question, font=FB, fill="#111")
    truth = {}
    for i, (letter, text) in enumerate(zip("ABCD", options)):
        cx, cy = 140 + (i % 2) * 530, 230 + (i // 2) * 190
        d.rounded_rectangle([cx, cy, cx + 470, cy + 150], radius=14,
                            outline="#9aa0ac", width=3, fill="#fbfbfd")
        d.text((cx + 28, cy + 22), letter, font=FB, fill="#1a73e8")
        d.text((cx + 28, cy + 80), text, font=F, fill="#111")
        truth[text] = (cy / H * 1000, cx / W * 1000,
                       (cy + 150) / H * 1000, (cx + 470) / W * 1000)
    d.rounded_rectangle([1040, 650, 1230, 700], radius=8, fill="#34a853")
    return truth


def dark_row(d, question, options):
    FB, FS = _font(29, True), _font(19)
    d.rectangle([0, 0, W, H], fill="#12141a")
    d.text((60, 150), question, font=FB, fill="#f1f3f7")
    truth = {}
    for i, (letter, text) in enumerate(zip("ABCD", options)):
        x, y = 70 + i * 295, 330
        d.rounded_rectangle([x, y, x + 265, y + 120], radius=12, fill="#1e2230",
                            outline="#39405a", width=2)
        d.text((x + 18, y + 16), letter, font=FB, fill="#7aa2ff")
        d.text((x + 18, y + 66), text, font=FS, fill="#e6e9f2")
        truth[text] = (y / H * 1000, x / W * 1000,
                       (y + 120) / H * 1000, (x + 265) / W * 1000)
    d.rounded_rectangle([1050, 620, 1230, 675], radius=8, fill="#7aa2ff")
    return truth


def true_false(d, question, options):
    F, FB = _font(24), _font(29, True)
    d.rectangle([0, 0, W, 70], fill="#7b1fa2")
    d.text((60, 160), question, font=FB, fill="#111")
    truth = {}
    for i, text in enumerate(options[:2]):
        y = 300 + i * 110
        d.rounded_rectangle([200, y, 1080, y + 80], radius=10, outline="#b39ddb",
                            width=3, fill="white")
        d.text((250, y + 26), text, font=F, fill="#111")
        truth[text] = (y / H * 1000, 200 / W * 1000,
                       (y + 80) / H * 1000, 1080 / W * 1000)
    d.rounded_rectangle([1050, 620, 1230, 675], radius=8, fill="#7b1fa2")
    return truth


def numbered_rows(d, question, options):
    F, FB, FS = _font(24), _font(29, True), _font(19)
    d.text((50, 90), "Question 7 of 16", font=FS, fill="#888")
    d.text((50, 140), question, font=FB, fill="#111")
    truth = {}
    for i, text in enumerate(options):
        y = 230 + i * 58
        d.text((80, y), f"{i + 1})", font=F, fill="#1a73e8")
        d.text((140, y), text, font=F, fill="#111")
        d.line([70, y + 42, 1100, y + 42], fill="#eee")
        truth[text] = ((y - 8) / H * 1000, 70 / W * 1000,
                       (y + 42) / H * 1000, 1100 / W * 1000)
    d.rounded_rectangle([1050, 620, 1230, 675], radius=8, fill="#111")
    return truth


LAYOUTS = [("radio list", radio_list), ("card grid", card_grid),
           ("dark row", dark_row), ("true/false", true_false),
           ("numbered rows", numbered_rows)]

MCQ = ("What is the capital city of Australia?",
       ["Sydney", "Melbourne", "Canberra", "Perth"], "Canberra")
PLANETS = ("Which planet has the most moons in our solar system?",
           ["Jupiter", "Saturn", "Uranus", "Neptune"], "Saturn")
TF = ("Water freezes at 0 degrees Celsius at sea level.",
      ["True", "False"], "True")

def normalise(text):
    """Compare answers the way the loop consumes them.

    The model sometimes returns "C. Canberra" rather than "Canberra"; production
    strips that label before locating the option, so the test must too.
    """
    from tools import quiz

    return quiz._strip_option_label(text).strip().lower()


def render(builder, question, options):
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), "#f7f7fa")
    truth = builder(ImageDraw.Draw(img), question, options)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue(), truth


@unittest.skipUnless(_ENABLED, "set AZLEEM_LIVE_TESTS=1 to run (uses API quota)")
class TestQuizVisionLive(unittest.TestCase):
    """Answer accuracy and click accuracy, measured against known geometry."""

    @classmethod
    def setUpClass(cls):
        import config

        config.validate()
        cls._first = True

    def pace(self):
        if not self._first:
            time.sleep(_PAUSE)
        type(self)._first = False

    def answer_for(self, jpeg):
        from tools import quiz

        self.pace()
        return quiz._ask(quiz._QUIZ_PROMPT, jpeg, quiz._answer_config())

    def point_at(self, jpeg, text):
        from tools import quiz

        self.pace()
        return quiz._locate(f'the answer option whose text is exactly "{text}"', jpeg)

    def test_answers_are_correct_on_every_layout(self):
        """The knowledge half. A layout it has never seen must not change this."""
        wrong = []
        for name, builder in LAYOUTS:
            question, options, correct = (
                TF if name == "true/false"
                else MCQ if name in ("radio list", "card grid") else PLANETS)
            jpeg, _ = render(builder, question, options)
            data = self.answer_for(jpeg)
            got = (data or {}).get("answer_text", "")
            print(f"\n  {name:<14} {question[:40]:<42} -> {got!r}")
            if normalise(got) != correct.lower():
                wrong.append(f"{name}: got {got!r}, wanted {correct!r}")
        self.assertEqual(wrong, [], f"wrong answers: {wrong}")

    def test_pointing_lands_on_the_right_option(self):
        """The clicking half, and the reason answering and pointing are split.

        Asked together with the answer, this scored 2/5 across these layouts —
        off by as much as 344/1000, i.e. selecting a different option than the
        one named. Asked on its own it lands within a few pixels.
        """
        misses = []
        for name, builder in LAYOUTS:
            question, options, correct = (
                TF if name == "true/false"
                else MCQ if name in ("radio list", "card grid") else PLANETS)
            jpeg, truth = render(builder, question, options)
            box = self.point_at(jpeg, correct)
            if not box:
                misses.append(f"{name}: no box returned")
                continue
            cy, cx = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            ymin, xmin, ymax, xmax = truth[correct]
            inside = ymin <= cy <= ymax and xmin <= cx <= xmax
            wrong = [t for t, r in truth.items()
                     if t != correct and r[0] <= cy <= r[2] and r[1] <= cx <= r[3]]
            verdict = "lands on " + correct if inside else "MISSES " + correct
            print(f"\n  {name:<14} click (y={cy:.0f}, x={cx:.0f}) {verdict}")
            if not inside:
                misses.append(f"{name}: ({cy:.0f},{cx:.0f}) outside {correct!r}")
            if wrong:
                misses.append(f"{name}: would select {wrong} instead")
        self.assertEqual(misses, [], f"clicks would miss: {misses}")

    def test_a_current_affairs_question_is_flagged(self):
        """The honest half: recency is past every model's training cutoff.

        The user's quiz asks what was 'recently announced'. Azleem must mark
        that for review rather than present a guess as fact.
        """
        jpeg, _ = render(
            radio_list,
            "Which organization recently announced a new climate plan for 2026?",
            ["World Bank", "UNFCCC", "Union for the Mediterranean", "G20"])
        data = self.answer_for(jpeg)
        self.assertIsNotNone(data, "no reply from the model")
        flagged = (data.get("needs_current_info")
                   or str(data.get("confidence", "")).lower() != "high")
        self.assertTrue(
            flagged,
            f"A 'recently announced' question was returned as confident "
            f"({data.get('confidence')!r}); it must be flagged for review.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
