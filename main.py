"""Jarvis — push-to-talk desktop assistant entry point.

Lifecycle:
  * Hold the hotkey (Caps Lock by default). After ``config.HOLD_SECONDS`` the
    mic opens and the blue HUD appears — a quick tap is left alone and still
    works as an ordinary Caps Lock toggle.
  * Release it -> the HUD hides, a worker thread transcribes the audio locally,
    routes it through Gemini, runs any tool the model chose, and prints the
    reply.

Threading layout (Tk is picky about this):

  main thread      Tk event loop, driving the recording HUD.
  listener thread  pynput keyboard hook -> starts/stops capture.
  timer thread     fires once the hotkey has been held long enough.
  worker thread    STT + Gemini per command, so holding the key stays smooth.

Overlapping commands are ignored while one is in flight.
"""

from __future__ import annotations

import atexit
import ctypes
import datetime
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pyautogui
from pynput import keyboard

import build_info
import cancellation
import config
import overlay as overlay_mod
import providers
import self_restart
import speaker_mute
from llm_agent import JarvisAgent
from stt_engine import Recorder, Transcriber
from tools import computer_use

# Under pythonw (the auto-start path) there is no console at all — stdout and
# stderr are None and every diagnostic would vanish. Redirect them to a log
# file so "check the log" is always possible.
_HEADLESS = sys.stdout is None or sys.stderr is None
if _HEADLESS:
    _log_dir = Path(__file__).resolve().parent / "logs"
    _log_dir.mkdir(exist_ok=True)
    _log = open(  # noqa: SIM115 — deliberately left open for process lifetime
        _log_dir / "azleem.log", "a", encoding="utf-8", buffering=1
    )
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log
    # The build stamp is the point of this line as much as the timestamp: it is
    # how you tell a running process apart from the source sitting on disk.
    print(
        f"\n===== Azleem started {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        f" | {build_info.describe()} ====="
    )

# Windows consoles default to a legacy code page (cp1252), which raises
# UnicodeEncodeError on the em dash in the banner below and on the curly quotes,
# accents and symbols Gemini routinely returns — losing the reply. Force UTF-8.
# line_buffering matters when output is redirected to a file: without it,
# replies sit in an 8 KB block buffer and the log looks frozen mid-command.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(  # type: ignore[union-attr]
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):
        pass  # Not a real stream (redirected/wrapped) — nothing to do.

pyautogui.FAILSAFE = True

# ---- Hotkey selection ------------------------------------------------------
# Caps Lock hold-to-talk, or Ctrl+Space hold-to-talk.
_USE_CAPS = config.HOTKEY != "ctrl_space"
_TRIGGER_KEY = keyboard.Key.caps_lock if _USE_CAPS else keyboard.Key.space

# Virtual key code for Caps Lock, used to read/reset its toggle LED state.
_VK_CAPITAL = 0x14

# Below this peak amplitude a take is effectively silence — almost always a
# muted mic or the wrong input device rather than a quiet speaker.
_SILENCE_PEAK = 0.002

# How close to a screen corner the cursor must get to count as "slammed into"
# it — the whole-task cancel gesture. Small, so ordinary mousing near an edge
# doesn't trigger it; you have to actually drive into the corner. This mirrors
# pyautogui's own FAILSAFE corners (which only abort a pyautogui action), but
# applies to the whole command, at any time, not just during a click.
_CORNER_PX = 8
# How often the corner watcher samples the cursor while a command is in flight.
_CORNER_POLL_S = 0.05


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos() -> "tuple[int, int]":
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _screen_size() -> "tuple[int, int]":
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def _in_corner(x: int, y: int, w: int, h: int, margin: int = _CORNER_PX) -> bool:
    """True when (x, y) sits in any corner box of a w×h screen.

    A corner needs *both* axes at an extreme — parking at the middle of an edge
    (which normal mousing does constantly) is not a corner and must not cancel.
    """
    near_x = x <= margin or x >= w - 1 - margin
    near_y = y <= margin or y >= h - 1 - margin
    return near_x and near_y


def _caps_is_on() -> bool:
    return bool(ctypes.windll.user32.GetKeyState(_VK_CAPITAL) & 1)


