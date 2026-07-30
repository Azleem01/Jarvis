"""The screen-change detector, which quietly corrupted quiz answers.

`screens_differ` used to compare 64px greyscale thumbnails by MEAN absolute
difference against a threshold of 2.0. On a text-heavy page that metric never
fires: measured on a real quiz screenshot, clicking an option scored 0.616 and
advancing to a completely different question scored 0.631 — both reported as
"SCREEN DID NOT CHANGE".

Two things went wrong as a result, and the log recorded both:

  * the computer-use prompt says "if an action changed nothing, do something
    DIFFERENT", so the agent abandoned answers it had already got right —
    step 6 of the failing run reads "Clicking option C (Germany) since the
    previous selections did not advance the question";
  * the oscillation guard only clears a repeat when `changed` is true, so
    legitimate repeated Next clicks tripped the abort at 5.

Averaging is the flaw: a page is mostly unchanged background, which dilutes any
local edit toward zero. These tests pin the replacement metric — the fraction of
pixels that actually moved — using generated frames that mimic a quiz page.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from tools import screen  # noqa: E402

W, H = 1280, 720


def page(question="Q1. Where is the ISA headquartered?", selected=None):
    """A plain MCQ frame: header, question, four option rows, a Next button."""
    img = Image.new("RGB", (W, H), "#f7f7fa")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 70], fill="#1a3a6b")
    d.text((60, 150), question, fill="#111")
    for i, label in enumerate("ABCD"):
        y = 250 + i * 70
        d.rounded_rectangle([110, y, 900, y + 50], radius=8,
                            outline="#c3c7d1", width=2, fill="white")
        d.ellipse([128, y + 14, 150, y + 36], outline="#7a7f8a", width=2)
        if selected == i:                       # radio fills + row highlights
            d.ellipse([133, y + 19, 145, y + 31], fill="#1a73e8")
            d.rounded_rectangle([110, y, 900, y + 50], radius=8,
                                outline="#1a73e8", width=3)
        d.text((170, y + 16), f"{label}. Option {label}", fill="#111")
    d.rounded_rectangle([1050, 620, 1230, 675], radius=8, fill="#1a73e8")
    return img


def option_region(i):
    """Option i as a normalised 0-1000 box, matching what a locator returns."""
    y = 250 + i * 70
    return [y / H * 1000, 110 / W * 1000, (y + 50) / H * 1000, 900 / W * 1000]


class TestChangeDetection(unittest.TestCase):
    def test_identical_frames_are_not_a_change(self):
        a = page()
        self.assertFalse(screen.screens_differ(a, a.copy()))

    def test_selecting_an_option_registers(self):
        """The regression: this used to read as 'nothing happened'."""
        before, after = page(), page(selected=1)
        self.assertTrue(
            screen.screens_differ(before, after),
            "Clicking an answer must register, or the agent is told its correct "
            "answer did nothing and picks a different option.",
        )

    def test_advancing_to_a_new_question_registers(self):
        before = page("Q1. Where is the ISA headquartered?", selected=1)
        after = page("Q2. Which planet has the most moons?")
        self.assertTrue(screen.screens_differ(before, after))

    def test_a_caret_sized_change_is_ignored(self):
        """Blinking carets and cursor ticks must stay below the threshold."""
        before = page()
        after = before.copy()
        ImageDraw.Draw(after).rectangle([400, 300, 402, 322], fill="black")
        self.assertFalse(screen.screens_differ(before, after))

    def test_the_old_metric_would_have_missed_all_of_it(self):
        """Anchors the tests above to the behaviour being guarded against.

        Mean difference on 64px thumbnails, the previous implementation.
        """
        from PIL import ImageChops, ImageStat

        def old_metric(a, b):
            size = (64, int(64 * a.height / a.width) or 1)
            diff = ImageChops.difference(a.convert("L").resize(size),
                                         b.convert("L").resize(size))
            return ImageStat.Stat(diff).mean[0]

        self.assertLess(old_metric(page(), page(selected=1)), 2.0)
        self.assertLess(old_metric(page("Q1. A"), page("Q2. B")), 2.0)

    def test_separation_is_wide_not_marginal(self):
        """A threshold that only just works is a threshold that will fail."""
        noise = screen.changed_fraction(page(), page())
        real = screen.changed_fraction(page(), page(selected=2))
        self.assertLess(noise, screen._CHANGED_FRACTION / 10)
        self.assertGreater(real, screen._CHANGED_FRACTION * 2)


class TestRegionScoped(unittest.TestCase):
    """Checking one element, used to confirm a click actually selected it."""

    def test_change_inside_the_region_is_seen(self):
        before, after = page(), page(selected=2)
        self.assertTrue(
            screen.screens_differ(before, after, threshold=0.004,
                                  region=option_region(2))
        )

    def test_change_elsewhere_does_not_count(self):
        """Selecting option 0 must not look like option 3 was selected."""
        before, after = page(), page(selected=0)
        self.assertFalse(
            screen.screens_differ(before, after, threshold=0.004,
                                  region=option_region(3))
        )

    def test_a_degenerate_region_reports_no_change(self):
        before, after = page(), page(selected=1)
        self.assertFalse(
            screen.screens_differ(before, after, region=[500, 500, 500, 500])
        )

    def test_mismatched_sizes_do_not_raise(self):
        before = page()
        after = page(selected=1).resize((640, 360))
        screen.screens_differ(before, after)  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
