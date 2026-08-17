"""send_file_to_phone: find the file, drive WhatsApp's attach->send flow.

Fully offline — fake mouse, fake vision, fake file finder. No screen touched,
no file sent.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


class _FailSafe(Exception):
    pass


class FakePyAutoGUI:
    FailSafeException = _FailSafe

    def __init__(self):
        self.written = []
        self.pressed = []

    def write(self, text, interval=0):
        self.written.append(text)

    def press(self, key):
        self.pressed.append(key)

    def moveTo(self, x, y, duration=0):
        pass

    def click(self):
        pass

    def size(self):
        return (1000, 1000)


class FakeImage:
    size = (1000, 1000)


def _frame():
    return FakeImage(), b"jpeg"


class SendFileTests(unittest.TestCase):
    def setUp(self):
        import tools.send_file as send_file

        self.send_file = send_file
        self.fake_pg = FakePyAutoGUI()
        self._patchers = [
            mock.patch.object(send_file, "pyautogui", self.fake_pg),
            mock.patch.object(send_file.time, "sleep"),
            mock.patch.object(
                send_file, "cancellation", mock.Mock(cancelled=lambda: False)
            ),
            mock.patch.object(send_file.whatsapp, "_open_whatsapp", return_value=True),
            mock.patch.object(
                send_file.whatsapp, "_find_and_open_chat", return_value=(True, "opened")
            ),
            mock.patch.object(send_file.whatsapp, "remember"),
            mock.patch.object(send_file.screen, "capture_screen", side_effect=_frame),
            mock.patch.object(send_file.screen, "click_box", return_value=True),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def _found(self, name="resume.pdf"):
        return mock.patch.object(self.send_file, "_find_file", return_value=Path(name))

    def test_missing_file_is_reported(self):
        with mock.patch.object(self.send_file, "_find_file", return_value=None):
            result = self.send_file.send_file_to_phone("nope")
        self.assertIn("couldn't find a file", result)

    def test_no_target_and_no_self_name_asks(self):
        with self._found(), mock.patch.object(config, "WHATSAPP_SELF_NAME", ""):
            result = self.send_file.send_file_to_phone("resume")
        self.assertIn("Who should I send it to", result)

    def test_happy_path_to_own_phone(self):
        with self._found(), mock.patch.object(config, "WHATSAPP_SELF_NAME", "Me"), \
                mock.patch.object(self.send_file.screen, "locate", return_value=[1, 1, 2, 2]), \
                mock.patch.object(self.send_file.screen, "wait_for_change",
                                  return_value=(True, FakeImage(), b"j")):
            result = self.send_file.send_file_to_phone("resume")
        self.assertEqual(result, "Sent resume.pdf to your phone on WhatsApp.")
        self.assertIn("resume.pdf", self.fake_pg.written)  # path typed into dialog

    def test_named_contact(self):
        with self._found(), \
                mock.patch.object(self.send_file.screen, "locate", return_value=[1, 1, 2, 2]), \
                mock.patch.object(self.send_file.screen, "wait_for_change",
                                  return_value=(True, FakeImage(), b"j")):
            result = self.send_file.send_file_to_phone("resume", contact="Dad")
        self.assertEqual(result, "Sent resume.pdf to Dad on WhatsApp.")

    def test_attach_button_not_found_is_reported(self):
        with self._found(), mock.patch.object(config, "WHATSAPP_SELF_NAME", "Me"), \
                mock.patch.object(self.send_file.screen, "locate", return_value=None), \
                mock.patch.object(self.send_file.screen, "wait_for_change",
                                  return_value=(True, FakeImage(), b"j")):
            result = self.send_file.send_file_to_phone("resume")
        self.assertIn("couldn't find the attach button", result)

    def test_unconfirmed_send_is_not_claimed(self):
        # attach + document found; picker opens; but the send never changes the
        # screen and there's no send button -> must NOT claim it sent.
        changes = [
            (True, FakeImage(), b"j"),   # attach menu opened
            (True, FakeImage(), b"j"),   # file picker opened
            (True, FakeImage(), b"j"),   # file selected / preview
            (False, FakeImage(), b"j"),  # send NOT confirmed
        ]
        with self._found(), mock.patch.object(config, "WHATSAPP_SELF_NAME", "Me"), \
                mock.patch.object(self.send_file.screen, "locate",
                                  side_effect=[[1, 1, 2, 2], [1, 1, 2, 2], None]), \
                mock.patch.object(self.send_file.screen, "wait_for_change",
                                  side_effect=changes):
            result = self.send_file.send_file_to_phone("resume")
        self.assertIn("couldn't confirm it sent", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