class Jarvis:
    def __init__(self, hud) -> None:  # noqa: ANN001
        config.validate()
        self.hud = hud
        self.recorder = Recorder(level_callback=hud.set_level)
        self.transcriber = Transcriber()  # loads model up front
        speaker_mute.prewarm()  # resolve the audio endpoint before it's needed
        self.agent = JarvisAgent()
        # Stream the computer-use agent's step-by-step progress into the HUD.
        computer_use.set_progress_callback(self._report_progress)
        self._busy = threading.Lock()

        # Command generation. A worker whose task was abandoned can still be
        # mid-step and emit progress; without this its stale update would
        # re-assert "thinking" over an already-delivered reply.
        self._active_seq = 0
        self._local = threading.local()
        # Set only while a command is genuinely in flight — i.e. cancelling it
        # can still change the outcome. Cleared before the reply is delivered.
        self._working = threading.Event()

        self._state = threading.Lock()
        self._held = False        # hotkey physically down
        self._capturing = False   # mic actually open
        self._hold_timer: threading.Timer | None = None
        self._ctrl_down = False
        self._caps_was_on = False
        # Injected Caps Lock events we must not mistake for the user's own.
        self._ignore_caps = 0

    def _report_progress(self, detail: str) -> None:
        """Push an agent step into the HUD, unless this worker is stale.

        Runs on the worker thread that owns the task, so the generation stamped
        into thread-local storage identifies which command is reporting.
        """
        if getattr(self._local, "seq", None) != self._active_seq:
            return  # an abandoned task still winding down; ignore it
        self.hud.set_state("thinking", detail)

    # -- Caps Lock toggle bookkeeping ----------------------------------------
    def _swallow_next_caps(self, count: int = 2) -> None:
        """Ignore ``count`` upcoming Caps Lock events (our own injected tap).

        Without this, the reset tap below re-enters the listener and starts a
        phantom recording. The timed clear matters too: if the injected events
        never reach the hook, a stuck counter would swallow the user's next
        real keypress.
        """
        with self._state:
            self._ignore_caps = count
        threading.Timer(0.4, self._clear_swallow).start()

    def _clear_swallow(self) -> None:
        with self._state:
            self._ignore_caps = 0

    def _reset_caps_lock(self, previous_on: bool) -> None:
        """Undo the Caps Lock toggle caused by holding the key to talk."""
        if _caps_is_on() == previous_on:
            return
        self._swallow_next_caps(2)
        ctypes.windll.user32.keybd_event(_VK_CAPITAL, 0, 0, 0)  # key down
        ctypes.windll.user32.keybd_event(_VK_CAPITAL, 0, 2, 0)  # key up

    # -- key handling --------------------------------------------------------
    def _is_caps_event(self, key) -> bool:  # noqa: ANN001
        return key == keyboard.Key.caps_lock

    def _consumed_injected(self, key) -> bool:  # noqa: ANN001
        """True if this event was our own injected Caps Lock tap."""
        if not self._is_caps_event(key):
            return False
        with self._state:
            if self._ignore_caps > 0:
                self._ignore_caps -= 1
                return True
        return False

    def _on_press(self, key) -> None:  # noqa: ANN001
        # Esc while a command is running = abort it. Esc at idle stays a
        # normal Esc so it never interferes with regular typing.
        # _working, not _busy: _busy stays held until the worker's finally,
        # which is *after* it has delivered its reply. Keying off _busy meant an
        # Esc in that window repainted "Cancelling…" over a finished reply, with
        # no worker left to clear it — the stuck-"thinking" symptom again.
        if key == keyboard.Key.esc and self._working.is_set():
            print("[Azleem] Esc pressed — cancelling the current task...")
            self._cancel_current()
            return
        if not _USE_CAPS and key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_down = True
            return
        if self._consumed_injected(key):
            return
        if not self._is_trigger(key):
            return
        # Windows repeats key-down while a key is held; only the first counts.
        with self._state:
            if self._held:
                return
            self._held = True
            self._caps_was_on = _caps_is_on()
            timer = threading.Timer(config.HOLD_SECONDS, self._begin_capture)
            timer.daemon = True
            self._hold_timer = timer
        timer.start()
        # Immediate feedback: if this line never appears, the keyboard hook
        # isn't seeing the key at all, which is a very different problem from
        # the mic or the HUD failing.
        print(f"[Azleem] hotkey down — keep holding {config.HOLD_SECONDS:g}s...", flush=True)

    def _begin_capture(self) -> None:
        """Fires once the hotkey has been held for HOLD_SECONDS."""
        with self._state:
            if not self._held or self._capturing:
                return  # released before the threshold
        # Open the mic FIRST so nothing the user says is missed, then mute the
        # speakers: whatever they are playing (a video, music) goes straight
        # into the microphone and would be transcribed as if the user had said
        # it. Muting costs a few ms, and losing that sliver of speaker bleed at
        # the very start of a take matters far less than delaying capture.
        try:
            self.recorder.start()
        except Exception as exc:
            print(f"[Azleem] could not open the microphone: {exc}")
            return
        speaker_mute.mute()  # restored on release and on every abort path
        # Only flag the capture once the recorder is actually running, and
        # re-check the key: a release that lands while the mic is opening would
        # otherwise stop() before start() and leave the recorder orphaned
        # (observed live as a 0.0s take followed by a stray "listening...").
        with self._state:
            if not self._held:
                released_meanwhile = True
            else:
                self._capturing = True
                released_meanwhile = False
        if released_meanwhile:
            self.recorder.stop()  # released at the threshold — abandon the take
            speaker_mute.restore()
            return
        self.hud.show()
        print("\n[Azleem] listening... (release to send)")

    def _on_release(self, key) -> None:  # noqa: ANN001
        if not _USE_CAPS and key in (keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
            self._ctrl_down = False
            # Releasing either half of the chord ends the hold; without this,
            # letting go of Ctrl before Space would leave the mic stuck open
            # (the Space release below no longer matches _is_trigger).
            if not self._held:
                return
        elif self._consumed_injected(key):
            return
        elif not self._ends_hold(key):
            return

        with self._state:
            if not self._held:
                return
            self._held = False
            was_capturing = self._capturing
            self._capturing = False
            if self._hold_timer is not None:
                self._hold_timer.cancel()
                self._hold_timer = None
            caps_was_on = self._caps_was_on

        if not was_capturing:
            # Too short to be a command — leave Caps Lock behaving normally.
            return

        audio = self.recorder.stop()
        # Restoring the speakers means COM calls and a retry sleep. This runs on
        # the pynput listener thread, where blocking also blocks Esc (the cancel
        # key), so hand it off — speaker_mute serialises itself internally.
        threading.Thread(target=speaker_mute.restore, daemon=True).start()
        if _USE_CAPS:
            self._reset_caps_lock(caps_was_on)

        # Claim the worker slot HERE, before showing "Thinking". Acquiring it
        # inside the worker instead meant a dropped overlapping command left
        # the HUD spinning forever with nothing left to answer it.
        if not self._busy.acquire(blocking=False):
            print("[Azleem] busy with a previous command; ignoring this one.")
            self.hud.show_reply("Still working on the last one.")
            return

        seconds = audio.size / config.SAMPLE_RATE if audio.size else 0.0
        peak = self.recorder.last_peak
        print(f"[Azleem] processing {seconds:.1f}s (peak level {peak:.3f})...")
        if peak < _SILENCE_PEAK:
            print(
                "[Azleem] that take was silent — check the mic isn't muted and "
                "that Windows' default input device is the one you're speaking into."
            )
            # Nothing to transcribe: skip a pointless Whisper decode, and keep
            # the specific mic advice instead of a generic "didn't catch that".
            self.hud.show_reply("That take was silent — check the mic or mute key.")
            self._busy.release()
            return

        self.hud.set_state("thinking")
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _is_trigger(self, key) -> bool:  # noqa: ANN001
        if _USE_CAPS:
            return key == keyboard.Key.caps_lock
        return key == keyboard.Key.space and self._ctrl_down

    def _ends_hold(self, key) -> bool:  # noqa: ANN001
        """Whether this key-up should terminate an armed hold.

        Unlike _is_trigger, a Space release counts even if Ctrl came up first:
        an armed hold can only have started from a valid Ctrl+Space press, and
        chord releases arrive in either order.
        """
        if _USE_CAPS:
            return key == keyboard.Key.caps_lock
        return key == keyboard.Key.space

    # -- command pipeline ----------------------------------------------------
    def _begin_worker(self) -> int:
        """Stamp this worker's generation and arm cancellation for the command.

        Enables Esc/corner cancel for the WHOLE command, transcription included —
        a slow Whisper decode used to ignore Esc entirely because _working wasn't
        set until after it. The corner watcher runs alongside.
        """
        with self._state:
            self._active_seq += 1
            self._local.seq = self._active_seq
            seq = self._active_seq
        cancellation.reset()
        self._working.set()
        self._start_corner_watch(seq)
        return seq

    def _answer(self, text: str) -> None:
        """Route text through the agent and deliver the reply, honouring cancel."""
        self.hud.set_state("thinking", f"“{text}”")
        try:
            reply = self.agent.handle(text)
        finally:
            # Cleared before the reply goes out, so an Esc arriving after this
            # point is treated as an idle Esc rather than repainting over a
            # reply nothing will clear.
            self._working.clear()

        print(f"[Azleem] {reply}")
        # If this worker was abandoned (Esc or a corner hit bumped the
        # generation), _cancel_current has already shown "Cancelled." — the late
        # result must not repaint over it. Same staleness guard _report_progress
        # uses, applied to the final reply.
        if self._is_stale():
            print("[Azleem] worker result discarded (command was cancelled).")
        elif cancellation.cancelled():
            self.hud.show_reply("Cancelled.")
        else:
            self.hud.show_reply(reply or "Done.")

    def _process(self, audio) -> None:  # noqa: ANN001
        # _busy is already held by _on_release, which claimed the slot before
        # the HUD was switched to "thinking"; this thread only has to release it.
        try:
            self._begin_worker()
            text = self.transcriber.transcribe(audio)
            if self._is_stale():
                print("[Azleem] cancelled during transcription; discarding.")
                return
            if not text:
                print("[Azleem] (heard nothing)")
                self.hud.show_reply("I didn't catch that.")
                return
            print(f"[you] {text}")
            self._answer(text)
        except Exception as exc:  # keep the loop alive no matter what
            print(f"[Azleem] error: {exc}")
            if not self._is_stale():
                self.hud.show_reply(f"Something went wrong: {exc}")
        finally:
            self._working.clear()   # belt and braces if we left early
            self._busy.release()

    def _process_text(self, text: str) -> None:
        """Worker for a command that arrived as text, not audio.

        Used for the request Azleem restarted to fulfil after add_capability —
        the transcription step is skipped, everything else matches _process.
        """
        try:
            self._begin_worker()
            print(f"[you] {text}")
            self._answer(text)
        except Exception as exc:
            print(f"[Azleem] error: {exc}")
            if not self._is_stale():
                self.hud.show_reply(f"Something went wrong: {exc}")
        finally:
            self._working.clear()
            self._busy.release()

    def dispatch_pending(self) -> None:
        """Run a command left behind by a self-extension restart, if any is fresh.

        add_capability writes the user's original request before it restarts, so
        the freshly installed tool can carry it out immediately. One shot: the
        pending file is consumed on read.
        """
        try:
            text = self_restart.take_pending()
        except Exception as exc:  # a bad pending file must never block startup
            print(f"[Azleem] could not read the pending command: {exc}")
            return
        if not text:
            return
        print(f"[Azleem] running the request I restarted for: {text!r}")
        if not self._busy.acquire(blocking=False):
            return  # something is already running; drop it rather than queue
        self.hud.show()
        threading.Thread(target=self._process_text, args=(text,), daemon=True).start()

    # -- cancellation --------------------------------------------------------
    def _is_stale(self) -> bool:
        """True if this worker's command has been abandoned (generation moved on).

        Runs on the worker thread, so its own generation lives in thread-local
        storage. _cancel_current bumps _active_seq, which is what makes a
        cancelled worker's remaining output stale.
        """
        return getattr(self._local, "seq", None) != self._active_seq

    def _cancel_current(self, reason: str = "Cancelled.") -> None:
        """Abort the in-flight command's UI at once and mark its worker stale.

        The worker thread can't be killed and its blocking call can't be
        interrupted, so this doesn't try. Instead it makes the *experience*
        instant: bump the generation so the worker's late progress and final
        reply are ignored, drop the terminal reply on the HUD now, and let the
        orphaned worker unwind on its own (the shortened request timeout and the
        cancel checks in gemini_client/quiz/computer_use free its _busy slot
        within a few seconds). Shared by the Esc key and the corner watcher.
        """
        if not self._working.is_set():
            return  # nothing genuinely in flight — treat as an idle gesture
        cancellation.request_cancel()
        with self._state:
            self._active_seq += 1   # every current worker is now stale
        self._working.clear()       # a second Esc is idle, not another repaint
        self.hud.show_reply(reason)

    def _start_corner_watch(self, seq: int) -> None:
        """Watch for the cursor hitting a screen corner, only while this command runs."""
        threading.Thread(
            target=self._watch_corners, args=(seq,), daemon=True
        ).start()

    def _watch_corners(self, seq: int) -> None:
        try:
            w, h = _screen_size()
        except Exception:
            return  # no usable metrics — Esc still cancels
        # Poll only while THIS command is the active one and still in flight, so
        # the watcher is free at idle and never blocks the listener thread.
        while self._active_seq == seq and self._working.is_set():
            try:
                x, y = _cursor_pos()
            except Exception:
                return
            if _in_corner(x, y, w, h):
                print("[Azleem] cursor hit a screen corner — cancelling.")
                self._cancel_current()
                return
            time.sleep(_CORNER_POLL_S)

    # -- lifecycle -----------------------------------------------------------
    def start_listener(self) -> keyboard.Listener:
        listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        listener.daemon = True
        listener.start()
        return listener

    def banner(self) -> None:
        label = "Caps Lock" if _USE_CAPS else "Ctrl+Space"
        hold = config.HOLD_SECONDS
        print("=" * 58)
        print("  AZLEEM — push-to-talk desktop assistant")
        print(f"  {build_info.describe()}")
        if hold <= 0.5:
            print(f"  Hold [{label}] — the mic opens immediately (HUD = live).")
            print("  Keep holding while you speak, then release to send.")
        else:
            print(f"  Hold [{label}] for {hold:g}s — the HUD means the mic is live.")
            print("  Keep holding while you speak, then release to send.")
            print(f"  A quick tap under {hold:g}s is ignored (Caps Lock still works).")
        print(f"  {providers.describe()}")
        print("  Speakers mute while listening, so it only hears you.")
        print("  Press Esc — or shove the cursor into a screen corner — to")
        print("  cancel a running task; the HUD slides aside if you near it.")
        if _HEADLESS:
            print("  Running in the background (auto-start). Quit via Task")
            print("  Manager: end the pythonw.exe running main.py.")
        else:
            print("  Press Ctrl+C in this window to quit.")
        print("=" * 58, flush=True)


def check_mic(seconds: float = 4.0) -> None:
    """Record for a few seconds and report exactly what the mic delivered.

    Run with ``python main.py --check-mic`` when nothing seems to be recording:
    it separates "the mic is dead" from "the mic is fine but Whisper didn't
    hear words", which look identical from the outside.
    """
    import numpy as np
    import sounddevice as sd

    try:
        dev = sd.query_devices(kind="input")
        print(f"[check] default input device: {dev['name']}")
    except Exception as exc:
        print(f"[check] no usable input device: {exc}")
        return

    levels: list[float] = []
    rec = Recorder(level_callback=levels.append)
    print(f"[check] recording {seconds:g}s — speak now...")
    try:
        rec.start()
    except Exception as exc:
        print(f"[check] could not open the microphone: {exc}")
        return
    import time as _time

    _time.sleep(seconds)
    audio = rec.stop()

    lv = np.array(levels) if levels else np.zeros(1)
    print(f"[check] blocks={len(levels)}  rms mean={lv.mean():.5f} max={lv.max():.5f}")
    print(f"[check] captured {audio.size / config.SAMPLE_RATE:.1f}s, peak={rec.last_peak:.4f}")
    if rec.last_peak < _SILENCE_PEAK:
        print("[check] VERDICT: silence. The mic is muted, or Windows' default")
        print("        input device isn't the one you're speaking into.")
        print("        Settings > System > Sound > Input, and check any mute key.")
        return
    print("[check] VERDICT: the mic is delivering audio.")
    print("[check] transcribing...")
    text = Transcriber().transcribe(audio)
    print(f"[check] heard: {text!r}" if text else "[check] heard: (no words recognised)")


_ERROR_ALREADY_EXISTS = 183

# Where the running Azleem records its own PID, so restart.ps1 never has to
# guess which process to stop. See _claim_pid_file.
_PID_FILE = Path(__file__).resolve().parent / "logs" / "azleem.pid"


def _claim_pid_file() -> None:
    """Say which process is Azleem, instead of leaving restart.ps1 to deduce it.

    ``restart.ps1`` used to identify us partly by elimination: Win32_Process
    exposes no working directory, so a bare ``pythonw.exe main.py`` that named
    no *other* directory was assumed to be ours. On this machine that is false
    — "my cluely" and the child it spawns are both launched bare — so a restart
    stopped two unrelated apps. Claiming a PID replaces the guess with a fact.

    Line 2 is the script we are actually running: it stops a pid file left by a
    different checkout of Azleem from being honoured here.
    """
    try:
        _PID_FILE.parent.mkdir(exist_ok=True)
        _PID_FILE.write_text(
            f"{os.getpid()}\n{Path(__file__).resolve()}\n", encoding="utf-8"
        )
    except OSError as exc:
        # A diagnostic must never be the reason the assistant fails to start;
        # restart.ps1 falls back to matching on this directory.
        print(f"[Azleem] could not write {_PID_FILE.name}: {exc}")


def _release_pid_file() -> None:
    """Drop our claim on exit — but never a newer instance's.

    A force-kill skips this, which is why restart.ps1 also checks the process
    is alive and predates the file rather than trusting the number alone.
    """
    try:
        if int(_PID_FILE.read_text(encoding="utf-8").split("\n")[0]) == os.getpid():
            _PID_FILE.unlink()
    except (OSError, ValueError):
        pass


def _acquire_single_instance() -> bool:
    """Named-mutex guard: with auto-start installed, a manual launch must not
    create a second keyboard hook (double recordings, duelling HUDs).

    The handle is deliberately never closed — Windows releases it when the
    process exits.
    """
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "AzleemAssistantSingleton")
        return ctypes.windll.kernel32.GetLastError() != _ERROR_ALREADY_EXISTS
    except (AttributeError, OSError):
        return True  # can't check — better to run than to refuse


