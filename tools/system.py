"""System control: volume, brightness, battery, lock and power.

These functions are handed to Gemini as callable tools. Each returns a short
human-readable status string, and each is defensive: they never raise, they
return a message describing what happened.

Four tools rather than a dozen. Every tool in the registry is one more thing the
router can pick by mistake, so the verbs are grouped by argument shape instead
of one function per action.

Two things here are less obvious than they look:

**Volume and the take-mute.** ``speaker_mute`` silences the speakers while the
microphone is open, remembers the state it found, and restores it on release —
on a *daemon thread*, because blocking the pynput listener also blocks Esc. So
"mute" arrives at this module with a restore possibly still in flight, which
would undo the very thing the user just asked for. ``restore()`` is lock-guarded
and idempotent, so calling it synchronously before touching the device converges
in every interleaving: already finished, it is a no-op; not started, we do it;
mid-flight, we block on its lock and then no-op. Only then is the user's change
applied, on top of a known state.

**Power is armed, not fired.** A misheard "shut down" that is obeyed is a bad
day, so ``power_action`` for sleep/shutdown/restart returns an instruction
instead of acting. The second utterance is a whole new take, so a single
mis-transcription cannot chain into a shutdown.

NOTE: no ``from __future__ import annotations`` here, deliberately. It turns
every type hint into a string, and google-genai's automatic function calling
does ``isinstance(value, annotation)`` when invoking a tool — with string
annotations every call dies with "isinstance() arg 2 must be a type" and the
tool never runs (some models then claim success anyway).
"""

import ctypes
import subprocess
import time

import speaker_mute

# How long an armed power action stays valid. Long enough to say four words,
# short enough that an arm you walked away from cannot be triggered by a later
# unrelated command.
_CONFIRM_WINDOW_S = 20.0

# Single slot: (action, expires_at). Only one power action can ever be armed,
# and arming a second replaces the first rather than queueing behind it.
_armed = None

_VOLUME_STEP = 0.10

_SET = {"set", "level"}
_UP = {"up", "raise", "increase", "louder"}
_DOWN = {"down", "lower", "decrease", "quieter", "reduce"}
_MUTE = {"mute", "silence", "off"}
_UNMUTE = {"unmute", "unsilence", "on", "restore"}

_LOCK = {"lock"}
_SLEEP = {"sleep", "suspend"}
_SHUTDOWN = {"shutdown", "shut down", "power off", "turn off"}
_RESTART = {"restart", "reboot"}

# Powers actions that must be confirmed before they run. Lock is deliberately
# absent: it loses no work and is trivially undone by typing a password.
_NEEDS_CONFIRM = _SLEEP | _SHUTDOWN | _RESTART

_PS = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]
_PS_TIMEOUT_S = 8.0


def _norm(value):
    return " ".join((value or "").strip().lower().replace("-", " ").split())


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------
def control_volume(action: str, level: int = -1) -> str:
    """Change the speaker volume, or mute and unmute the speakers.

    Args:
        action: One of "set", "up", "down", "mute", "unmute". Use "set" together
            with level for an exact volume, and "up"/"down" to step it.
        level: Only used with "set". A percentage from 0 to 100.

    Returns:
        A status string describing the new volume.
    """
    verb = _norm(action)
    if not verb:
        return "Tell me what to do with the volume."

    # Settle any take-mute still in flight before touching the device, so its
    # restore cannot land on top of the change we are about to make.
    speaker_mute.restore()

    endpoint = speaker_mute.endpoint()
    if endpoint is None:
        return "I couldn't reach the speakers — no audio device is available."

    try:
        if verb in _MUTE:
            endpoint.SetMute(1, None)
            return "Speakers muted."
        if verb in _UNMUTE:
            endpoint.SetMute(0, None)
            return f"Speakers unmuted, volume {_read_percent(endpoint)}%."

        if verb in _SET:
            if not 0 <= level <= 100:
                return "Give me a volume between 0 and 100."
            wanted = level / 100.0
        elif verb in _UP:
            wanted = min(1.0, endpoint.GetMasterVolumeLevelScalar() + _VOLUME_STEP)
        elif verb in _DOWN:
            wanted = max(0.0, endpoint.GetMasterVolumeLevelScalar() - _VOLUME_STEP)
        else:
            return (
                f"I don't know how to '{action}' the volume — try set, up, "
                "down, mute or unmute."
            )

        endpoint.SetMasterVolumeLevelScalar(wanted, None)
        # Changing the volume on muted speakers produces silence at the new
        # level, which reads as a broken tool. Unmute so the change is audible.
        endpoint.SetMute(0, None)
        return f"Volume {_read_percent(endpoint)}%."
    except Exception as exc:
        speaker_mute.drop_cache()  # a dead endpoint must not stay cached
        return f"Could not change the volume: {exc}"


