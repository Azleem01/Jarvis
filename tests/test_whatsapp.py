"""The by-name WhatsApp path: cache, routing, and the vision send loop.

Everything runs offline with a fake mouse and a fake vision model, the same way
tests/test_quiz.py fakes its pointing calls. No screen is touched, no network is
hit, no message is sent.
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402


class _FailSafe(Exception):
    """Stand-in for pyautogui.FailSafeException."""


class FakePyAutoGUI:
    """A no-op mouse/keyboard that records what it was asked to do."""

    FailSafeException = _FailSafe

    def __init__(self):
        self.written = []
        self.pressed = []
        self.scrolls = 0

    def write(self, text, interval=0):
        self.written.append(text)

    def press(self, key):
        self.pressed.append(key)

    def hotkey(self, *keys):
        pass

    def moveTo(self, x, y, duration=0):
        pass

    def click(self):
        pass

    def scroll(self, amount, x=None, y=None):
        self.scrolls += 1

    def size(self):
        return (1000, 1000)


class FakeImage:
    size = (1000, 1000)


def _frame():
    return FakeImage(), b"jpeg-bytes"


class ContactCacheTests(unittest.TestCase):
    def setUp(self):
        import tools.whatsapp as whatsapp

        self.whatsapp = whatsapp
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)  # start with no file, so we test creation too
        self.cache_path = path
        patcher = mock.patch.object(config, "WHATSAPP_CONTACT_CACHE", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))

    def test_missing_cache_reads_as_empty(self):
        self.assertEqual(self.whatsapp._load_cache(), {})
        self.assertEqual(self.whatsapp.cached_phone("nobody"), "")

    def test_remember_then_read_back(self):
        self.whatsapp.remember("Alex Example")
        self.assertEqual(self.whatsapp.cached_phone("alex example"), "")
        # Name is stored canonically and case-insensitively.
        cache = self.whatsapp._load_cache()
        self.assertEqual(cache["alex example"]["name"], "Alex Example")

    def test_remember_keeps_a_known_number(self):
        self.whatsapp.remember("Alex", phone="+15551234567")
        self.assertEqual(self.whatsapp.cached_phone("ALEX"), "+15551234567")

    def test_corrupt_cache_does_not_raise(self):
        with open(self.cache_path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(self.whatsapp._load_cache(), {})


class SendRoutingTests(unittest.TestCase):
    """send_whatsapp_message: known number -> deep link; unknown -> UI search."""

    def setUp(self):
        import tools.os_tools as os_tools

        self.os_tools = os_tools

    def test_known_number_uses_the_deep_link(self):
        with mock.patch.object(config, "WHATSAPP_CONTACTS", {"alex": "+15550001111"}), \
                mock.patch("os.startfile", create=True) as startfile, \
                mock.patch.object(self.os_tools, "_wait_for_window", return_value=True), \
                mock.patch.object(self.os_tools.time, "sleep"), \
                mock.patch("pyautogui.press") as press:
            result = self.os_tools.send_whatsapp_message("Alex", "hello there")

        self.assertEqual(result, "Sent WhatsApp message to Alex.")
        link = startfile.call_args[0][0]
        self.assertIn("phone=+15550001111", link)
        self.assertIn("text=hello%20there", link)
        press.assert_called_once_with("enter")

    def test_cached_number_uses_the_deep_link(self):
        with mock.patch.object(config, "WHATSAPP_CONTACTS", {}), \
                mock.patch("tools.whatsapp.cached_phone", return_value="+15552223333"), \
                mock.patch("os.startfile", create=True) as startfile, \
                mock.patch.object(self.os_tools, "_wait_for_window", return_value=True), \
                mock.patch.object(self.os_tools.time, "sleep"), \
                mock.patch("pyautogui.press"):
            result = self.os_tools.send_whatsapp_message("Cached Person", "hi")

        self.assertEqual(result, "Sent WhatsApp message to Cached Person.")
        self.assertIn("phone=+15552223333", startfile.call_args[0][0])

    def test_unknown_contact_falls_through_to_ui_search(self):
        with mock.patch.object(config, "WHATSAPP_CONTACTS", {}), \
                mock.patch("tools.whatsapp.cached_phone", return_value=""), \
                mock.patch("tools.whatsapp.send_via_ui", return_value="SENT-VIA-UI") as ui:
            result = self.os_tools.send_whatsapp_message("Someone New", "yo")

        self.assertEqual(result, "SENT-VIA-UI")
        ui.assert_called_once_with("Someone New", "yo")


class SendViaUITests(unittest.TestCase):
    """The vision-driven send: search box -> contact row -> message box -> send."""

    def setUp(self):
        import tools.whatsapp as whatsapp

        self.whatsapp = whatsapp
        self.fake_pg = FakePyAutoGUI()
        # Skip the real launch/window wait and settle delays.
        self._patchers = [
            mock.patch.object(whatsapp, "pyautogui", self.fake_pg),
            mock.patch.object(whatsapp, "_open_whatsapp", return_value=True),
            mock.patch.object(whatsapp.time, "sleep"),
            mock.patch.object(whatsapp, "cancellation", mock.Mock(cancelled=lambda: False)),
            mock.patch.object(whatsapp.screen, "capture_screen", side_effect=_frame),
            mock.patch.object(whatsapp.screen, "click_box", return_value=True),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_happy_path_sends_and_remembers(self):
        remembered = []
        with mock.patch.object(self.whatsapp.screen, "locate", return_value=[10, 10, 20, 20]), \
                mock.patch.object(self.whatsapp.screen, "wait_for_change",
                                  return_value=(True, FakeImage(), b"j")), \
                mock.patch.object(self.whatsapp, "remember",
                                  side_effect=lambda n, phone="": remembered.append(n)):
            result = self.whatsapp.send_via_ui("Jamie", "on my way")

        self.assertEqual(result, "Sent WhatsApp message to Jamie.")
        self.assertIn("on my way", self.fake_pg.written)
        self.assertIn("enter", self.fake_pg.pressed)
        self.assertEqual(remembered, ["Jamie"])

    def test_search_box_not_found_is_reported_honestly(self):
        with mock.patch.object(self.whatsapp.screen, "locate", return_value=None), \
                mock.patch.object(self.whatsapp.screen, "wait_for_change",
                                  return_value=(True, FakeImage(), b"j")):
            result = self.whatsapp.send_via_ui("Jamie", "hi")

        self.assertIn("Couldn't message Jamie", result)
        self.assertIn("search box", result)
        # Nothing was sent.
        self.assertNotIn("enter", self.fake_pg.pressed)

    def test_contact_not_found_is_reported(self):
        # Search box found, but the result row is not.
        boxes = [[0, 0, 5, 5], None]
        with mock.patch.object(self.whatsapp.screen, "locate", side_effect=boxes), \
                mock.patch.object(self.whatsapp.screen, "wait_for_change",
                                  return_value=(True, FakeImage(), b"j")):
            result = self.whatsapp.send_via_ui("Ghost", "hi")

        self.assertIn("couldn't find a contact matching 'Ghost'", result)

    def test_unconfirmed_send_is_not_claimed_as_success(self):
        # Every locate succeeds and the chat opens, but the final send never
        # changes the screen -> we must NOT say it was sent (gotcha 22/14).
        change = [
            (True, FakeImage(), b"j"),   # results filtered
            (True, FakeImage(), b"j"),   # chat opened
            (False, FakeImage(), b"j"),  # send NOT confirmed
        ]
        with mock.patch.object(self.whatsapp.screen, "locate", return_value=[1, 1, 2, 2]), \
                mock.patch.object(self.whatsapp.screen, "wait_for_change", side_effect=change):
            result = self.whatsapp.send_via_ui("Jamie", "hi")

        self.assertIn("couldn't confirm it was sent", result)


class CaptureContactsTests(unittest.TestCase):
    def setUp(self):
        import tools.whatsapp as whatsapp

        self.whatsapp = whatsapp
        self.fake_pg = FakePyAutoGUI()
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)
        self.cache_path = path
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        self._patchers = [
            mock.patch.object(config, "WHATSAPP_CONTACT_CACHE", path),
            mock.patch.object(whatsapp, "pyautogui", self.fake_pg),
            mock.patch.object(whatsapp, "_open_whatsapp", return_value=True),
            mock.patch.object(whatsapp.time, "sleep"),
            mock.patch.object(whatsapp, "cancellation", mock.Mock(cancelled=lambda: False)),
            mock.patch.object(whatsapp.screen, "capture_screen", side_effect=_frame),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_scrolls_until_no_change_and_saves_all_names(self):
        reads = [["Alice", "Bob"], ["Charlie"]]
        changes = [
            (True, FakeImage(), b"j"),   # list moved after first scroll
            (False, FakeImage(), b"j"),  # bottom reached
        ]
        with mock.patch.object(self.whatsapp, "_read_visible_names", side_effect=reads), \
                mock.patch.object(self.whatsapp.screen, "wait_for_change", side_effect=changes):
            result = self.whatsapp.capture_whatsapp_contacts()

        self.assertIn("Saved 3 WhatsApp contacts", result)
        cache = self.whatsapp._load_cache()
        self.assertEqual(set(cache), {"alice", "bob", "charlie"})
        self.assertGreater(self.fake_pg.scrolls, 0)

    def test_no_names_is_reported_not_faked(self):
        with mock.patch.object(self.whatsapp, "_read_visible_names", return_value=[]), \
                mock.patch.object(self.whatsapp.screen, "wait_for_change",
                                  return_value=(False, FakeImage(), b"j")):
            result = self.whatsapp.capture_whatsapp_contacts()

        self.assertIn("couldn't read any contact names", result)


class ContactResolveTests(unittest.TestCase):
    """Fuzzy + relationship-aware matching, and the confirm-when-unsure gate."""

    def setUp(self):
        from tools import contacts

        self.contacts = contacts
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)  # empty cache to start
        self.cache_path = path
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        p = mock.patch.object(config, "WHATSAPP_CONTACT_CACHE", path)
        p.start()
        self.addCleanup(p.stop)

    def _contacts(self, mapping):
        return mock.patch.object(config, "WHATSAPP_CONTACTS", mapping)

    def test_relationship_variant_matches_saved_spelling(self):
        # Saved as "mum"; the user says "mom".
        with self._contacts({"mum": "+1"}):
            self.assertEqual(self.contacts.resolve_contact("mom"), ("match", "mum"))

    def test_daddy_resolves_to_dad(self):
        with self._contacts({"dad": "+1"}):
            self.assertEqual(self.contacts.resolve_contact("daddy"), ("match", "dad"))

    def test_fuzzy_close_name_matches(self):
        with self._contacts({"jonathan": "+1"}):
            self.assertEqual(
                self.contacts.resolve_contact("jonathon"), ("match", "jonathan")
            )

    def test_two_close_names_are_ambiguous(self):
        with self._contacts({"john": "+1", "joan": "+2"}):
            kind, names = self.contacts.resolve_contact("jon")
            self.assertEqual(kind, "ambiguous")
            self.assertEqual(len(names), 2)

    def test_nothing_close_is_none(self):
        with self._contacts({"mum": "+1"}):
            self.assertEqual(self.contacts.resolve_contact("zxqw"), ("none", []))

    def test_ambiguous_name_asks_and_never_sends(self):
        """The whole point: an unclear match must ask, not message a wrong person."""
        import tools.os_tools as os_tools

        with self._contacts({"john": "+1", "joan": "+2"}), \
                mock.patch("os.startfile", create=True) as startfile, \
                mock.patch("tools.whatsapp.send_via_ui") as ui:
            result = os_tools.send_whatsapp_message("jon", "hi")

        self.assertIn("Who did you mean", result)
        startfile.assert_not_called()
        ui.assert_not_called()

    def test_taught_alias_then_resolves(self):
        from tools import whatsapp

        with self._contacts({}):
            whatsapp.link_contact_alias("boss", "Aisha Khan")
            self.assertEqual(
                self.contacts.resolve_contact("boss"), ("match", "Aisha Khan")
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
