"""Everyday productivity tools: screenshots, alarms, calendar events, notes.

Each is deliberately native-and-direct (no vision loop, no UI clicking) so
they're instant and can't misclick.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import datetime as _dt
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from tools import screen

_JARVIS_DIR = Path(__file__).resolve().parent.parent
_PYTHONW = Path(sys.executable).parent / "pythonw.exe"


def take_screenshot() -> str:
    """Take a screenshot of the screen and save it to Pictures.

    Deliberately does NOT pop the image open. These captures are usually taken
    as part of another task — and a real bug had the model calling this because
    an on-screen assignment said "include a screenshot", which then flung image
    windows in front of the user mid-task. Saving quietly and returning the path
    is all this should ever do; the model relays the path if asked.

    Returns:
        A status string with the saved file's path.
    """
    image, _ = screen.capture_screen()
    folder = Path.home() / "Pictures" / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"jarvis_{_dt.datetime.now():%Y%m%d_%H%M%S}.png"
    image.save(path, format="PNG")
    return f"Screenshot saved to {path}."


def set_alarm(time: str, message: str = "", date: str = "") -> str:
    """Set an alarm that pops up with a sound at the given time.

    Convert any relative phrasing ("in 20 minutes", "tomorrow morning") to a
    concrete time yourself before calling, using the current date/time you
    were given.

    Args:
        time: 24-hour clock "HH:MM", e.g. "07:30" or "18:45".
        message: What the alarm is for; shown in the popup.
        date: Optional "YYYY-MM-DD". Empty means today — or tomorrow if the
            time has already passed today.

    Returns:
        A status string confirming when the alarm will fire.
    """
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", time.strip())
    if not m:
        return f"Could not understand the time '{time}' — use 24-hour HH:MM."
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return f"'{time}' is not a valid time of day."

    now = _dt.datetime.now()
    if date.strip():
        try:
            day = _dt.date.fromisoformat(date.strip())
        except ValueError:
            return f"Could not understand the date '{date}' — use YYYY-MM-DD."
    else:
        day = now.date()
        if _dt.datetime.combine(day, _dt.time(hour, minute)) <= now:
            day += _dt.timedelta(days=1)  # "7:00" said at 9pm means tomorrow

    when = _dt.datetime.combine(day, _dt.time(hour, minute))
    if when <= now:
        return f"{when:%Y-%m-%d %H:%M} is in the past."

    label = message.strip() or "Alarm"
    task_name = f"Azleem_{when:%Y%m%d%H%M}_{uuid.uuid4().hex[:6]}"
    runner = _PYTHONW if _PYTHONW.exists() else Path(sys.executable)
    popup = _JARVIS_DIR / "alarm_popup.pyw"
    # PowerShell's Register-ScheduledTask takes an ISO datetime, unlike
    # schtasks /sd whose date format follows the system locale.
    safe_label = label.replace("'", "''").replace('"', "")
    ps = (
        "$a = New-ScheduledTaskAction -Execute '{exe}' "
        "-Argument '\"{popup}\" \"{label}\"'; "
        "$t = New-ScheduledTaskTrigger -Once -At '{when}'; "
        "Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Force"
    ).format(
        exe=str(runner).replace("'", "''"),
        popup=popup,
        label=safe_label,
        when=when.strftime("%Y-%m-%dT%H:%M:%S"),
        name=task_name,
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True,
            # Without a timeout a wedged PowerShell holds the worker thread
            # forever, and the HUD sits on "Thinking" with nothing coming.
            timeout=25,
        )
    except subprocess.TimeoutExpired:
        return "Could not schedule the alarm: the scheduler did not respond."
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:200]
        return f"Could not schedule the alarm: {detail}"
    return f"Alarm set for {when:%A %H:%M} — {label}."


def add_calendar_event(title: str, date: str, time: str, duration_minutes: int = 60) -> str:
    """Add an event to the user's calendar app with a date and time.

    Creates a standard calendar (.ics) event and opens it — the default
    calendar app shows it pre-filled for the user to save. Convert relative
    dates ("tomorrow", "next Friday") to concrete ones yourself first.

    Args:
        title: What the event is, e.g. "Lunch with Sam".
        date: "YYYY-MM-DD".
        time: 24-hour "HH:MM" start time.
        duration_minutes: Length of the event (default one hour).

    Returns:
        A status string describing the event that was created.
    """
    try:
        start = _dt.datetime.combine(
            _dt.date.fromisoformat(date.strip()),
            _dt.time.fromisoformat(time.strip()),
        )
    except ValueError:
        return f"Could not understand date '{date}' / time '{time}'."
    end = start + _dt.timedelta(minutes=max(1, int(duration_minutes)))

    def stamp(d: _dt.datetime) -> str:
        return d.strftime("%Y%m%dT%H%M%S")

    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Azleem//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uuid.uuid4()}@jarvis\r\n"
        f"DTSTAMP:{stamp(_dt.datetime.now())}\r\n"
        f"DTSTART:{stamp(start)}\r\n"
        f"DTEND:{stamp(end)}\r\n"
        f"SUMMARY:{title.strip()}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    folder = _JARVIS_DIR / "events"
    folder.mkdir(exist_ok=True)
    path = folder / f"event_{stamp(start)}.ics"
    path.write_text(ics, encoding="utf-8")
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except OSError as exc:
        return f"Event file created at {path} but no calendar app opened it: {exc}"
    return (
        f"Created '{title}' on {start:%A %Y-%m-%d} at {start:%H:%M} "
        f"({duration_minutes} min) — your calendar app is asking you to save it."
    )


def write_note(content: str, title: str = "") -> str:
    """Write a note to a text file and open it in Notepad.

    Args:
        content: The note text to save.
        title: Optional short title used for the file name.

    Returns:
        A status string with the note's path.
    """
    folder = Path.home() / "Documents" / "Azleem Notes"
    folder.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\- ]", "", title.strip())[:40].strip() or \
        f"note_{_dt.datetime.now():%Y%m%d_%H%M%S}"
    path = folder / f"{slug}.txt"
    if path.exists():  # don't clobber an earlier note with the same title
        path = folder / f"{slug}_{_dt.datetime.now():%H%M%S}.txt"
    path.write_text(content.strip() + "\n", encoding="utf-8")
    subprocess.Popen(["notepad", str(path)])
    return f"Note saved to {path} and opened in Notepad."
