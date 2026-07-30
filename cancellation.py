"""Process-wide task cancellation, driven by the Esc key.

main.py sets the flag when Esc is pressed while a command is running; any
long-running tool (the computer-use loop, the coding tool) polls it between
steps and winds down. A plain module-level Event keeps this dependency-free —
tools must not import main.py.
"""

import threading

_cancel = threading.Event()


def request_cancel() -> None:
    _cancel.set()


def cancelled() -> bool:
    return _cancel.is_set()


def reset() -> None:
    _cancel.clear()
