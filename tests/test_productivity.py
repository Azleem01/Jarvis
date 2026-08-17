"""take_screenshot must save quietly and never fling an image window open.

The confirmed bug: an on-screen assignment said "include a screenshot", the
model called take_screenshot, and its os.startfile popped an image viewer in
front of the user mid-task. It now only saves and returns the path.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import productivity  # noqa: E402


class _FakeImage:
    def __init__(self):
        self.saved_to = None

    def save(self, path, format=None):  # noqa: A002 - mirrors PIL's signature
        self.saved_to = path


class TakeScreenshotTests(unittest.TestCase):
    def test_saves_but_never_opens_the_image(self):
        fake = _FakeImage()
        with mock.patch.object(
            productivity.screen, "capture_screen", return_value=(fake, b"jpeg")
        ), mock.patch("os.startfile", create=True) as startfile, mock.patch.object(
            productivity.Path, "mkdir", lambda *a, **k: None
        ):
            result = productivity.take_screenshot()

        startfile.assert_not_called()
        self.assertIsNotNone(fake.saved_to, "the screenshot should still be saved")
        self.assertIn("saved", result.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
