"""Identify which copy of the source a running Azleem actually loaded.

Python imports each module into memory once, at startup. Editing a .py file
does nothing to a process that is already running — and because Azleem holds a
single-instance mutex, launching a "fresh" copy to pick up a change silently
exits with "already running" and hands you back the stale one.

That combination cost a full round of debugging: fixes were verified on disk,
tested green, and the assistant kept reproducing the old behaviour because the
process predated them by three hours. Nothing in the logs said so.

So the banner and the log now carry a fingerprint of the source that was
actually loaded. Comparing it against ``python -m build_info`` answers "is the
running Azleem my latest code?" in one glance.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

# Directories that never affect runtime behaviour. tests/ is excluded on
# purpose: editing a test must not make a running assistant look stale.
_SKIP_DIRS = {"__pycache__", "tests", "logs", "events", ".git", ".venv"}


# .pyw counts: alarm_popup.pyw is real runtime code, launched by the scheduled
# task that productivity.set_alarm registers.
_SOURCE_GLOBS = ("*.py", "*.pyw")


def _source_files() -> list[Path]:
    """Every source file whose contents the running process depends on."""
    found = []
    for pattern in _SOURCE_GLOBS:
        for path in _ROOT.rglob(pattern):
            if any(part in _SKIP_DIRS for part in path.relative_to(_ROOT).parts):
                continue
            found.append(path)
    return sorted(found)


def fingerprint() -> tuple[str, datetime.datetime | None, int]:
    """Return ``(short_hash, newest_mtime, file_count)`` for the loaded source.

    The hash covers file contents *and* relative paths, so adding or removing a
    module changes it even if no existing file was edited.
    """
    digest = hashlib.sha256()
    newest = None
    files = _source_files()
    for path in files:
        digest.update(path.relative_to(_ROOT).as_posix().encode("utf-8"))
        try:
            digest.update(path.read_bytes())
            mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue  # a file vanishing mid-scan must not crash startup
        if newest is None or mtime > newest:
            newest = mtime
    return digest.hexdigest()[:7], newest, len(files)


def describe() -> str:
    """One-line build summary for the startup banner and the log."""
    try:
        short, newest, count = fingerprint()
    except Exception as exc:  # never let diagnostics break startup
        return f"build unknown ({type(exc).__name__})"
    when = f"{newest:%Y-%m-%d %H:%M:%S}" if newest else "unknown"
    return f"build {short} ({count} files, newest {when})"


if __name__ == "__main__":
    # `python -m build_info` prints the fingerprint of the source on disk.
    # Compare it with the newest banner in logs/azleem.log: if they differ, the
    # running process is stale and needs a restart (see restart.ps1).
    print(describe())
