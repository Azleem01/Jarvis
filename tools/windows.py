"""Window management: focus, snap, minimise, close.

These functions are handed to Gemini as callable tools. Each returns a short
human-readable status string, and each is defensive: they never raise, they
return a message describing what happened.

Why this exists as a tool rather than a screen loop: juggling windows through
``perform_computer_task`` costs a screenshot and a vision call per step, to do
something the window manager can do directly. "Switch to the editor" is one API
call, not a twelve-step agent.

This module owns the window *matching* helper (``find_windows``,
``wait_for_window``); ``os_tools`` imports them for its WhatsApp wait. The
dependency runs one way only — nothing here imports ``os_tools``.

NOTE: no ``from __future__ import annotations`` here, deliberately. It turns
every type hint into a string, and google-genai's automatic function calling
does ``isinstance(value, annotation)`` when invoking a tool — with string
annotations every call dies with "isinstance() arg 2 must be a type" and the
tool never runs (some models then claim success anyway).
"""

import time

# Actions arrange_window understands, and the aliases speech recognition
# realistically produces for each. Both spellings of "minimise" are here
# because the model will produce either depending on how the user said it.
_SNAP_LEFT = {"left", "snap left", "snap_left"}
_SNAP_RIGHT = {"right", "snap right", "snap_right"}
_MAXIMISE = {"maximise", "maximize", "full screen", "fullscreen"}
_MINIMISE = {"minimise", "minimize", "hide"}
_MINIMISE_ALL = {
    "minimise_all", "minimize_all", "minimise all", "minimize all",
    "show desktop", "minimise everything", "minimize everything",
}
_CLOSE = {"close", "quit", "exit"}


def _norm(action):
    return " ".join((action or "").strip().lower().replace("-", " ").split())


# ---------------------------------------------------------------------------
# Matching helpers (shared with os_tools)
# ---------------------------------------------------------------------------
def find_windows(title_substring: str):
    """Visible windows whose title contains ``title_substring`` (case-insensitive).

    Returns a list, most recently active first where the platform reports it.
    Windows with an empty title are skipped — they are almost always invisible
    helper windows, and matching one would make a "close" land on nothing the
    user can see.
    """
    import pygetwindow

    needle = (title_substring or "").strip().lower()
    if not needle:
        return []
    try:
        candidates = pygetwindow.getAllWindows()
    except Exception:
        return []

    matches = []
    for window in candidates:
        try:
            title = (window.title or "").strip()
        except Exception:
            continue  # pygetwindow raises on transient window states
        if title and needle in title.lower():
            matches.append(window)
    return matches


def wait_for_window(title_substring: str, timeout: float = 8.0) -> bool:
    """Poll until a window whose title contains ``title_substring`` is active."""
    import pygetwindow

    needle = (title_substring or "").strip().lower()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            active = pygetwindow.getActiveWindow()
            if active is not None and needle in (active.title or "").lower():
                return True
        except Exception:
            # pygetwindow raises on some transient window states; keep polling.
            pass
        time.sleep(0.15)
    return False


def _active_window():
    import pygetwindow

    try:
        return pygetwindow.getActiveWindow()
    except Exception:
        return None


def _title_of(window) -> str:
    try:
        return (window.title or "").strip()
    except Exception:
        return ""


def _activate(window) -> bool:
    """Bring a window to the front, returning whether it actually came forward.

    ``pygetwindow.activate()`` raises on Windows more often than not: the OS
    refuses SetForegroundWindow from a process that does not already own the
    foreground. The minimise/restore nudge is the standard workaround — it gives
    our thread the foreground right the API is withholding.
    """
    for attempt in (0, 1):
        try:
            window.activate()
        except Exception:
            try:
                if attempt == 0:
                    window.minimize()
                window.restore()
            except Exception:
                pass
        time.sleep(0.12)
        active = _active_window()
        if active is not None and _title_of(active) == _title_of(window):
            return True
    return False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def focus_window(name: str) -> str:
    """Bring an already-open window to the front by (partial) title.

    Use this to switch to a program the user already has running. It does not
    launch anything — if nothing matches, say so rather than guessing.

    Args:
        name: Part of the window title or program name, e.g. "editor" or
            "browser". Matching is case-insensitive and substring-based.

    Returns:
        A status string naming the window that was brought forward.
    """
    target = (name or "").strip()
    if not target:
        return "Tell me which window to switch to."

    matches = find_windows(target)
    if not matches:
        return f"No open window matching '{target}'."

    window = matches[0]
    title = _title_of(window) or target
    if _activate(window):
        return f"Switched to {title}."
    return (
        f"Found the window '{title}' but Windows would not bring it to the "
        "front — click it once and try again."
    )


def arrange_window(action: str, target: str = "") -> str:
    """Snap, maximise, minimise or close a window.

    Args:
        action: One of "left", "right", "maximise", "minimise", "minimise_all"
            or "close". Use "left"/"right" to snap a window to that half of the
            screen.
        target: Optional. Part of the window title to act on. Leave empty to act
            on the window currently in front, which is what phrases like "this
            window" mean.

    Returns:
        A status string naming the window that was acted on.
    """
    verb = _norm(action)
    if not verb:
        return "Tell me what to do with the window."

    if verb in _MINIMISE_ALL:
        return _minimise_all()

    wanted = (target or "").strip()
    if wanted:
        matches = find_windows(wanted)
        if not matches:
            return f"No open window matching '{wanted}'."
        window = matches[0]
    else:
        window = _active_window()
        if window is None or not _title_of(window):
            return "I couldn't tell which window is in front."

    title = _title_of(window) or wanted or "that window"

    try:
        if verb in _SNAP_LEFT:
            return _snap(window, title, "left")
        if verb in _SNAP_RIGHT:
            return _snap(window, title, "right")
        if verb in _MAXIMISE:
            window.maximize()
            return f"Maximised {title}."
        if verb in _MINIMISE:
            window.minimize()
            return f"Minimised {title}."
        if verb in _CLOSE:
            # Named explicitly in the reply: closing can lose unsaved work, so
            # the user has to be able to see what went, not just that something
            # did. A mis-transcribed name must never close "whatever was there".
            window.close()
            return f"Closed {title}."
    except Exception as exc:
        return f"Could not {verb} {title}: {exc}"

    return (
        f"I don't know how to '{action}' a window — try left, right, maximise, "
        "minimise or close."
    )


def _snap(window, title: str, side: str) -> str:
    """Snap a window to one half of the screen with the OS shortcut.

    Uses Win+Left/Right rather than setting geometry directly: it produces the
    real snap layout the user expects (including the size the OS picks for that
    monitor) instead of a window that merely happens to cover half the screen.
    """
    import pyautogui

    if not _activate(window):
        return (
            f"Found '{title}' but Windows would not bring it to the front, so "
            "there was nothing to snap."
        )
    pyautogui.hotkey("win", side)
    time.sleep(0.25)  # let the snap animation commit before reporting
    return f"Snapped {title} to the {side}."


def _minimise_all() -> str:
    """Show the desktop. Win+D rather than iterating windows, so it is one step."""
    import pyautogui

    try:
        pyautogui.hotkey("win", "d")
    except Exception as exc:
        return f"Could not minimise everything: {exc}"
    return "Minimised everything."
