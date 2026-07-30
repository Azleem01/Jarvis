"""Azleem alarm popup — launched by Windows Task Scheduler at alarm time.

Usage: pythonw alarm_popup.pyw "<message>"

Shows a topmost alert window and plays an alarm tone until dismissed (or for
at most two minutes). Deliberately dependency-free: only stdlib, so it fires
even when Jarvis itself isn't running.
"""

import sys
import threading
import time
import tkinter as tk

try:
    import winsound
except ImportError:  # non-Windows — silent popup only
    winsound = None

MESSAGE = sys.argv[1] if len(sys.argv) > 1 else "Alarm!"
MAX_RING_SECONDS = 120

_stop = threading.Event()


def ring() -> None:
    while not _stop.is_set() and time.monotonic() - t0 < MAX_RING_SECONDS:
        if winsound is not None:
            for freq in (880, 988, 880, 660):
                if _stop.is_set():
                    return
                winsound.Beep(freq, 180)
        time.sleep(1.2)


t0 = time.monotonic()

root = tk.Tk()
root.title("Azleem Alarm")
root.attributes("-topmost", True)
root.configure(bg="#161618")
root.geometry("+560+320")
root.resizable(False, False)

tk.Label(
    root, text="⏰  Azleem Alarm", fg="#0a84ff", bg="#161618",
    font=("Segoe UI", 14, "bold"), padx=30, pady=(18),
).pack()
tk.Label(
    root, text=MESSAGE, fg="#f5f5f7", bg="#161618",
    font=("Segoe UI", 12), padx=30, wraplength=380,
).pack()
tk.Button(
    root, text="Dismiss", command=root.destroy,
    font=("Segoe UI", 11), padx=24, pady=4,
).pack(pady=18)

threading.Thread(target=ring, daemon=True).start()
root.mainloop()
_stop.set()
