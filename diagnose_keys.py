"""Raw keyboard-event logger — run this, then hold Caps Lock.

Why this exists
---------------
Jarvis opens the mic only after the hotkey has been held for HOLD_SECONDS. That
depends on pynput reporting a *key-down*, then a *key-up* only when you actually
let go. Some Windows keyboards and drivers treat Caps Lock as a pure toggle and
emit key-down and key-up back-to-back on a single press, no matter how long you
hold it. If that happens here, the hold timer is cancelled instantly and the HUD
can never appear.

This script does no recording and no filtering: it prints every key event with a
timestamp, then summarises how long each Caps Lock press *looked* like it was
held. Run it, hold Caps Lock ~3 seconds, release, then press Esc.

    python diagnose_keys.py

Read the summary:
  * "held 3.0s"  -> events are fine; the hold gate should work.
  * "held 0.0s"  -> your keyboard reports Caps Lock as an instant toggle. The
                    hold-to-talk gate cannot work on Caps Lock; use
                    HOTKEY=ctrl_space in .env instead.
  * no release at all -> pynput never sees key-up for Caps Lock.
  * no events at all  -> the hook isn't receiving anything (permissions, or a
                    security tool blocking low-level keyboard hooks).
"""

from __future__ import annotations

import ctypes
import time

from pynput import keyboard

_VK_CAPITAL = 0x14

_events: list[tuple[float, str, str]] = []
_t0 = time.monotonic()


def _name(key) -> str:  # noqa: ANN001
    try:
        return key.name  # special keys (caps_lock, ctrl_l, ...)
    except AttributeError:
        return repr(getattr(key, "char", key))


def _caps_led() -> str:
    return "ON" if ctypes.windll.user32.GetKeyState(_VK_CAPITAL) & 1 else "off"


def _on_press(key) -> None:  # noqa: ANN001
    t = time.monotonic() - _t0
    _events.append((t, "DOWN", _name(key)))
    print(f"{t:7.3f}s  DOWN  {_name(key):12s} (caps LED {_caps_led()})", flush=True)


def _on_release(key) -> bool | None:  # noqa: ANN001
    t = time.monotonic() - _t0
    _events.append((t, "UP", _name(key)))
    print(f"{t:7.3f}s  UP    {_name(key):12s} (caps LED {_caps_led()})", flush=True)
    if key == keyboard.Key.esc:
        return False  # stop the listener
    return None


def _summarise() -> None:
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if not _events:
        print("No key events were received at all.")
        print("The low-level keyboard hook isn't working — check whether an")
        print("antivirus/EDR tool is blocking it, or try running as admin.")
        return

    caps = [(t, kind) for t, kind, name in _events if name == "caps_lock"]
    if not caps:
        print("No Caps Lock events seen. Did you press it?")
        print(f"(Other keys were seen: {len({n for _, _, n in _events})} distinct.)")
        return

    # Pair each DOWN with the next UP.
    pending: float | None = None
    holds: list[float] = []
    for t, kind in caps:
        if kind == "DOWN" and pending is None:
            pending = t
        elif kind == "UP" and pending is not None:
            holds.append(t - pending)
            pending = None

    if pending is not None:
        print("A Caps Lock DOWN was never followed by an UP.")
        print("pynput isn't reporting key-up for Caps Lock on this machine.")

    for i, h in enumerate(holds, 1):
        print(f"  press {i}: appeared held for {h:.3f}s")

    if holds:
        longest = max(holds)
        print()
        if longest < 0.3:
            print("VERDICT: Caps Lock reports as an INSTANT TOGGLE (down+up together).")
            print("A hold-to-talk gate cannot work on this key.")
            print("Fix: set HOTKEY=ctrl_space in .env and hold Ctrl+Space instead.")
        else:
            print(f"VERDICT: hold is reported correctly (longest {longest:.2f}s).")
            print("The hold gate should work; the problem is elsewhere.")


def main() -> None:
    print("Raw key logger. Hold CAPS LOCK for ~3 seconds, release, then press ESC.")
    print("-" * 60, flush=True)
    with keyboard.Listener(on_press=_on_press, on_release=_on_release) as listener:
        listener.join()
    _summarise()


if __name__ == "__main__":
    main()
