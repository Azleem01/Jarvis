"""Jarvis HUD — a quiet, dark glass panel in the Apple idiom.

The HUD is the face of the assistant, not just a mic light:

  * ``listening`` — live monochrome waveform driven by real mic RMS (a dead mic
    reads as a flat line), elapsed timer, release hint.
  * ``thinking``  — animated ellipsis while the command is transcribed/routed;
    a detail line streams the computer-use agent's current step.
  * ``reply``     — Jarvis's actual answer, wrapped, auto-dismissing. Without
    this the reply only ever reached a console the user never sees.

Threading contract (unchanged from v1)
--------------------------------------
Tk is not thread-safe and wants the main thread, so ``Overlay`` is constructed
and ``run()`` is called on the **main** thread; ``show()`` / ``hide()`` /
``set_level()`` / ``set_state()`` / ``show_reply()`` are safe from **any**
thread — they only post onto a queue drained by the Tk animation tick.

If Tk is unavailable, ``create()`` returns a no-op stand-in so Jarvis still
works headless.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time

# ---- Palette (graphite glass, single restrained accent) --------------------
_TRANSPARENT = "#010203"  # colour key punched out to fake rounded corners
_PANEL = "#161618"
_PANEL_EDGE = "#3c3c3e"
_TEXT = "#f5f5f7"
_TEXT_DIM = "#98989d"
_TEXT_FAINT = "#6e6e73"
_ACCENT = "#0a84ff"
_AMBER = "#ffd60a"

_BARS = 56
_W, _H = 520, 138

# A muted or absent input device delivers exact zeros, so "no input" only needs
# to catch RMS at the noise floor. Measured ambient room noise on this hardware
# sits around 0.008-0.02 RMS, so this is set well below that to avoid crying
# wolf on a quiet room or a quiet speaker.
_NO_INPUT_RMS = 0.0008
# Wait this long before complaining, so the verdict rests on enough blocks.
_NO_INPUT_AFTER = 2.0

# Reply display time: base + reading time, clamped.
_REPLY_MIN_S = 4.0
_REPLY_MAX_S = 12.0

# Watchdog: "thinking" is the one state nothing else times out, so a dropped
# command or a crashed worker used to leave the HUD spinning forever.
#
# Only perform_computer_task reports progress; the other tools set "thinking"
# once and go quiet, so this has to clear a genuinely slow command — a Gemini
# call walking the whole fallback chain with cooldowns can run well over a
# minute. Hence a generous window, and wording that does not claim the command
# failed: it may still be running, and its real reply will replace this.
_THINKING_STALL_S = 120.0
_STALL_MESSAGE = "Still working — no update for a while."

# HUD gets out of the way: if the cursor comes within this many px of the panel
# it hops to whichever vertical slot (top or bottom of the screen) is farther
# from the cursor, so it never sits under what the user is pointing at — and the
# corner-cancel gesture behind it stays reachable.
_AVOID_MARGIN = 60

# Animation tick cadence. The HUD only animates while it is showing, but the
# tick also has to keep draining its queue to notice the next show/reply. Doing
# that at 30 fps around the clock kept the CPU out of deep idle and cost real
# battery for nothing — the panel is hidden the vast majority of the time. So
# the tick runs fast only while visible and idles slowly while hidden; a queued
# show/reply is still picked up within one slow tick and snaps back to fast.
_ACTIVE_TICK_MS = 33   # ~30 fps while the HUD is on screen
_IDLE_TICK_MS = 150    # ~7 Hz while hidden — enough to stay responsive


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_xy():  # noqa: ANN202
    """Current mouse position in physical pixels, or None if it can't be read."""
    try:
        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except (AttributeError, OSError):
        return None


def _dpi_scale() -> float:
    """Physical-pixel scale factor.

    Jarvis marks itself DPI-aware (for accurate screenshots), so Tk reports
    physical pixels. Without this the HUD would render tiny at 125%/150%.
    """
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        return max(1.0, dpi / 96.0)
    except (AttributeError, OSError):
        return 1.0


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):  # noqa: ANN001, ANN003
    """Rounded rectangle via a smoothed polygon."""
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class _NullOverlay:
    """Stand-in used when Tk isn't available; every method is a no-op."""

    available = False

    def show(self) -> None: ...
    def hide(self) -> None: ...
    def set_level(self, level: float) -> None: ...
    def set_state(self, state: str, detail: str = "") -> None: ...
    def show_reply(self, text: str) -> None: ...
    def stop(self) -> None: ...

    def run(self, on_ready=None) -> None:  # noqa: ANN001
        """Block forever so the caller's shutdown logic still works."""
        if on_ready is not None:
            on_ready()
        threading.Event().wait()


