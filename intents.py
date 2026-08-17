"""Local fast paths: commands answered without calling a model at all.

Routing a command through Gemini costs 1-3 seconds and one request against a
free-tier quota that allows 20 per day per model. But a large share of real
commands are completely unambiguous — "open notepad", "take a screenshot",
"what time is it". Matching those locally and dispatching straight to the tool
removes the model from the loop entirely: the whole command becomes
sub-millisecond, and it keeps working when the quota is gone.

The rule that makes this safe: **only ever match an unambiguous command, and
fall through to the model on any doubt.** A wrong instant answer is far worse
than a right slow one. Every pattern here is anchored to the whole utterance —
"open notepad" fast-paths, "open notepad and copy the text from my screen" does
not, because the trailing clause changes the job.

Whisper mangling is handled by matching leniently on the *verb* and letting
``open_application``'s own alias table sort out the noun, which it already does
("Nutspad" -> notepad). What is never guessed at is the *intent*.

NOTE: no ``from __future__ import annotations`` — this module is imported by
llm_agent alongside tools/, and the ban applies package-wide.
"""

import datetime
import re

# Filler that survives transcription but carries no meaning. Stripped before
# matching so "hey, could you open notepad please" still fast-paths.
_FILLER = re.compile(
    r"^(?:hey|ok|okay|so|um|uh|please|could you|can you|would you|jarvis|"
    r"azleem|azlim)\b[,\s]*", re.I)
_TRAILING = re.compile(r"\b(?:please|thanks|thank you|now|for me)\b[.!?,\s]*$", re.I)

# A second clause means the command is no longer a single obvious action, so it
# belongs to the model. "and"/"then" are the giveaways.
_MULTI_CLAUSE = re.compile(
    r"\b(?:and then|then|and also|after that|and)\s+\w+", re.I)


def _clean(text):
    """Strip politeness, filler and punctuation down to the bare command."""
    cleaned = (text or "").strip()
    for _ in range(3):  # "hey azleem, please ..." stacks
        new = _FILLER.sub("", cleaned, count=1)
        if new == cleaned:
            break
        cleaned = new
    cleaned = _TRAILING.sub("", cleaned)
    return cleaned.strip(" .!?,").strip()


# -- the table ---------------------------------------------------------------
# Each entry: (compiled pattern, handler). The handler receives the regex match
# and returns (tool_name, kwargs) — the tool itself is resolved by the caller so
# this module imports nothing from tools/ and stays trivially testable.

def _screenshot(_m):
    return "take_screenshot", {}


# The generic "read the whole screen" query. read_screen takes any query; a
# general description is what "what's on my screen" asks for.
_DESCRIBE_QUERY = "Everything currently visible on the screen, described briefly."


def _read_screen(_m):
    """Dispatch a bare 'what's on my screen' straight to read_screen.

    This is the one screen-touching fast path (see the module docstring's rule).
    It is safe *because the rule below is anchored to a closed set of exact
    phrasings* — the whole-screen read is the unambiguous intent of every one of
    them. Anything with a specific target ("what's the ANSWER on my screen",
    "read the THIRD LINE"), a second clause ("...and solve it"), or a pronoun
    fails the `^...$` anchor and falls through to the model, which is where a
    command that means more than "describe the screen" belongs.
    """
    return "read_screen", {"query": _DESCRIBE_QUERY}


# Words that survive as a bare "app name" but name nothing: "open up" must not
# launch an application called "up". A pronoun means the target was contextual,
# which the fast path has no context to resolve.
_NOT_A_TARGET = {
    "up", "it", "that", "this", "one", "them", "these", "those", "again",
    "something", "anything", "the", "a", "an", "my", "some",
}

# The app pattern's capture class contains a space, so it captures *phrases*,
# not just names — "open youtube on my chrome browser" arrived here whole and
# was handed to `cmd /c start` as if it were a program. A whole-capture check
# against _NOT_A_TARGET can't see that; the guard has to be word-level.
#
# No real application name contains a preposition or a pronoun. One of these
# words inside the capture means the utterance is a sentence, so it belongs to
# the model. Checked against every multi-word name in os_tools._APP_ALIASES
# ("windows media player", "file explorer", "google chrome", ...) — none match.
_NOT_IN_A_NAME = {
    "on", "in", "with", "using", "my", "the", "a", "an", "from", "to", "for",
    "into", "at", "and", "or", "that", "this", "it",
}
_WORDS = re.compile(r"[a-z]+")


