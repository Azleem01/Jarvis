"""Native Windows OS automation tools.

These functions are handed to Gemini as callable tools. Each returns a short
human-readable status string that the model can relay back to the user, and
each is defensive: they never raise on the happy-or-sad path, they return a
message describing what happened.

Docstrings and type hints matter here — the ``google-genai`` SDK reads them to
build the function declarations Gemini sees, so keep them accurate.

NOTE: no ``from __future__ import annotations`` here, deliberately. It turns
every type hint into a string, and google-genai's automatic function calling
does ``isinstance(value, annotation)`` when invoking a tool — with string
annotations every call dies with "isinstance() arg 2 must be a type" and the
tool never runs (some models then claim success anyway).
"""

import fnmatch
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import winreg
from pathlib import Path

import config


# ---------------------------------------------------------------------------
# File search
# ---------------------------------------------------------------------------
def search_and_open_file(filename: str, folder: str = "") -> str:
    """Search the user's folders for a file by (partial) name and open it.

    Args:
        filename: Full or partial file name to look for, e.g. "invoice" or
            "report.pdf". Matching is case-insensitive and substring-based.
        folder: Optional. Leave empty to search Downloads, Documents and
            Desktop; or give a folder name under the user's home (e.g.
            "Documents", "Pictures") or an absolute path to search just that.

    Returns:
        A status string describing what was opened or why nothing matched.
    """
    if folder.strip():
        bases = [_resolve_folder(folder)]
    else:
        bases = [config.DOWNLOADS_DIR, Path.home() / "Documents", Path.home() / "Desktop"]
    searched = [b for b in bases if b.exists()]
    if not searched:
        return f"Folder not found: {bases[0]}"

    needle = filename.strip().lower()
    pattern = f"*{needle}*"
    matches: list[Path] = []
    for base in searched:
        for root, _dirs, files in os.walk(base):
            for name in files:
                if fnmatch.fnmatch(name.lower(), pattern):
                    matches.append(Path(root) / name)
            if len(matches) > 50:  # plenty; stop walking huge trees
                break

    if not matches:
        where = ", ".join(str(b) for b in searched)
        return f"No file matching '{filename}' found in {where}."

    # Prefer the most recently modified match.
    best = max(matches, key=lambda p: p.stat().st_mtime)
    try:
        os.startfile(str(best))  # type: ignore[attr-defined]  # Windows-only
        return f"Opened '{best.name}' from {best.parent}."
    except OSError as exc:
        return f"Found '{best.name}' but could not open it: {exc}"


def _resolve_folder(folder: str) -> Path:
    if not folder or folder.strip().lower() in ("downloads", "download"):
        return config.DOWNLOADS_DIR
    candidate = Path(folder).expanduser()
    if candidate.is_absolute():
        return candidate
    return Path.home() / folder


# ---------------------------------------------------------------------------
# Application launching
# ---------------------------------------------------------------------------
# Map friendly spoken names -> a launch spec. Each value is tried with the
# Windows shell "start" command, which resolves apps on PATH, URIs, and
# registered App Paths (so "chrome", "notepad", etc. work without full paths).
_APP_ALIASES: dict[str, str] = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "browser": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "explorer": "explorer",
    "file explorer": "explorer",
    "files": "explorer",
    "cmd": "cmd",
    "terminal": "wt",  # Windows Terminal
    "settings": "ms-settings:",
    "whatsapp": "whatsapp://",
    "media player": "mswindowsmusic:",
    "windows media player": "wmplayer",
    "music": "mswindowsmusic:",
    "spotify": "spotify",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "paint": "mspaint",
}


# Non-http URI schemes we are willing to hand to the Windows shell. Anything
# else the model invents is refused: os.startfile on an arbitrary scheme is a
# shell hand-off, and the model's output is not a trusted source of URIs.
_SAFE_URI_SCHEMES = frozenset(
    {"whatsapp", "ms-settings", "mswindowsmusic", "ms-photos", "spotify", "mailto"}
)


