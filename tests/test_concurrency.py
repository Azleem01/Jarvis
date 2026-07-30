"""Command lifecycle: the worker slot, and who is allowed to touch the HUD.

The concrete stuck-"Thinking" bug lived here. The HUD was switched to
"thinking" on the listener thread, and only *then* did the worker thread try to
claim the busy lock — so a command dropped for overlapping returned without
ever delivering a reply, and the panel span forever.

Everything heavy (Whisper, the Gemini client, the audio device, the HUD) is
replaced with a stub, so these tests exercise main.py's real control flow.
"""

import os
import sys
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class FakeHud:
    """Records everything main.py asks the HUD to display."""

    def __init__(self):
        self.calls = []          # ("state"|"reply", payload)
        self.available = True

    def show(self):
        self.calls.append(("show", ""))

    def hide(self):
        self.calls.append(("hide", ""))

    def set_level(self, level):
        pass

    def set_state(self, state, detail=""):
        self.calls.append(("state", state))

    def show_reply(self, text):
        self.calls.append(("reply", str(text)))

    def stop(self):
        pass

    @property
    def final(self):
        """The last thing the user would actually be looking at."""
        return self.calls[-1] if self.calls else None

    @property
    def replied(self):
        return any(kind == "reply" for kind, _ in self.calls)


class FakeRecorder:
    def __init__(self, *a, **kw):
        self.last_peak = 0.5           # loud enough to count as speech
        self.stopped = 0

    def start(self):
        pass

    def stop(self):
        self.stopped += 1
        return np.ones(16000, dtype=np.float32) * 0.1


class FakeTranscriber:
    text = "open youtube in chrome"

    def __init__(self, *a, **kw):
        pass

    def transcribe(self, audio):
        return self.text