def _open_app(m):
    app = m.group("app").strip()
    if app.lower() in _NOT_A_TARGET:
        return None
    if _NOT_IN_A_NAME.intersection(_WORDS.findall(app.lower())):
        return None
    return "open_application", {"app_name": app}


def _open_site(m):
    payload = {"url": m.group("site").strip()}
    # Only present on the site-plus-browser rule. Omitted rather than passed
    # empty so a bare "open youtube" still uses the default browser.
    browser = (m.groupdict().get("browser") or "").strip()
    if browser:
        payload["browser"] = browser
    return "open_url", payload


def _volume(action):
    """Fixed-verb volume commands: no capture, so nothing to guess at."""
    def handler(_m):
        return "control_volume", {"action": action}
    return handler


def _volume_level(m):
    level = int(m.group("level"))
    if not 0 <= level <= 100:
        return None  # "volume 900" is a mishearing, not a command
    return "control_volume", {"action": "set", "level": level}


def _battery(_m):
    return "system_status", {}


def _lock(_m):
    return "power_action", {"action": "lock"}


def _minimise_all(_m):
    return "arrange_window", {"action": "minimise_all"}


def _confirm_power(m):
    """The second half of the two-utterance power confirmation.

    Safe to fast-path precisely because it cannot act alone: power_action only
    proceeds when it already armed the same action within the last 20 seconds,
    so a stray "confirm shutdown" heard out of nowhere is refused by the tool.
    """
    # Whitespace-collapsed, not word-split: "shut down" is one action name and
    # taking the first word would arrive at the tool as "shut".
    action = " ".join(m.group("action").lower().split())
    return "power_action", {"action": action, "confirm": True}


def _time_now(_m):
    return None, datetime.datetime.now().strftime("It's %H:%M.")


def _date_today(_m):
    return None, datetime.datetime.now().strftime("It's %A %d %B %Y.")


# Sites that must go to open_url rather than open_application. Anything not
# listed falls through to the model rather than being guessed at.
_SITES = (
    "youtube|google|gmail|github|whatsapp web|maps|google maps|drive|"
    "google drive|calendar|google calendar|netflix|spotify|reddit|"
    "wikipedia|amazon|linkedin|twitter|chatgpt"
)

# Only the browsers os_tools._BROWSER_COMMANDS can actually launch. Naming one
# it can't would send the request to the default browser while the reply says
# otherwise, so the alternation is kept in step with that table by hand — this
# module imports nothing from tools/ on purpose. Longest names first so
# "google chrome" wins over "chrome".
_BROWSERS = (
    "google chrome|microsoft edge|chrome|crome|edge|firefox|brave|opera"
)