def open_application(app_name: str) -> str:
    """Launch a desktop application by its common name.

    For websites use open_url instead — this is only for local applications.

    Args:
        app_name: Friendly name of the app, e.g. "chrome", "whatsapp",
            "notepad", "media player", "explorer".

    Returns:
        A status string describing whether the app was launched.
    """
    key = app_name.strip().lower()
    target = _APP_ALIASES.get(key, key)

    # A URL landed here instead of open_url — just do the right thing rather
    # than launching a browser to a blank page.
    if target.startswith(("http://", "https://", "www.")):
        return open_url(app_name)

    # A site name landed here instead of open_url. Same reasoning as the URL
    # case above, and the same failure it prevents: `start youtube` resolves
    # nothing. Checked after _APP_ALIASES so "whatsapp" still opens the desktop
    # app rather than web.whatsapp.com.
    if key not in _APP_ALIASES and key in _SITE_ALIASES:
        return open_url(key)

    # URI-style targets (whatsapp://, ms-settings:) open cleanly via os.startfile.
    if "://" in target or target.endswith(":"):
        scheme = target.split("://", 1)[0].rstrip(":").lower()
        if scheme not in _SAFE_URI_SCHEMES:
            return f"I don't know how to open '{app_name}'."
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return f"Launched {app_name}."
        except OSError as exc:
            return f"Could not launch {app_name}: {exc}"

    # This goes through cmd, which splits its command line on & | ^ < > and
    # would execute whatever follows. `target` can be an unaliased string
    # straight from the model, so anything that isn't a plain program name is
    # refused rather than quoted — no legitimate app name contains these.
    if any(ch in target for ch in '&|^<>"\n\r'):
        return f"I don't know how to open '{app_name}'."

    # Check the target resolves *before* spawning anything. Popen succeeding is
    # not the launch succeeding: `start` fails inside the child, asynchronously,
    # and with no console attached Windows reports it as a modal dialog we never
    # see. That is how "youtube on my chrome browser" produced a "Windows cannot
    # find" box while the HUD said "Launched". _resolve_executable does the same
    # PATH + App Paths lookup `start` does, so what it can't find, start can't
    # either — and we say so instead of claiming success.
    if not _launchable(target):
        return f"I don't know how to open '{app_name}'."

    # Use the shell "start" builtin so registered app names resolve.
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", target],
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return f"Launched {app_name}."
    except (OSError, subprocess.SubprocessError) as exc:
        return f"Could not launch {app_name}: {exc}"


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------
# Sites the user names out loud. Keys include the shapes local speech
# recognition actually produces ("you tube", "g mail"), because the transcript
# is what reaches the model, not what was said.
_SITE_ALIASES: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "you tube": "https://www.youtube.com",
    "utube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "g mail": "https://mail.google.com",
    "email": "https://mail.google.com",
    "github": "https://github.com",
    "git hub": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "chat gpt": "https://chatgpt.com",
    "claude": "https://claude.ai",
    "gemini": "https://gemini.google.com",
    "whatsapp web": "https://web.whatsapp.com",
    "maps": "https://www.google.com/maps",
    "google maps": "https://www.google.com/maps",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "linkedin": "https://www.linkedin.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "reddit": "https://www.reddit.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "wikipedia": "https://www.wikipedia.org",
    "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com",
    "spotify web": "https://open.spotify.com",
}

# Search endpoints keyed by site host, so "search YouTube for X" is one
# navigation instead of a click-and-type loop.
_SEARCH_URLS: dict[str, str] = {
    "www.youtube.com": "https://www.youtube.com/results?search_query={q}",
    "www.google.com": "https://www.google.com/search?q={q}",
    "github.com": "https://github.com/search?q={q}",
    "www.reddit.com": "https://www.reddit.com/search/?q={q}",
    "stackoverflow.com": "https://stackoverflow.com/search?q={q}",
    "www.amazon.com": "https://www.amazon.com/s?k={q}",
    "www.linkedin.com": "https://www.linkedin.com/search/results/all/?keywords={q}",
    "x.com": "https://x.com/search?q={q}",
    "www.wikipedia.org": "https://en.wikipedia.org/w/index.php?search={q}",
    "www.netflix.com": "https://www.netflix.com/search?q={q}",
}

# Browsers we can target by name -> their executable. Anything else falls back
# to the default browser rather than launching an unknown executable name.
_BROWSER_COMMANDS: dict[str, str] = {
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "crome": "chrome.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe",
    "brave": "brave.exe",
    "opera": "opera.exe",
}

_APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"


def _resolve_executable(exe_name: str) -> "str | None":
    """Find a browser's full path without going through the shell.

    Browsers are usually not on PATH; Windows registers them under App Paths,
    which is what `start chrome` consults. Resolving it ourselves means we can
    hand the URL to CreateProcess directly — see _launch_url for why that
    matters.
    """
    found = shutil.which(exe_name)
    if found:
        return found
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, f"{_APP_PATHS_KEY}\\{exe_name}") as key:
                # QueryValueEx, not QueryValue: the legacy call reads only
                # REG_SZ and raises "The data is invalid" on REG_EXPAND_SZ,
                # which is exactly how Windows registers its own apps
                # ("%ProgramFiles(x86)%\\Windows Media Player\\wmplayer.exe").
                # That made this return None for installed programs, so a
                # browser registered that way silently fell back to the default
                # one. expandvars finishes the job the registry left to us.
                path, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        path = os.path.expandvars(str(path or "")).strip().strip('"')
        if path and os.path.exists(path):
            return path
    return None


