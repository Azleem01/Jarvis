"""Window management, against a fake window manager.

The interesting cases are the refusals. ``close`` can lose unsaved work, so a
mis-transcribed name must fall through to "no such window" rather than closing
whatever happened to be in front — the same class of bug as the fast path
launching a sentence as an application name.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import windows  # noqa: E402


class FakeWindow:
    def __init__(self, title, raises=()):
        self.title = title
        self.raises = set(raises)
        self.actions = []

    def _do(self, name):
        self.actions.append(name)
        if name in self.raises:
            raise RuntimeError(f"{name} refused by the window manager")

    def activate(self):
        self._do("activate")

    def minimize(self):
        self._do("minimize")

    def restore(self):
        self._do("restore")

    def maximize(self):
        self._do("maximize")

    def close(self):
        self._do("close")


class WindowTestCase(unittest.TestCase):
    def use(self, *titles, active=None, raises=()):
        """Install a fake window list, and the window reported as in front."""
        self.windows = [FakeWindow(t, raises=raises) for t in titles]
        by_title = {w.title: w for w in self.windows}
        self.active = by_title.get(active) if active else None

        patcher = mock.patch.object(
            windows, "find_windows",
            side_effect=lambda needle: [
                w for w in self.windows
                if needle and needle.strip().lower() in w.title.lower()
            ],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # _activate reads the foreground back to confirm the switch worked.
        active_patcher = mock.patch.object(
            windows, "_active_window", side_effect=lambda: self.active
        )
        active_patcher.start()
        self.addCleanup(active_patcher.stop)

        sleep_patcher = mock.patch.object(windows.time, "sleep")
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        return by_title


class TestFocusWindow(WindowTestCase):
    def test_a_matching_window_is_brought_forward(self):
        self.use("Text Editor - notes.txt", active="Text Editor - notes.txt")
        reply = windows.focus_window("text editor")
        self.assertIn("Switched to Text Editor", reply)
        self.assertIn("activate", self.windows[0].actions)

    def test_matching_is_case_insensitive_and_partial(self):
        self.use("Spreadsheet - budget", active="Spreadsheet - budget")
        self.assertIn("Switched to", windows.focus_window("SPREAD"))

    def test_no_match_says_so_and_touches_nothing(self):
        self.use("Text Editor", active="Text Editor")
        reply = windows.focus_window("browser")
        self.assertIn("No open window matching 'browser'", reply)
        self.assertEqual(self.windows[0].actions, [])

    def test_an_empty_name_is_refused(self):
        self.use("Text Editor", active="Text Editor")
        self.assertIn("which window", windows.focus_window("   "))

    def test_a_refused_activation_is_reported_not_claimed(self):
        """SetForegroundWindow can be denied; saying "switched" would be a lie."""
        self.use("Text Editor", active=None, raises=("activate", "restore"))
        reply = windows.focus_window("text editor")
        self.assertNotIn("Switched to", reply)
        self.assertIn("would not bring it to the front", reply)

    def test_the_minimise_restore_nudge_is_tried_when_activate_raises(self):
        self.use("Text Editor", active="Text Editor", raises=("activate",))
        windows.focus_window("text editor")
        self.assertIn("restore", self.windows[0].actions)


class TestArrangeWindow(WindowTestCase):
    def test_maximise_and_minimise_the_foreground_window(self):
        self.use("Text Editor", active="Text Editor")
        self.assertIn("Maximised", windows.arrange_window("maximise"))
        self.assertIn("Minimised", windows.arrange_window("minimise"))
        self.assertIn("maximize", self.windows[0].actions)
        self.assertIn("minimize", self.windows[0].actions)

    def test_american_and_british_spellings_both_work(self):
        """The model produces whichever the user said."""
        self.use("Text Editor", active="Text Editor")
        for verb in ("minimise", "minimize", "maximise", "maximize"):
            self.assertNotIn("don't know how", windows.arrange_window(verb))

    def test_an_empty_target_means_the_window_in_front(self):
        self.use("Editor", "Browser", active="Browser")
        reply = windows.arrange_window("maximise")
        self.assertIn("Browser", reply)

    def test_a_named_target_wins_over_the_foreground(self):
        self.use("Editor", "Browser", active="Browser")
        reply = windows.arrange_window("maximise", "editor")
        self.assertIn("Editor", reply)

    def test_close_names_what_it_closed(self):
        """Closing can lose work, so the reply has to be specific."""
        self.use("Text Editor - notes.txt", active="Text Editor - notes.txt")
        reply = windows.arrange_window("close")
        self.assertIn("Closed Text Editor - notes.txt", reply)

    def test_close_refuses_an_unmatched_name(self):
        """A mistranscribed name must never close the foreground window."""
        self.use("Text Editor", active="Text Editor")
        reply = windows.arrange_window("close", "speadsheet")
        self.assertIn("No open window matching", reply)
        self.assertEqual(self.windows[0].actions, [])

    def test_snapping_uses_the_os_shortcut(self):
        self.use("Editor", active="Editor")
        with mock.patch.object(windows, "pyautogui", create=True), \
             mock.patch.dict(sys.modules, {"pyautogui": mock.Mock()}) as mods:
            reply = windows.arrange_window("left")
            hotkey = mods["pyautogui"].hotkey
        self.assertIn("Snapped Editor to the left", reply)
        hotkey.assert_called_once_with("win", "left")

    def test_snapping_a_window_that_will_not_focus_is_not_claimed(self):
        """Win+Left acts on the foreground, so an unfocused snap hits the
        wrong window. Reporting success there would move something else.

        The window is named explicitly rather than left to the foreground: with
        no foreground window arrange_window returns early, and this test would
        then pass without ever reaching the snap path it exists to guard.
        """
        self.use("Editor", active=None, raises=("activate", "restore"))
        with mock.patch.dict(sys.modules, {"pyautogui": mock.Mock()}) as mods:
            reply = windows.arrange_window("right", "editor")
            mods["pyautogui"].hotkey.assert_not_called()
        self.assertNotIn("Snapped", reply)
        self.assertIn("would not bring it to the front", reply)

    def test_minimise_all_needs_no_window(self):
        self.use(active=None)
        with mock.patch.dict(sys.modules, {"pyautogui": mock.Mock()}) as mods:
            reply = windows.arrange_window("minimise_all")
            mods["pyautogui"].hotkey.assert_called_once_with("win", "d")
        self.assertIn("Minimised everything", reply)

    def test_an_unknown_verb_changes_nothing(self):
        self.use("Editor", active="Editor")
        reply = windows.arrange_window("defenestrate")
        self.assertIn("don't know how", reply)
        self.assertEqual(self.windows[0].actions, [])

    def test_no_foreground_window_is_reported(self):
        self.use(active=None)
        self.assertIn("couldn't tell which window", windows.arrange_window("close"))

    def test_a_window_manager_error_is_reported_not_raised(self):
        self.use("Editor", active="Editor", raises=("close",))
        reply = windows.arrange_window("close")
        self.assertIn("Could not close", reply)


class TestFindWindows(unittest.TestCase):
    """The matcher os_tools now shares for its WhatsApp wait."""

    def _pygetwindow(self, *titles):
        module = mock.Mock()
        module.getAllWindows.return_value = [FakeWindow(t) for t in titles]
        return module

    def test_untitled_windows_are_skipped(self):
        """Invisible helper windows have empty titles; matching one would make
        a close land on something the user cannot see."""
        module = self._pygetwindow("", "   ", "Editor")
        with mock.patch.dict(sys.modules, {"pygetwindow": module}):
            found = windows.find_windows("e")
        self.assertEqual([w.title for w in found], ["Editor"])

    def test_an_empty_needle_matches_nothing(self):
        """Otherwise a blank target would match every window on the desktop."""
        module = self._pygetwindow("Editor", "Browser")
        with mock.patch.dict(sys.modules, {"pygetwindow": module}):
            self.assertEqual(windows.find_windows(""), [])
            self.assertEqual(windows.find_windows("   "), [])

    def test_a_raising_window_manager_returns_no_matches(self):
        module = mock.Mock()
        module.getAllWindows.side_effect = RuntimeError("COM failure")
        with mock.patch.dict(sys.modules, {"pygetwindow": module}):
            self.assertEqual(windows.find_windows("editor"), [])


class TestOsToolsStillUsesTheSharedMatcher(unittest.TestCase):
    def test_wait_for_window_delegates(self):
        """os_tools kept the name but not a second copy of the logic."""
        from tools import os_tools

        with mock.patch.object(windows, "wait_for_window",
                               return_value=True) as waiter:
            self.assertTrue(os_tools._wait_for_window("chat", timeout=1.0))
        waiter.assert_called_once_with("chat", timeout=1.0)


if __name__ == "__main__":
    unittest.main()