def selftest() -> int:
    """Run the offline test suite. No network, no API key, nothing launched."""
    import unittest

    root = os.path.dirname(os.path.abspath(__file__))
    suite = unittest.TestLoader().discover(
        start_dir=os.path.join(root, "tests"), top_level_dir=root
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("\n[Azleem] self-test passed.")
        print("  Latency:    python tests/bench.py")
        print("  Live routing: set AZLEEM_LIVE_TESTS=1 first (spends API quota)")
        return 0
    print("\n[Azleem] self-test FAILED — see the failures above.")
    return 1


def main() -> None:
    if "--check-mic" in sys.argv:
        check_mic()
        return

    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    if not _acquire_single_instance():
        print("[Azleem] already running — this copy will exit.")
        return

    # Only after the singleton check: a duplicate launch must not overwrite the
    # real instance's claim with a PID that is about to exit.
    _claim_pid_file()
    atexit.register(_release_pid_file)

    hud = overlay_mod.create(config.SHOW_OVERLAY)
    jarvis = Jarvis(hud)

    def on_ready() -> None:
        jarvis.start_listener()
        jarvis.banner()
        # If this start was triggered by add_capability, run the request that
        # caused it, now that the new tool is loaded.
        jarvis.dispatch_pending()

    def shutdown(_sig=None, _frame=None) -> None:  # noqa: ANN001
        print("\n[Azleem] shutting down. Goodbye.")
        # Never leave the user's speakers muted because we quit mid-take.
        speaker_mute.restore()
        hud.stop()

    signal.signal(signal.SIGINT, shutdown)
    try:
        hud.run(on_ready)
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