def _launchable(target: str) -> bool:
    """Would the shell's `start` find this program?

    Mirrors what `start` resolves: PATH (with PATHEXT, via shutil.which) and the
    registered App Paths keys, which is where browsers and Office live. App
    Paths keys are named with the extension ("chrome.exe", "winword.exe"), so a
    bare name is tried both ways.

    Used as a gate rather than a replacement for `start` — see open_application.
    """
    candidates = [target]
    if not target.lower().endswith(".exe"):
        candidates.append(f"{target}.exe")
    return any(_resolve_executable(name) for name in candidates)


def _resolve_site(name: str) -> str:
    """Turn a spoken site name or bare domain into an https URL."""
    raw = name.strip().strip("\"'")
    if not raw:
        return ""

    # An explicit URL is already exact — never rewrite it, or a deep link like
    # https://site.com/watch?v=… would collapse to the site's home page.
    if raw.startswith(("http://", "https://")):
        return raw

    # Some other scheme (file:, javascript:, ms-settings:) — hand it back
    # unchanged so the caller's http/https check refuses it. Prefixing
    # "https://" here would disguise it as a web address. A trailing :port is
    # not a scheme, so "localhost:3000" still resolves normally.
    scheme_match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(.*)$", raw, re.DOTALL)
    if scheme_match and not scheme_match.group(2).split("/")[0].isdigit():
        return raw

    key = raw.lower().rstrip("/")
    if key in _SITE_ALIASES:
        return _SITE_ALIASES[key]

    # "youtube.com" -> the canonical address from the map (with www, https)
    # rather than a guess. Only when there is no path: "youtube.com/watch?v=x"
    # must keep its path, so it cannot go through the alias map at all — that
    # would silently drop everything after the host and open the home page
    # while reporting success.
    host, slash, path = key.removeprefix("www.").partition("/")
    if not slash or not path:
        for suffix in (".com", ".org", ".net", ".io", ".ai", ".co"):
            if host.endswith(suffix):
                base = host[: -len(suffix)]
                if base in _SITE_ALIASES:
                    return _SITE_ALIASES[base]
                break

    return f"https://{raw.lstrip('/')}"


