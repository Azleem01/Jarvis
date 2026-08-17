"""Restart Azleem in place, and carry one command across the restart.

Two jobs, both in service of ``add_capability`` (tools/self_extend.py):

**Spawning the restart.** ``restart.ps1``'s first act is to ``Stop-Process``
the running Azleem before it relaunches. So it MUST be launched detached: an
in-process ``subprocess.run`` would kill the very process making the call before
the relaunch line runs, and nothing would come back up. ``spawn_restart`` uses
``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` so the launcher outlives its
parent, and a brief ``Start-Sleep`` first lets the HUD paint its "restarting"
reply before it is torn down. The singleton-mutex wait that a naive self-restart
would deadlock on is already handled inside ``restart.ps1``.

**Carrying the command.** A just-installed capability is no use if the request
that triggered it is lost to the restart. The tool writes the user's original
task here; ``main.py`` reads it once on the next startup and runs it, so the new
tool is exercised immediately. A freshness window guards against a request left
behind by a restart the user has long since moved on from.
"""

import json
import subprocess
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_RESTART_PS1 = _ROOT / "restart.ps1"
_PENDING_FILE = _ROOT / "logs" / "pending_command.json"

# A command older than this is not re-run — a restart that took minutes, or one
# the user abandoned, must not surprise them by firing a stale request.
_PENDING_MAX_AGE_S = 120.0


def write_pending(text: str) -> None:
    """Record the command to re-run after the restart."""
    try:
        _PENDING_FILE.parent.mkdir(exist_ok=True)
        _PENDING_FILE.write_text(
            json.dumps({"text": text, "at": time.time()}), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[self_restart] could not write pending command: {exc}")


def take_pending():
    """Return a fresh pending command's text and consume it, else None.

    The file is deleted whether or not it is fresh, so a stale one is never
    retried on the *next* start either — one shot, by construction.
    """
    try:
        raw = _PENDING_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        _PENDING_FILE.unlink()
    except OSError:
        pass
    try:
        data = json.loads(raw)
        text = str(data.get("text", "")).strip()
        at = float(data.get("at", 0))
    except (ValueError, TypeError, AttributeError):
        return None
    if not text:
        return None
    if time.time() - at > _PENDING_MAX_AGE_S:
        print("[self_restart] pending command was stale; discarding.")
        return None
    return text


def spawn_restart(delay_s: float = 1.0) -> bool:
    """Launch restart.ps1 detached, after a short pause, so it survives us.

    Returns True if the launcher was spawned. It does NOT wait for the restart —
    by the time it would matter, this process has been stopped by the script.
    """
    flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    cmd = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
        f"Start-Sleep -Milliseconds {int(max(0.0, delay_s) * 1000)}; "
        f"& '{_RESTART_PS1}'",
    ]
    try:
        subprocess.Popen(
            cmd, cwd=str(_ROOT), creationflags=flags, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError as exc:
        print(f"[self_restart] could not spawn the restart: {exc}")
        return False