class FakeAgent:
    def __init__(self, *a, **kw):
        self.reply = "Opened youtube.com in chrome."
        self.block = None          # optional Event to hang on

    def handle(self, text):
        if self.block is not None:
            self.block.wait(timeout=5)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class JarvisTestCase(unittest.TestCase):
    def setUp(self):
        import main as main_mod
        self.main = main_mod

        patches = [
            mock.patch.object(main_mod, "Recorder", FakeRecorder),
            mock.patch.object(main_mod, "Transcriber", FakeTranscriber),
            mock.patch.object(main_mod, "JarvisAgent", FakeAgent),
            mock.patch.object(main_mod.config, "validate", lambda: None),
            mock.patch.object(main_mod.speaker_mute, "prewarm", lambda: None),
            mock.patch.object(main_mod.speaker_mute, "restore", lambda: None),
            mock.patch.object(main_mod.speaker_mute, "mute", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        self.hud = FakeHud()
        self.jarvis = main_mod.Jarvis(self.hud)

    def release_hotkey(self):
        """Drive _on_release as though a real push-to-talk take just ended."""
        from pynput import keyboard
        key = (keyboard.Key.caps_lock if self.main._USE_CAPS
               else keyboard.Key.space)
        with self.jarvis._state:
            self.jarvis._held = True
            self.jarvis._capturing = True
        self.jarvis._on_release(key)

    def drain_workers(self, timeout=5.0):
        for t in threading.enumerate():
            if t is not threading.current_thread() and t.daemon and t.is_alive():
                t.join(timeout=timeout)


class TestBusySlot(JarvisTestCase):
    def test_normal_command_ends_in_a_reply(self):
        self.release_hotkey()
        self.drain_workers()
        self.assertTrue(self.hud.replied)
        self.assertEqual(self.hud.final[0], "reply")
        self.assertFalse(self.jarvis._busy.locked(), "worker slot was not released")

    def test_dropped_overlapping_command_still_replies(self):
        """The original bug: HUD left spinning with nothing coming to clear it."""
        self.jarvis._busy.acquire()          # a previous command is still running
        self.addCleanup(self.jarvis._busy.release)

        self.release_hotkey()

        self.assertTrue(
            self.hud.replied,
            "A dropped command must still deliver a reply — otherwise the HUD "
            "sits on 'Thinking' forever with no worker left to clear it.",
        )
        self.assertEqual(self.hud.final[0], "reply")

    def test_dropped_command_never_shows_thinking(self):
        """Don't even flash 'Thinking' for work that was never started."""
        self.jarvis._busy.acquire()
        self.addCleanup(self.jarvis._busy.release)

        self.release_hotkey()

        states = [payload for kind, payload in self.hud.calls if kind == "state"]
        self.assertNotIn("thinking", states)

    def test_silent_take_replies_and_frees_the_slot(self):
        self.jarvis.recorder.last_peak = 0.0
        self.release_hotkey()
        self.drain_workers()
        self.assertTrue(self.hud.replied)
        self.assertIn("silent", self.hud.final[1].lower())
        self.assertFalse(self.jarvis._busy.locked())

    def test_slot_is_released_when_the_agent_raises(self):
        self.jarvis.agent.reply = RuntimeError("gemini exploded")
        self.release_hotkey()
        self.drain_workers()
        self.assertFalse(
            self.jarvis._busy.locked(),
            "An exception must not strand the worker slot — every later "
            "command would be dropped.",
        )
        self.assertEqual(self.hud.final[0], "reply")

    def test_empty_transcription_still_replies(self):
        self.jarvis.transcriber.text = ""
        self.release_hotkey()
        self.drain_workers()
        self.assertEqual(self.hud.final[0], "reply")
        self.assertFalse(self.jarvis._busy.locked())

    def test_every_path_ends_in_a_reply(self):
        """Whatever happens, the HUD must not be left mid-flight."""
        scenarios = {
            "normal": lambda: None,
            "silent": lambda: setattr(self.jarvis.recorder, "last_peak", 0.0),
            "no speech": lambda: setattr(self.jarvis.transcriber, "text", ""),
            "agent raises": lambda: setattr(
                self.jarvis.agent, "reply", RuntimeError("boom")),
            "empty reply": lambda: setattr(self.jarvis.agent, "reply", ""),
        }
        for name, setup in scenarios.items():
            with self.subTest(scenario=name):
                self.setUp()
                setup()
                self.release_hotkey()
                self.drain_workers()
                self.assertEqual(
                    self.hud.final[0], "reply",
                    f"scenario '{name}' left the HUD on "
                    f"'{self.hud.final[0]}' instead of delivering a reply",
                )
                self.assertFalse(self.jarvis._busy.locked())


class TestEscCancel(JarvisTestCase):
    """Esc must only act while a command can still be cancelled.

    _busy stays held until the worker's `finally`, which runs *after* the reply
    is delivered. Keying Esc off _busy meant pressing it just as a command
    finished repainted "Cancelling…" over the reply, with no worker left to
    clear it — the stuck-"thinking" symptom, reintroduced by the cancel path.
    """

    def esc(self):
        from pynput import keyboard
        self.jarvis._on_press(keyboard.Key.esc)

    def test_esc_while_working_cancels(self):
        self.jarvis._working.set()
        self.addCleanup(self.jarvis._working.clear)
        self.esc()
        self.assertIn(("state", "thinking"), self.hud.calls)

    def test_esc_after_reply_does_not_repaint_thinking(self):
        """_busy is still held here; _working is not."""
        self.jarvis._busy.acquire()
        self.addCleanup(self.jarvis._busy.release)
        self.hud.show_reply("Opened youtube.com.")
        self.esc()
        self.assertEqual(
            self.hud.final[0], "reply",
            "Esc landed after the reply and dragged the HUD back to 'thinking' "
            "with no worker left to clear it.",
        )

    def test_esc_at_idle_is_inert(self):
        self.esc()
        self.assertEqual(self.hud.calls, [])

    def test_working_is_clear_after_a_command(self):
        self.release_hotkey()
        self.drain_workers()
        self.assertFalse(self.jarvis._working.is_set())

    def test_working_is_clear_after_the_agent_raises(self):
        self.jarvis.agent.reply = RuntimeError("boom")
        self.release_hotkey()
        self.drain_workers()
        self.assertFalse(
            self.jarvis._working.is_set(),
            "A crashed command left _working set, so every later Esc would be "
            "swallowed as a cancel.",
        )


class TestStaleProgress(JarvisTestCase):
    """A worker whose task was abandoned must not repaint the HUD."""

    def test_current_worker_can_report(self):
        with self.jarvis._state:
            self.jarvis._active_seq = 7
        self.jarvis._local.seq = 7
        self.jarvis._report_progress("Step 2/12: clicking")
        self.assertIn(("state", "thinking"), self.hud.calls)

    def test_stale_worker_is_ignored(self):
        with self.jarvis._state:
            self.jarvis._active_seq = 9
        self.jarvis._local.seq = 3        # an older, abandoned command
        self.jarvis._report_progress("Step 5/12: still going")
        self.assertNotIn(
            ("state", "thinking"), self.hud.calls,
            "A stale progress update re-asserted 'thinking' over a delivered "
            "reply — this is what re-froze the HUD after a task finished.",
        )

    def test_unstamped_thread_is_ignored(self):
        with self.jarvis._state:
            self.jarvis._active_seq = 1
        self.jarvis._report_progress("from nowhere")
        self.assertEqual(self.hud.calls, [])

    def test_late_progress_cannot_overwrite_a_delivered_reply(self):
        """End to end: finish a command, then let a straggler step fire."""
        self.release_hotkey()
        self.drain_workers()
        self.assertEqual(self.hud.final[0], "reply")

        self.jarvis._local.seq = 0        # the abandoned worker's generation
        self.jarvis._report_progress("Step 9/12: still clicking around")

        self.assertEqual(
            self.hud.final[0], "reply",
            "The delivered reply was overwritten by a stale progress update.",
        )


class TestRecorderThreadSafety(unittest.TestCase):
    """start() runs on the hold timer, stop() on the listener thread."""

    def test_start_stop_race_does_not_corrupt_state(self):
        from stt_engine import Recorder

        rec = Recorder()
        with mock.patch("stt_engine.sd.InputStream") as stream:
            stream.return_value = mock.Mock()
            errors = []

            def hammer():
                try:
                    for _ in range(200):
                        rec.start()
                        rec.stop()
                except Exception as exc:   # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=hammer) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            self.assertEqual(errors, [], f"race produced errors: {errors}")
            self.assertFalse(rec._recording)

    def test_frame_buffer_is_capped(self):
        """A key release that never arrives must not grow memory without limit."""
        import stt_engine
        from stt_engine import Recorder

        rec = Recorder()
        block = np.zeros((512, 1), dtype=np.float32)
        for _ in range(stt_engine._MAX_FRAMES + 500):
            rec._callback(block, 512, None, None)
        self.assertLessEqual(len(rec._frames), stt_engine._MAX_FRAMES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