_RULES = [
    # Screenshots: no argument, no ambiguity.
    (re.compile(r"^(?:take|grab|capture|get)\s+(?:a\s+|the\s+)?"
                r"(?:screen\s*shot|screen\s*grab|screenie)$", re.I), _screenshot),
    (re.compile(r"^screen\s*shot$", re.I), _screenshot),

    # Reading the whole screen: "what's on my screen", "read my screen". A
    # closed set of exact phrasings, anchored end to end — the moment a specific
    # target, a second clause or a pronoun appears, the anchor fails and it
    # falls through to the model. Comes before the app rule so "read the screen"
    # can't be misread as launching an app called "screen".
    (re.compile(r"^(?:"
                r"what(?:'?s| is)?\s+(?:currently\s+)?on\s+(?:my\s+|the\s+)?screen"
                r"|read\s+(?:my|the)\s+screen"
                r"|what\s+does\s+(?:my|the)\s+screen\s+say"
                r"|describe\s+(?:my|the)\s+screen"
                r")$", re.I), _read_screen),

    # A known site *and* a named browser: "open youtube on my chrome browser".
    # Must come before the app rule, which would otherwise swallow the whole
    # phrase as an application name. Both halves are closed sets, so there is
    # nothing here to guess at.
    (re.compile(rf"^(?:open|launch|go\s+to|bring\s+up)\s+(?:up\s+)?"
                rf"(?P<site>{_SITES})\s+(?:on|in|with|using)\s+(?:my\s+)?"
                rf"(?P<browser>{_BROWSERS})(?:\s+browser)?$", re.I), _open_site),

    # Websites, listed explicitly so "open X" can't send an app to the browser.
    (re.compile(rf"^(?:open|launch|go\s+to|bring\s+up)\s+(?:up\s+)?"
                rf"(?P<site>{_SITES})$", re.I), _open_site),

    # Local applications. The noun is passed through untouched — the alias
    # table in os_tools already repairs mangled app names.
    (re.compile(r"^(?:open|launch|start|run|fire\s+up)\s+(?:up\s+)?"
                r"(?P<app>[\w .+-]{2,30})$", re.I), _open_app),

    # Machine control. Every rule below is either zero-argument or captures a
    # bare number — none of them take an open capture, which is the shape that
    # let a whole sentence through as an application name. "switch to X" and
    # "close X" are deliberately absent for exactly that reason: they belong to
    # the model, which can tell a window name from a sentence.
    (re.compile(r"^mute(?:\s+(?:the\s+)?(?:speakers?|sound|volume|audio))?$",
                re.I), _volume("mute")),
    (re.compile(r"^unmute(?:\s+(?:the\s+)?(?:speakers?|sound|volume|audio))?$",
                re.I), _volume("unmute")),
    (re.compile(r"^(?:turn\s+(?:it|the\s+(?:volume|sound|music))\s+up|"
                r"(?:volume|sound)\s+up|louder)$", re.I), _volume("up")),
    (re.compile(r"^(?:turn\s+(?:it|the\s+(?:volume|sound|music))\s+down|"
                r"(?:volume|sound)\s+down|quieter)$", re.I), _volume("down")),
    (re.compile(r"^(?:set\s+(?:the\s+)?volume\s+to|volume)\s+"
                r"(?P<level>\d{1,3})(?:\s*(?:%|percent))?$", re.I),
     _volume_level),

    (re.compile(r"^lock(?:\s+(?:the|my)\s+(?:screen|pc|computer|laptop|"
                r"workstation))?$", re.I), _lock),

    (re.compile(r"^(?:what(?:'s| is)?\s+(?:my|the)\s+battery(?:\s+(?:level|"
                r"percentage|at))?|how(?:'s| is)\s+(?:my|the)\s+battery|"
                r"battery(?:\s+(?:level|status))?)$", re.I), _battery),

    (re.compile(r"^(?:minimi[sz]e\s+(?:everything|all|all\s+(?:the\s+)?"
                r"windows)|show\s+(?:the\s+)?desktop)$", re.I), _minimise_all),

    # The confirming half of a power action. The tool refuses unless it armed
    # the same action moments ago, so matching this locally cannot cause one.
    (re.compile(r"^confirm\s+(?P<action>shut\s*down|restart|reboot|sleep)$",
                re.I), _confirm_power),

    # Answered from the clock; no tool and no model.
    (re.compile(r"^(?:what(?:'s| is)?\s+the\s+time|what\s+time\s+is\s+it|"
                r"tell\s+me\s+the\s+time|time)$", re.I), _time_now),
    (re.compile(r"^(?:what(?:'s| is)?\s+(?:the\s+)?(?:date|day)(?:\s+today)?|"
                r"what\s+day\s+is\s+it|today(?:'s)?\s+date)$", re.I), _date_today),
]


def match(text):
    """Resolve a command locally, or return None to let the model route it.

    Returns:
        ``None`` when the command is not an unambiguous match — the caller must
        fall through to Gemini.
        ``(tool_name, kwargs)`` to dispatch a tool directly.
        ``(None, reply)`` when the answer needs no tool at all.
    """
    command = _clean(text)
    if not command:
        return None

    for pattern, handler in _RULES:
        m = pattern.match(command)
        if not m:
            continue
        # A site/app name that itself contains a second clause is not a simple
        # command, whatever the verb looked like.
        captured = (m.groupdict().get("app") or m.groupdict().get("site") or "")
        if captured and _MULTI_CLAUSE.search(captured):
            return None
        # A handler returns None when the pattern matched but the capture turned
        # out to be meaningless ("open up") — fall through rather than guess.
        resolved = handler(m)
        if resolved is not None:
            return resolved
    return None
