"""Live progress reporting from a long-running tool into the HUD.

``perform_computer_task`` and ``answer_quiz`` both run for tens of seconds and
both need to stream what they are doing, so the callback lives here rather than
as module state inside one of them. ``main.py`` registers it once.

NOTE: no ``from __future__ import annotations`` — this sits in tools/ alongside
modules the SDK inspects, and the import is banned package-wide (see
tools/os_tools.py).
"""

_callback = None


def set_progress_callback(callback) -> None:
    """Register a ``fn(detail: str)`` invoked before each action."""
    global _callback
    _callback = callback


def progress(detail: str, prefix: str = "agent") -> None:
    """Print a step and mirror it to the HUD.

    A HUD hiccup must never kill the task that is reporting, so the callback is
    wrapped — the console line has already been written by then either way.
    """
    print(f"[{prefix}] {detail}")
    if _callback is not None:
        try:
            _callback(detail)
        except Exception:
            pass