def open_url(url: str, browser: str = "", search_query: str = "") -> str:
    """Open a website, optionally running a search on it, in one step.

    This is the fast path for anything on the web — always prefer it over
    perform_computer_task, which drives the screen click by click. It goes
    straight to the address, so it is both quicker and exact.

    Args:
        url: The site to open. A full URL ("https://example.com/page"), a bare
            domain ("example.com"), or a common site name ("youtube", "gmail").
        browser: Optional. Which browser to use, e.g. "chrome", "edge",
            "firefox". Leave empty for the system default browser.
        search_query: Optional. When the user wants to search *on* that site,
            put the search terms here and they are turned into a direct results
            URL. Use the user's exact words — never substitute your own topic.

    Returns:
        A status string describing what was opened.
    """
    target = _resolve_site(url)
    if not target:
        return "No website given."

    parsed = urllib.parse.urlsplit(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"'{url}' is not a website I can open."

    query = search_query.strip()
    if query:
        # Match the search endpoint with and without "www.", so an explicit
        # https://youtube.com still reaches the real results page instead of
        # degrading to a scoped web search.
        host = parsed.netloc.lower()
        template = _SEARCH_URLS.get(host) or _SEARCH_URLS.get(f"www.{host}")
        if template is None and host.startswith("www."):
            template = _SEARCH_URLS.get(host[4:])
        if template:
            target = template.format(q=urllib.parse.quote_plus(query))
        else:
            # No site-specific endpoint — a web search scoped to that host still
            # beats clicking around the page.
            scoped = urllib.parse.quote_plus(f"site:{parsed.netloc} {query}")
            target = f"https://www.google.com/search?q={scoped}"

    browser_key = browser.strip().lower()
    if browser_key:
        exe_name = _BROWSER_COMMANDS.get(browser_key)
        exe = _resolve_executable(exe_name) if exe_name else None
        if exe:
            try:
                # Launch the browser directly — never via `cmd /c start`. cmd
                # splits its command line on & | ^ > <, and Python only quotes
                # arguments containing spaces, so a perfectly ordinary URL like
                # https://site.com/watch?v=abc&t=90 would lose everything from
                # "&" onward AND run the remainder as a shell command. The URL
                # here originates from the model, so that is a live injection,
                # not a theoretical one. CreateProcess takes argv directly and
                # has no metacharacters.
                subprocess.Popen([exe, target], shell=False)
                return f"Opened {target} in {browser_key}."
            except (OSError, subprocess.SubprocessError) as exc:
                return f"Could not open {target} in {browser_key}: {exc}"
        # Unknown or missing browser: still honour the request as best we can
        # rather than failing, but say which browser actually got used.
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return f"Opened {target} in the default browser ({browser} not found)."
        except OSError as exc:
            return f"Could not open {target}: {exc}"

    try:
        # os.startfile hands the string to ShellExecute, not to a command
        # interpreter, so metacharacters are inert on this path.
        os.startfile(target)  # type: ignore[attr-defined]
        return f"Opened {target}."
    except OSError as exc:
        return f"Could not open {target}: {exc}"


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------
def send_whatsapp_message(contact_name: str, message: str) -> str:
    """Send a WhatsApp message to a contact by name and auto-send it.

    Works even for people NOT saved in WHATSAPP_CONTACTS. If a phone number is
    known — from WHATSAPP_CONTACTS in .env, or a previous send Azleem cached —
    it uses the fast deep-link path (open the chat pre-filled, press Enter).
    Otherwise it opens WhatsApp, searches the app itself for the name, clicks the
    matching chat, types the message and sends it, then remembers the name.

    Args:
        contact_name: Name of the recipient, as the user said it.
        message: The text to send.

    Returns:
        A status string describing whether the message was sent.
    """
    # Imported lazily so importing this module never requires a display/GUI.
    import pyautogui

    name = contact_name.strip()
    # Resolve the spoken name to a real contact FIRST, so "mom"/"mum" and
    # relationship words ("daddy") land on the right person instead of a literal
    # search — and so an unclear match asks rather than messaging the wrong one.
    try:
        from tools import contacts
        kind, value = contacts.resolve_contact(name)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[os_tools] contact resolver unavailable: {exc}")
        kind, value = "none", []
    if kind == "ambiguous":
        options = ", ".join(value)
        return (
            f"I know more than one contact like '{name}': {options}. "
            f"Who did you mean?"
        )
    if kind == "match" and value.strip().lower() != name.strip().lower():
        # Switch to the saved spelling only when it is genuinely a different
        # name (mom -> Mum); keep the user's own casing otherwise, since .env
        # keys are stored lower-cased and "Sent to alex" reads worse than "Alex".
        name = value

    phone = config.WHATSAPP_CONTACTS.get(name.lower())
    if not phone:
        # A previously-reached contact may have a number cached. The cache also
        # owns the vision-driven search path used when we still have no number.
        try:
            from tools import whatsapp
        except Exception as exc:  # pragma: no cover - defensive
            return (
                f"Can't message {name}: no saved number and the search path is "
                f"unavailable ({exc})."
            )
        phone = whatsapp.cached_phone(name)
        if not phone:
            # No number anywhere — drive WhatsApp's own UI to find them by name.
            return whatsapp.send_via_ui(name, message)

    encoded = urllib.parse.quote(message)
    deep_link = f"whatsapp://send?phone={phone}&text={encoded}"
    try:
        os.startfile(deep_link)  # type: ignore[attr-defined]
    except OSError as exc:
        return f"Could not open WhatsApp for {name}: {exc}"

    # Wait for WhatsApp to focus the chat and populate the message box. Polling
    # for the window returns as soon as it is actually up, instead of always
    # paying a fixed worst-case wait.
    if not _wait_for_window("whatsapp", timeout=config.WHATSAPP_SEND_TIMEOUT):
        return (
            f"Opened WhatsApp for {name}, but its window never came to "
            "the front — the message is typed but not sent."
        )
    time.sleep(config.WHATSAPP_SETTLE_SECONDS)  # let the message box populate
    pyautogui.press("enter")
    return f"Sent WhatsApp message to {name}."


def _wait_for_window(title_substring: str, timeout: float = 8.0) -> bool:
    """Poll until a window whose title contains ``title_substring`` is active.

    The implementation lives in ``tools.windows``, which owns window matching
    for the whole package. Kept as a thin alias because this name is part of
    how ``send_whatsapp_message`` reads, and because the import is deferred —
    importing this module must never require a display.
    """
    from tools.windows import wait_for_window

    return wait_for_window(title_substring, timeout=timeout)