def _read_percent(endpoint) -> int:
    """The device's actual level, read back rather than assumed."""
    try:
        return int(round(endpoint.GetMasterVolumeLevelScalar() * 100))
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Brightness
# ---------------------------------------------------------------------------
def set_brightness(level: int) -> str:
    """Set the display brightness.

    Works on built-in laptop displays. Most external monitors do not accept
    software brightness control and will report that they could not be changed.

    Args:
        level: Brightness percentage from 0 to 100.

    Returns:
        A status string describing the new brightness.
    """
    if not 0 <= level <= 100:
        return "Give me a brightness between 0 and 100."

    # WmiSetBrightness is the only path that works without a dependency, and it
    # throws on panels that don't support it rather than silently ignoring the
    # call — which is what makes the failure detectable instead of a false
    # success. The timeout is not optional: a hung PowerShell would wedge the
    # worker thread and leave the HUD spinning.
    script = (
        "(Get-CimInstance -Namespace root/WMI "
        "-ClassName WmiMonitorBrightnessMethods)"
        f".WmiSetBrightness(0,{level})"
    )
    try:
        done = subprocess.run(
            _PS + [script],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return "The brightness control stopped responding."
    except Exception as exc:
        return f"Could not set the brightness: {exc}"

    if done.returncode != 0:
        # Verify before reporting: a process exiting is not the action working.
        return (
            f"This display doesn't accept software brightness control — "
            f"{level}% was not applied."
        )
    return f"Brightness set to {level}%."


def _brightness_percent():
    """Current brightness, or None when the display doesn't report it."""
    script = (
        "(Get-CimInstance -Namespace root/WMI "
        "-ClassName WmiMonitorBrightness).CurrentBrightness"
    )
    try:
        done = subprocess.run(
            _PS + [script],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if done.returncode != 0:
        return None
    text = (done.stdout or "").strip().splitlines()
    try:
        return int(text[0].strip())
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
class _PowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _battery():
    """(percent, plugged_in) from the OS, or (None, None) if unavailable.

    ``ctypes`` rather than psutil: this is the one field needed, and it isn't
    worth a dependency.
    """
    status = _PowerStatus()
    try:
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None, None
    except Exception:
        return None, None
    percent = int(status.BatteryLifePercent)
    if percent == 255:  # documented "unknown" sentinel — desktops report this
        return None, bool(status.ACLineStatus == 1)
    return percent, bool(status.ACLineStatus == 1)


def system_status() -> str:
    """Report battery level, power source, volume and brightness.

    Use this for questions about the machine's state — how much battery is
    left, whether it is charging, how loud the speakers are.

    Returns:
        A status string with whatever the machine could report.
    """
    parts = []

    percent, plugged = _battery()
    if percent is None:
        if plugged:
            parts.append("Running on mains power (no battery)")
    else:
        parts.append(
            f"Battery {percent}%" + (" and charging" if plugged else "")
        )

    endpoint = speaker_mute.endpoint()
    if endpoint is not None:
        try:
            if int(bool(endpoint.GetMute())):
                parts.append("speakers muted")
            else:
                parts.append(f"volume {_read_percent(endpoint)}%")
        except Exception:
            speaker_mute.drop_cache()

    brightness = _brightness_percent()
    if brightness is not None:
        parts.append(f"brightness {brightness}%")

    if not parts:
        return "I couldn't read the system status."
    return ", ".join(parts) + "."


# ---------------------------------------------------------------------------
# Power
# ---------------------------------------------------------------------------
def power_action(action: str, confirm: bool = False) -> str:
    """Lock the screen, or sleep, shut down or restart the machine.

    Locking happens immediately. Sleep, shutdown and restart are NOT performed
    on the first call: the tool arms the action and asks the user to confirm it
    out loud. Only call this with confirm set to true when the user has just
    said "confirm" together with the action.

    Args:
        action: One of "lock", "sleep", "shutdown", "restart".
        confirm: True only when the user is confirming a previously armed
            action. Never set this on a first request.

    Returns:
        A status string, or the confirmation the user needs to say.
    """
    global _armed

    verb = _norm(action)
    if verb in _LOCK:
        _armed = None
        return _lock_screen()

    if verb not in _NEEDS_CONFIRM:
        return (
            f"I don't know how to '{action}' — try lock, sleep, shutdown or "
            "restart."
        )

    if not confirm:
        _armed = (verb, time.monotonic() + _CONFIRM_WINDOW_S)
        return f"That will {verb} the machine. Say 'confirm {verb}' to go ahead."

    armed = _armed
    _armed = None  # single use, cleared whatever the outcome
    if armed is None:
        return f"Nothing was waiting to be confirmed. Ask me to {verb} first."
    armed_action, expires_at = armed
    if time.monotonic() > expires_at:
        return f"That {armed_action} request expired. Ask me again if you meant it."
    if armed_action != verb:
        return (
            f"I was waiting to confirm {armed_action}, not {verb}. "
            "Ask me again."
        )

    return _run_power(verb)


def _lock_screen() -> str:
    try:
        if ctypes.windll.user32.LockWorkStation():
            return "Screen locked."
    except Exception as exc:
        return f"Could not lock the screen: {exc}"
    return "Could not lock the screen."


def _run_power(verb: str) -> str:
    try:
        if verb in _SHUTDOWN:
            subprocess.Popen(["shutdown", "/s", "/t", "0"])
            return "Shutting down."
        if verb in _RESTART:
            subprocess.Popen(["shutdown", "/r", "/t", "0"])
            return "Restarting."
        if verb in _SLEEP:
            return _sleep()
    except Exception as exc:
        return f"Could not {verb}: {exc}"
    return f"Could not {verb}."


def _sleep() -> str:
    """Suspend the machine, reporting which of sleep/hibernate will happen.

    SetSuspendState's first argument asks for hibernate, but Windows overrides
    it: with hibernation enabled the machine hibernates whatever is passed. So
    the reply is based on what the OS is actually configured to do, not on what
    was requested — promising "sleep" and delivering hibernate is the kind of
    false success that erodes trust in every other reply.
    """
    hibernating = _hibernation_enabled()
    try:
        subprocess.Popen(
            ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        )
    except Exception as exc:
        return f"Could not sleep: {exc}"
    return "Hibernating." if hibernating else "Going to sleep."


def _hibernation_enabled() -> bool:
    try:
        done = subprocess.run(
            ["powercfg", "/a"],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    text = (done.stdout or "").lower()
    # "The following sleep states are available" lists what works; the machine
    # hibernates instead of sleeping only when Hibernate is among them.
    head = text.split("the following sleep states are not available")[0]
    return "hibernate" in head


def _reset_for_tests() -> None:
    """Clear the armed slot. Tests only — arming must not leak between cases."""
    global _armed
    _armed = None
