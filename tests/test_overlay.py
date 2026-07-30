"""The HUD must always reach a terminal state.

"Thinking" is the one state nothing else expires, which is why a dropped or
crashed command used to leave it spinning after the work was visibly done.
Each test here pins one of the four ways that happened.

Tk is driven for real (no display server needed on Windows), but the clock is
faked so the watchdog is testable without waiting 45 seconds.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import overlay as overlay_mod  # noqa: E402


class _Clock:
    """Monotonic clock we control."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class OverlayTestCase(unittest.TestCase):
    """Builds a real Overlay with a fake clock, skipping if Tk is unavailable."""

    def setUp(self):
        try:
            import tkinter  # noqa: F401
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"Tk unavailable: {exc}")

        self.clock = _Clock()
        patcher = mock.patch.object(overlay_mod.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

        try:
            self.hud = overlay_mod.Overlay()
        except Exception as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"Could not create Tk window: {exc}")
        self.addCleanup(self._destroy)

        # The panel is normally made click-through via win32 calls; harmless to
        # keep, but stub it so tests don't depend on window-manager behaviour.
        self.hud._make_click_through = lambda: None

    def _destroy(self):
        try:
            self.hud.root.destroy()
        except Exception:
            pass

    def pump(self):
        """Process queued messages and one animation frame, as _tick would."""
        self.hud._drain()
        if self.hud._visible:
            self.hud._animate()

    @property
    def state(self):
        return self.hud._state


class TestReplyAlwaysShows(OverlayTestCase):
    """Bug (c): a reply arriving while hidden was invisible AND never expired."""

    def test_reply_while_hidden_becomes_visible(self):
        self.assertFalse(self.hud._visible)
        self.hud.show_reply("Opened youtube.com.")
        self.pump()
        self.assertTrue(
            self.hud._visible,
            "A reply must show itself; otherwise it never animates, so its "
            "expiry timer never runs and the HUD latches.",
        )
        self.assertEqual(self.state, "reply")

    def test_reply_expires_on_its_own(self):
        self.hud.show_reply("Done.")
        self.pump()
        self.assertTrue(self.hud._visible)
        self.clock.advance(overlay_mod._REPLY_MAX_S + 1)
        self.pump()
        self.assertFalse(self.hud._visible, "The reply should have dismissed itself.")

    def test_long_replies_stay_up_longer_but_still_clamp(self):
        self.hud.show_reply("word " * 500)
        self.pump()
        self.clock.advance(overlay_mod._REPLY_MAX_S + 1)
        self.pump()
        self.assertFalse(self.hud._visible)


class TestThinkingWatchdog(OverlayTestCase):
    """Bug (d): 'thinking' had no timeout of any kind."""

    def _enter_thinking(self):
        self.hud.show()          # make the panel visible, as a real capture does
        self.hud.set_state("thinking", "working")
        self.pump()
        self.assertEqual(self.state, "thinking")

    def test_thinking_does_not_expire_early(self):
        self._enter_thinking()
        self.clock.advance(overlay_mod._THINKING_STALL_S - 5)
        self.pump()
        self.assertEqual(
            self.state, "thinking",
            "A task still within the stall window must not be cut off.",
        )

    def test_thinking_expires_when_progress_goes_quiet(self):
        self._enter_thinking()
        self.clock.advance(overlay_mod._THINKING_STALL_S + 1)
        self.pump()   # trips the watchdog, which queues a reply
        self.pump()   # drains it
        self.assertEqual(
            self.state, "reply",
            "Thinking must not spin forever once progress has stopped.",
        )

    def test_progress_updates_reset_the_stall_clock(self):
        """A genuinely long task that keeps reporting is never cut off."""
        self._enter_thinking()
        for _ in range(10):
            self.clock.advance(overlay_mod._THINKING_STALL_S - 5)
            self.hud.set_state("thinking", "still going")
            self.pump()
            self.assertEqual(self.state, "thinking")

    def test_thinking_always_terminates(self):
        """The property the user actually cares about, stated directly."""
        self._enter_thinking()
        for _ in range(200):     # ~200 frames of fake time
            self.clock.advance(1.0)
            self.pump()
            if self.state != "thinking":
                break
        else:
            self.fail("HUD stayed in 'thinking' indefinitely.")


class TestStateTransitions(OverlayTestCase):
    def test_listening_to_thinking_to_reply(self):
        self.hud.show()
        self.pump()
        self.assertEqual(self.state, "listening")
        self.assertTrue(self.hud._visible)

        self.hud.set_state("thinking", "“open youtube”")
        self.pump()
        self.assertEqual(self.state, "thinking")

        self.hud.show_reply("Opened youtube.com in chrome.")
        self.pump()
        self.assertEqual(self.state, "reply")

        self.clock.advance(overlay_mod._REPLY_MAX_S + 1)
        self.pump()
        self.assertFalse(self.hud._visible)

    def test_reply_text_is_truncated_not_dropped(self):
        self.hud.show_reply("x" * 5000)
        self.pump()
        self.assertEqual(self.state, "reply")
        self.assertLessEqual(len(self.hud._fit_reply(self.hud._reply_text)), 221)


class TestNullOverlay(unittest.TestCase):
    """The headless stand-in must satisfy the same surface."""

    def test_supports_every_method_main_calls(self):
        null = overlay_mod._NullOverlay()
        for name in ("show", "hide", "set_level", "set_state", "show_reply", "stop"):
            self.assertTrue(hasattr(null, name), f"_NullOverlay lacks {name}()")
        null.show()
        null.set_state("thinking", "detail")
        null.show_reply("done")
        null.set_level(0.5)
        null.hide()
        null.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