class Overlay:
    """The HUD itself. Construct on the main thread, then call ``run()``."""

    available = True

    def __init__(self) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self._tk = tk
        self._q: queue.Queue[tuple] = queue.Queue()
        self._visible = False
        self._state = "listening"
        self._started_at = 0.0
        self._level = 0.0          # smoothed input level, 0..1
        self._peak = 0.0           # loudest level seen this take
        self._heights = [0.0] * _BARS
        self._phase = 0.0
        self._reply_until = 0.0
        self._state_since = 0.0    # when the current state was last refreshed
        self._detail = ""
        self._reply_text = ""
        self._stopping = False

        s = _dpi_scale()
        self._s = s
        self.w, self.h = int(_W * s), int(_H * s)

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-transparentcolor", _TRANSPARENT)
        except tk.TclError:
            pass  # No colour-key support: corners just render dark.
        try:
            self.root.attributes("-alpha", 0.96)
        except tk.TclError:
            pass
        self.root.configure(bg=_TRANSPARENT)

        # Segoe UI Variable is Windows 11's SF-Pro analogue; fall back cleanly.
        families = set(tkfont.families())
        self._family = next(
            (f for f in ("Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI")
             if f in families),
            "Segoe UI",
        )

        self.canvas = tk.Canvas(
            self.root, width=self.w, height=self.h,
            bg=_TRANSPARENT, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # Bottom-centre, clear of the taskbar. Two vertical slots exist so the
        # HUD can flee the cursor: its default bottom slot and a top slot.
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._x = (sw - self.w) // 2
        self._y_bottom = sh - self.h - int(110 * s)
        self._y_top = int(60 * s)
        self._y = self._y_bottom
        self._cur_y = self._y_bottom     # slot the panel currently occupies
        self.root.geometry(f"{self.w}x{self.h}+{self._x}+{self._y}")

        self._build()

    # -- window flags --------------------------------------------------------
    def _make_click_through(self) -> None:
        """Let clicks pass through and never take focus.

        Critical for a push-to-talk tool: the HUD must not steal focus from
        whatever the user is typing in, and must not intercept the clicks the
        computer-use agent performs.
        """
        GWL_EXSTYLE = -20
        WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x8000000
        try:
            hwnd = int(self.root.winfo_id())
            user32 = ctypes.windll.user32
            # The real top-level window is the parent of Tk's client HWND.
            parent = user32.GetParent(hwnd) or hwnd
            cur = user32.GetWindowLongW(parent, GWL_EXSTYLE)
            user32.SetWindowLongW(
                parent, GWL_EXSTYLE,
                cur | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE,
            )
        except (AttributeError, OSError, ValueError):
            pass

    def _font(self, size: float, weight: str = "normal"):  # noqa: ANN201
        return (self._family, int(size * self._s), weight)

    # -- static chrome -------------------------------------------------------
    def _build(self) -> None:
        s, w, h = self._s, self.w, self.h
        r = int(22 * s)

        # One quiet panel: hairline edge, solid graphite fill. No glow.
        _round_rect(self.canvas, 1, 1, w - 1, h - 1, r, fill=_PANEL_EDGE, outline="")
        _round_rect(
            self.canvas, 1 + int(s), 1 + int(s), w - 1 - int(s), h - 1 - int(s),
            r - int(s), fill=_PANEL, outline="",
        )

        # Header: status dot, wordmark, timer.
        d = int(7 * s)
        self._dot = self.canvas.create_oval(
            int(26 * s), int(24 * s), int(26 * s) + d, int(24 * s) + d,
            fill=_ACCENT, outline="",
        )
        self.canvas.create_text(
            int(40 * s), int(27 * s), text="Azleem", anchor="w",
            fill=_TEXT_DIM, font=self._font(10.5),
        )
        self._timer = self.canvas.create_text(
            w - int(26 * s), int(27 * s), text="", anchor="e",
            fill=_TEXT_FAINT, font=self._font(10),
        )

        # Centre stage: waveform (listening) OR status/reply text.
        self._mid = int(h * 0.55)
        self._bar_w = max(1, int(2 * s))
        span = w - int(52 * s)
        gap = span / _BARS
        self._bars = []
        for i in range(_BARS):
            x = int(26 * s + i * gap + gap / 2)
            self._bars.append(
                self.canvas.create_rectangle(
                    x, self._mid - int(s), x + self._bar_w, self._mid + int(s),
                    fill=_TEXT_FAINT, outline="",
                )
            )

        # Thinking ellipsis / reply text share the centre.
        self._centre = self.canvas.create_text(
            int(w / 2), self._mid, text="", anchor="center",
            fill=_TEXT, font=self._font(12),
            width=w - int(60 * s), justify="center",
        )

        # Caption line: hint, agent detail, or mic warning.
        cap_y = h - int(22 * s)
        self._caption = self.canvas.create_text(
            int(w / 2), cap_y, text="", anchor="center",
            fill=_TEXT_FAINT, font=self._font(9.5),
            width=w - int(60 * s),
        )

    # -- public, thread-safe -------------------------------------------------
    def show(self) -> None:
        self._q.put(("show",))

    def hide(self) -> None:
        self._q.put(("hide",))

    def set_level(self, level: float) -> None:
        """Feed the current input level (0..1) from the audio callback."""
        self._q.put(("level", float(level)))

    def set_state(self, state: str, detail: str = "") -> None:
        """Switch between 'listening' and 'thinking' (with optional detail)."""
        self._q.put(("state", state, detail))

    def show_reply(self, text: str) -> None:
        """Show Jarvis's reply, auto-hiding after a reading-time delay."""
        self._q.put(("reply", str(text)))

    def stop(self) -> None:
        self._q.put(("stop",))

    # -- main loop -----------------------------------------------------------
    def run(self, on_ready=None) -> None:  # noqa: ANN001
        self.root.after(10, self._tick)
        if on_ready is not None:
            self.root.after(60, on_ready)
        self.root.mainloop()

    def _drain(self) -> None:
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                return
            kind = msg[0]
            if kind == "level":
                # Rise fast, fall slow — reads like a real level meter.
                lv = msg[1]
                self._level = max(lv, self._level * 0.72)
                self._peak = max(self._peak, lv)
            elif kind == "show":
                self._state = "listening"
                self._started_at = time.monotonic()
                self._level = self._peak = 0.0
                self._apply_state()
                self._reset_position()  # each new take starts in the bottom slot
                if not self._visible:
                    self._visible = True
                    self.root.deiconify()
                    self.root.attributes("-topmost", True)
                    self._make_click_through()
            elif kind == "hide" and self._visible:
                self._visible = False
                self.root.withdraw()
            elif kind == "state":
                self._state = msg[1]
                self._detail = msg[2]
                # Every fresh update restarts the stall clock, so a long task
                # that is genuinely reporting progress is never cut off.
                self._state_since = time.monotonic()
                self._apply_state()
            elif kind == "reply":
                self._state = "reply"
                self._reply_text = msg[1]
                words = max(1, len(msg[1].split()))
                self._reply_until = time.monotonic() + min(
                    _REPLY_MAX_S, max(_REPLY_MIN_S, 1.5 + words * 0.35)
                )
                self._state_since = time.monotonic()
                self._apply_state()
                # A reply must be able to show itself. Without this, a reply
                # arriving while the panel is hidden was both invisible and
                # never expired — latching the HUD in "reply" forever.
                if not self._visible:
                    self._visible = True
                    self.root.deiconify()
                    self.root.attributes("-topmost", True)
                    self._make_click_through()
            elif kind == "stop":
                self._stopping = True
                self.root.quit()

    def _apply_state(self) -> None:
        """Reconfigure the static parts of the canvas for the current state."""
        state = self._state
        c = self.canvas
        if state == "listening":
            for bar in self._bars:
                c.itemconfigure(bar, state="normal")
            c.itemconfigure(self._centre, text="", font=self._font(12))
            c.itemconfigure(self._caption, text="Release to send", fill=_TEXT_FAINT)
            c.itemconfigure(self._dot, fill=_ACCENT)
            self._heights = [0.0] * _BARS
        elif state == "thinking":
            for bar in self._bars:
                c.itemconfigure(bar, state="hidden")
            c.itemconfigure(self._centre, text="", fill=_TEXT, font=self._font(12))
            detail = getattr(self, "_detail", "")
            c.itemconfigure(self._caption, text=detail, fill=_TEXT_DIM)
            c.itemconfigure(self._timer, text="")
            c.itemconfigure(self._dot, fill=_TEXT_FAINT)
        elif state == "reply":
            for bar in self._bars:
                c.itemconfigure(bar, state="hidden")
            c.itemconfigure(
                self._centre, text=self._fit_reply(self._reply_text),
                fill=_TEXT, font=self._font(11.5),
            )
            c.itemconfigure(self._caption, text="", fill=_TEXT_FAINT)
            c.itemconfigure(self._timer, text="")
            c.itemconfigure(self._dot, fill=_ACCENT)

    def _fit_reply(self, text: str) -> str:
        text = " ".join(text.split())  # collapse whitespace/newlines
        return text[:220] + "…" if len(text) > 220 else text

    # -- animation -----------------------------------------------------------
    def _tick(self) -> None:
        self._drain()
        if self._stopping:
            return
        if self._visible:
            self._avoid_cursor()
            self._animate()
        self.root.after(self._tick_delay_ms(), self._tick)

    def _tick_delay_ms(self) -> int:
        """Fast while the HUD shows, slow while it's hidden (battery)."""
        return _ACTIVE_TICK_MS if self._visible else _IDLE_TICK_MS

    # -- get out of the cursor's way -----------------------------------------
    def _reset_position(self) -> None:
        self._cur_y = self._y_bottom
        self.root.geometry(f"{self.w}x{self.h}+{self._x}+{self._cur_y}")

    def _avoid_cursor(self) -> None:
        """Hop to the slot farthest from the cursor if it's hovering the panel.

        The panel is click-through, so it can't be dragged out of the way — it
        moves itself. Once it hops, the cursor is no longer over it, so it
        settles; chase it and it flees again.
        """
        pos = _cursor_xy()
        if pos is None:
            return
        x, y = pos
        over = (
            self._x - _AVOID_MARGIN <= x <= self._x + self.w + _AVOID_MARGIN
            and self._cur_y - _AVOID_MARGIN <= y <= self._cur_y + self.h + _AVOID_MARGIN
        )
        if not over:
            return
        top_center = self._y_top + self.h / 2
        bottom_center = self._y_bottom + self.h / 2
        target = (
            self._y_top if abs(y - top_center) > abs(y - bottom_center)
            else self._y_bottom
        )
        if target != self._cur_y:
            self._cur_y = target
            self.root.geometry(f"{self.w}x{self.h}+{self._x}+{self._cur_y}")

    def _animate(self) -> None:
        import math

        s = self._s
        self._phase += 0.35
        state = self._state

        if state == "listening":
            elapsed = time.monotonic() - self._started_at
            # Soft breathing on the dot, not a hard blink.
            pulse = 0.5 + 0.5 * math.sin(self._phase * 0.55)
            self.canvas.itemconfigure(
                self._dot, fill=_ACCENT if pulse > 0.3 else "#0a5cc0"
            )
            self.canvas.itemconfigure(self._timer, text=f"{elapsed:4.1f}s")

            # Bars: travelling wave scaled by the live input level, plus a faint
            # idle shimmer so the HUD never looks frozen. Speech RMS mostly
            # lands in 0.02-0.15, so it needs real gain to fill the meter.
            lv = min(1.0, self._level * 6.5)
            max_h = self.h * 0.24
            for i, bar in enumerate(self._bars):
                env = 0.35 + 0.65 * math.sin(math.pi * (i + 0.5) / _BARS)
                wave = 0.45 + 0.55 * math.sin(self._phase - i * 0.33)
                idle = 0.025 * env * (0.6 + 0.4 * math.sin(self._phase * 0.6 - i * 0.3))
                target = lv * env * wave + idle
                self._heights[i] += (target - self._heights[i]) * 0.45
                hh = max(int(s), int(self._heights[i] * max_h))
                x = self.canvas.coords(bar)[0]
                self.canvas.coords(bar, x, self._mid - hh, x + self._bar_w, self._mid + hh)
                height = self._heights[i]
                colour = _TEXT if height > 0.38 else _TEXT_DIM if height > 0.12 else _TEXT_FAINT
                self.canvas.itemconfigure(bar, fill=colour)

            # Call out a silent mic rather than letting the user guess; clear
            # the warning again the moment real input arrives.
            if elapsed > _NO_INPUT_AFTER and self._peak < _NO_INPUT_RMS:
                self.canvas.itemconfigure(
                    self._caption, text="No input detected — check the mic or mute key",
                    fill=_AMBER,
                )
            else:
                self.canvas.itemconfigure(
                    self._caption, text="Release to send", fill=_TEXT_FAINT
                )
        elif state == "thinking":
            # Nothing else ever clears this state, so if progress has gone
            # quiet for too long, stop implying work is still in flight.
            if time.monotonic() - self._state_since > _THINKING_STALL_S:
                self.show_reply(_STALL_MESSAGE)
                return
            dots = "·" * (1 + int(self._phase * 0.9) % 3)
            self.canvas.itemconfigure(self._centre, text=f"Thinking {dots}")
        elif state == "reply":
            if time.monotonic() > self._reply_until:
                self._visible = False
                self.root.withdraw()


def create(enabled: bool = True):  # noqa: ANN201
    """Build an Overlay, or a no-op stand-in if disabled/unavailable."""
    if not enabled:
        return _NullOverlay()
    try:
        return Overlay()
    except Exception as exc:  # tkinter missing, no display, etc.
        print(f"[overlay] disabled ({exc}); running without the HUD.")
        return _NullOverlay()
