"""Process-wide task cancellation, driven by the Esc key.

main.py sets the flag when Esc is pressed while a command is running; any
long-running tool (the computer-use loop, the coding tool) polls it between
steps and winds down. A plain module-level Event keeps this dependency-free —
tools must not import main.py.
"""

import threading

_cancel = threading.Event()


class Cancelled(RuntimeError):
    """Raised by a long-running call that noticed the cancel flag and bailed.

    Distinct from an error: the user asked for this, so callers should treat it
    as a clean stop (report "Cancelled." and record nothing), not a failure.
    """


def request_cancel() -> None:
    _cancel.set()


def cancelled() -> bool:
    return _cancel.is_set()


def reset() -> None:
    _cancel.clear()
