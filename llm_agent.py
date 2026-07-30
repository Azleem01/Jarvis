"""Gemini intent-routing agent.

Wires the OS and vision tools into Gemini's automatic function calling. Given a
transcribed command, Gemini decides which tool(s) to call, the SDK invokes them
directly (they are plain Python callables with typed signatures + docstrings),
and we return the model's final natural-language reply.

Requests go through ``gemini_client``, which falls back across a chain of models
when one is rate limited or retired. One wrinkle that fallback creates here:
these tools *do things* — launch apps, send messages, click the screen. If the
API fails on the follow-up turn, after a tool has already run, retrying the same
command on another model would perform the action twice. So each tool call is
tracked, and once anything has executed the fallback stops and we report what
actually happened instead of silently repeating it.
"""

from __future__ import annotations

import datetime
import functools
import time
from collections import deque
from typing import Callable

from google.genai import types

import config
import gemini_client
import intents
from tools.coding import solve_with_python
from tools.computer_use import perform_computer_task
from tools.os_tools import (
    open_application,
    open_url,
    search_and_open_file,
    send_whatsapp_message,
)
from tools.productivity import (
    add_calendar_event,
    set_alarm,
    take_screenshot,
    write_note,
)
from tools.quiz import answer_quiz
from tools.system import (
    control_volume,
    power_action,
    set_brightness,
    system_status,
)
from tools.vision_tools import execute_screen_task, read_screen
from tools.windows import arrange_window, focus_window

_SYSTEM_PROMPT = (
    "You are Azleem, a concise Windows desktop assistant. The user speaks a "
    "command that has been transcribed to text by local speech recognition, "
    "which often mangles proper nouns — 'Open Nutspad' or 'Open Note 5' means "
    "'open Notepad', 'crome' means 'Chrome'. Infer the plainly intended app, "
    "file, or action instead of taking a garbled name literally; only ask for "
    "clarification when the intent is genuinely ambiguous.\n"
    "Pick the cheapest tool that does the job — always prefer a dedicated "
    "tool over perform_computer_task when one matches:\n"
    "- open_application: just launching a local app ('open Notepad').\n"
    "- open_url: ANY website — opening a site, going to a page, or searching "
    "on a site. Put the user's own words in search_query and pass the browser "
    "they named. This is one fast step; never use perform_computer_task to "
    "reach a website.\n"
    "- search_and_open_file: finding/opening a file by name.\n"
    "- send_whatsapp_message: messaging a known contact.\n"
    "- take_screenshot: capture the screen to a file.\n"
    "- set_alarm: alarms and timed reminders. Convert relative times ('in 20 "
    "minutes') to HH:MM using the current time given below.\n"
    "- add_calendar_event: calendar entries with a date and time.\n"
    "- write_note: jotting notes/text the user dictates into Notepad.\n"
    "- control_volume: speaker volume and mute ('turn it down', 'mute').\n"
    "- set_brightness: screen brightness.\n"
    "- system_status: battery, charge state, current volume and brightness.\n"
    "- power_action: lock, sleep, shut down or restart the machine. Call it "
    "WITHOUT confirm for any first request — it arms the action and asks the "
    "user to confirm out loud. Only pass confirm=true when the user has just "
    "said 'confirm' with the action; never confirm on their behalf.\n"
    "- focus_window: switch to a program that is already running.\n"
    "- arrange_window: snap left/right, maximise, minimise or close a window. "
    "An empty target means the window in front, which is what 'this window' "
    "means.\n"
    "- solve_with_python: computational or programmatic tasks — maths, data "
    "generation, text processing ('compute the first 100 primes'). Returns a "
    "shareable solution link when available.\n"
    "- read_screen: read text off the screen (an exercise statement, a "
    "question, an error) so you can pass it to another tool.\n"
    "- answer_quiz: ANY multiple-choice quiz, test or question set on screen — "
    "one question or a whole quiz. It reads each question, answers it, clicks "
    "the choice and moves on by itself. Never use perform_computer_task for a "
    "quiz, and never read_screen first; pass the user's own words as scope.\n"
    "- execute_screen_task: one single obvious click on screen.\n"
    "- perform_computer_task: only for goals needing several steps on what is "
    "already on screen — navigating menus, filling in a form. Not for websites: "
    "'open YouTube in Chrome' is a single open_url call, not a screen loop.\n"
    "Questions, quizzes and exercises already visible on the user's screen: "
    "answer them from your OWN knowledge. Never open a browser or run a web "
    "search to look one up — that navigates away from the very screen the task "
    "is about, and every step after it then acts on the wrong window.\n"
    "A command with more than one clause ('answer it AND move to the next "
    "one') goes to ONE perform_computer_task call with BOTH clauses written "
    "into the task string. Never drop the second half, and do not spend a "
    "read_screen first — perform_computer_task takes its own screenshot before "
    "every step.\n"
    "Do exactly what was asked and nothing more. Never substitute a specific "
    "site, channel, creator, product or search term the user did not say — "
    "'open YouTube' means the YouTube home page, not a channel or a video. "
    "When a request is under-specified, take the plain default action and stop; "
    "do not go looking for extra work to do.\n"
    "Chained example — 'solve the coding exercise on my screen and submit the "
    "link': (1) read_screen for the full exercise statement, (2) "
    "solve_with_python with that statement to get the solution link (when a "
    "Colab link is offered, submit that one — course sites asking for a "
    "'shared Colab notebook' accept it), (3) you MUST then call "
    "perform_computer_task yourself to type that link into the page's "
    "submission field (scrolling to it if needed) — never tell the user to "
    "paste it themselves. The final Submit button is never clicked — after "
    "the link is typed, tell the user it is ready for them to review and "
    "submit.\n"
    "Earlier turns in this conversation are there to resolve follow-ups — what "
    "'it', 'that one' or 'the same again' refers to. They are context, not a "
    "list of work: a tool that ran in an earlier turn has already happened and "
    "is never repeated unless the user asks for it again in the newest "
    "message. Always act on the newest message only.\n"
    "Call a tool when the request maps to one; otherwise answer briefly in one "
    "or two sentences. After a tool runs, relay its actual outcome plainly — "
    "including partial progress or failure. Never invent file names, contacts, "
    "or results, and never claim success a tool did not report."
)

_TOOLS: list[Callable[..., str]] = [
    search_and_open_file,
    open_application,
    open_url,
    send_whatsapp_message,
    execute_screen_task,
    read_screen,
    answer_quiz,
    perform_computer_task,
    take_screenshot,
    set_alarm,
    add_calendar_event,
    write_note,
    solve_with_python,
    control_volume,
    set_brightness,
    system_status,
    power_action,
    focus_window,
    arrange_window,
]


# Tools that only observe — running them again on another model is harmless,
# so they must not stop the rate-limit fallback the way side-effect tools do.
_READ_ONLY_TOOLS = {"read_screen", "take_screenshot"}

# Reply budget. overlay._fit_reply cuts the HUD text at 220 characters, so
# anything past that is invisible to the user — and what gets pushed out is the
# tail, which is exactly where the outcome lives. Observed live: a quiz command
# replied with two read_screen dumps (~700 chars of a search-results page)
# followed by what Azleem had actually done, and only the dumps fitted.
_PER_RESULT_CHARS = 100
_SUMMARY_CHARS = 200


def _tracked(fn: Callable[..., str], log: list[list]) -> Callable[..., str]:
    """Wrap a tool so we know whether it ran, and what it reported.

    ``functools.wraps`` keeps the name, docstring and annotations intact, which
    is what the SDK reads to build the function declaration Gemini sees.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = fn(*args, **kwargs)
        log.append([fn.__name__, str(result)])
        return result

    return wrapper


def _only_reads(performed: list) -> bool:
    return all(name in _READ_ONLY_TOOLS for name, _ in performed)


def _clip(text: str, limit: int) -> str:
    """One tool result, whitespace-collapsed and cut to ``limit`` characters."""
    text = " ".join(str(text).split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _summarise(performed: list) -> str:
    """What actually happened, short enough for the HUD to show all of it.

    Reading the screen is *input*, not an outcome: once a tool has genuinely
    done something, a verbatim dump of what was on screen only crowds out the
    part the user needs. So read-only results are dropped whenever there is
    anything else to report, and kept when they are all there is — "what does
    my screen say?" still has to answer.

    The full, untrimmed log goes to stdout (and so to logs/azleem.log); only
    the user-facing string is budgeted.
    """
    if not performed:
        return ""
    for name, result in performed:
        print(f"[llm] {name} -> {result}")

    reportable = [(n, r) for n, r in performed if n not in _READ_ONLY_TOOLS]
    if not reportable:
        reportable = list(performed)

    joined = " ".join(
        clipped for _, r in reportable if (clipped := _clip(r, _PER_RESULT_CHARS))
    )
    return _clip(joined, _SUMMARY_CHARS)


class JarvisAgent:
    """Gemini client wrapper holding a short rolling conversation history.

    History exists so follow-ups work: "open a site" then "now search it for
    something" needs the first turn to resolve "it". It is deliberately small
    and short-lived — see ``config.HISTORY_TURNS`` and
    ``config.HISTORY_IDLE_SECONDS``.

    Thread safety: ``main.py`` claims ``_busy`` in ``_on_release``, before the
    worker thread starts, so exactly one command is ever in flight and a plain
    deque needs no lock. That invariant is the thing that would have to change
    for this to need one.
    """

    def __init__(self) -> None:
        self.router = gemini_client.get_router()
        # Two entries per turn: the user's command and the reply to it.
        self._history: deque = deque(maxlen=max(0, config.HISTORY_TURNS) * 2)
        self._last_turn_at = 0.0
        print(f"[llm] model chain: {' -> '.join(config.GEMINI_MODEL_CHAIN)}")

    # -- conversation history ------------------------------------------------
    @property
    def _hist(self) -> deque:
        """The history deque, created on first use.

        Lazy because the tests build the agent with ``__new__`` to avoid
        standing up a real router. Requiring ``__init__`` to have run would put
        the burden on every future test to know this attribute exists, and the
        one that forgot would fail with an AttributeError far from the cause.
        """
        history = self.__dict__.get("_history")
        if history is None:
            history = deque(maxlen=max(0, config.HISTORY_TURNS) * 2)
            self.__dict__["_history"] = history
        return history

    def _recall(self) -> list:
        """Prior turns still recent enough to be context, oldest first.

        Expiry is checked on read rather than on a timer: nothing else is
        running between commands, and a stale history that is never read is
        harmless. Whatever is left is dropped wholesale, not trimmed — half a
        conversation is a worse antecedent than none.
        """
        history = self._hist
        if not history:
            return []
        last = getattr(self, "_last_turn_at", 0.0)
        if time.monotonic() - last > config.HISTORY_IDLE_SECONDS:
            history.clear()
            print("[llm] conversation history expired; starting fresh.")
            return []
        return list(history)

    def _remember(self, command: str, reply: str) -> None:
        """Record a completed turn, whether the model answered it or not.

        Fast-path turns are recorded too. "Open a site" is answered by
        ``intents`` with no model call at all, so leaving it out would make the
        very follow-up this feature exists for — "now search it for something"
        — the one case with no antecedent.
        """
        history = self._hist
        if history.maxlen == 0 or not reply:
            return
        history.append(
            types.Content(role="user", parts=[types.Part(text=command)])
        )
        history.append(
            types.Content(role="model", parts=[types.Part(text=reply)])
        )
        self._last_turn_at = time.monotonic()

    def forget(self) -> None:
        """Drop the conversation history."""
        self._hist.clear()

    def handle(self, text: str) -> str:
        """Route a transcribed command, and return the reply.

        Unambiguous commands are dispatched locally without a model call — see
        ``intents``. Everything else goes to Gemini, with the last few turns
        prepended so follow-ups resolve.
        """
        command = (text or "").strip()
        if not command:
            return ""

        # Read before dispatching: the fast path records a turn of its own, and
        # this command's own turn must not appear in its own context.
        history = self._recall()

        fast = self._fast_path(command)
        if fast is not None:
            self._remember(command, fast)
            return fast

        # Per-command tool wrappers so the side-effect log can't leak between
        # commands (main.py runs each command on its own thread). Entries are
        # [tool_name, result].
        performed: list[list] = []
        # Gemini has no clock; without this, "in 20 minutes" and "tomorrow"
        # cannot be converted for set_alarm / add_calendar_event.
        now = datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M")
        gen_config = types.GenerateContentConfig(
            system_instruction=f"{_SYSTEM_PROMPT}\nCurrent date and time: {now}.",
            tools=[_tracked(fn, performed) for fn in _TOOLS],
            temperature=0.2,
        )

        def keep_falling_back(_model: str, _exc: Exception) -> bool:
            # Stop once a side-effect tool has run, so it isn't executed a
            # second time. Pure reads are safe to repeat on the next model.
            return _only_reads(performed)

        contents = history + [
            types.Content(role="user", parts=[types.Part(text=command)])
        ]
        try:
            response = self.router.generate_content(
                contents=contents,
                gen_config=gen_config,
                on_attempt_failed=keep_falling_back,
            )
        except gemini_client.NetworkUnavailable as exc:
            print(f"[llm] network unavailable: {exc}")
            return _with_actions(
                "I can't reach the internet right now — check the connection "
                "and try again.",
                performed,
            )
        except gemini_client.AllModelsUnavailable as exc:
            print(f"[llm] all models unavailable: {exc}")
            return _with_actions(
                "Gemini is out of quota right now — try again in a minute.",
                performed,
            )
        except Exception as exc:  # surface API/network errors to the user
            # Full details to the console; the spoken/HUD reply stays human.
            print(f"[llm] request failed: {exc}")
            summary = str(exc).splitlines()[0][:120]
            if "429" in summary or "RESOURCE_EXHAUSTED" in summary:
                summary = "Gemini hit a rate limit."
            return _with_actions(summary, performed)

        reply = (response.text or "").strip()
        if not reply:
            # No prose came back, but the action may still have succeeded.
            reply = _summarise(performed) or "(no response)"
        # Only successful turns are recorded. The failure paths above return
        # early on purpose: an API error is not something a follow-up should be
        # able to refer back to, and a half-finished turn is a worse antecedent
        # than none — "do that again" then asks instead of guessing.
        self._remember(command, reply)
        return reply

    @staticmethod
    def _fast_path(command: str):
        """Answer a completely unambiguous command without a model call.

        Saves the 1-3 s routing round trip and, as importantly, one request
        against a free-tier quota of 20/day per model — so the commands used
        most often keep working after the quota is spent.

        Returns None whenever there is any doubt, which sends the command down
        the normal Gemini path.
        """
        try:
            resolved = intents.match(command)
        except Exception as exc:  # a bad pattern must never block a command
            print(f"[llm] fast-path error, falling back to the model: {exc}")
            return None
        if resolved is None:
            return None

        name, payload = resolved
        if name is None:                       # answered from local knowledge
            print(f"[llm] answered locally, no model call: {command!r}")
            return payload

        fn = _tool_by_name(name)
        if fn is None:                         # table names a tool that moved
            return None
        print(f"[llm] fast path -> {name}({payload}), no model call")
        try:
            return fn(**payload)
        except Exception as exc:
            # Falling through costs a round trip but always beats failing.
            print(f"[llm] fast path failed ({exc}); handing to the model.")
            return None


def _tool_by_name(name: str):
    return next((f for f in _TOOLS if f.__name__ == name), None)


def _with_actions(message: str, performed: list) -> str:
    """Report an API failure without hiding work that already completed.

    When tools already ran, what the user cares about is what actually
    happened — the API hiccup on the follow-up turn is a footnote.
    """
    results = _summarise(performed)
    if not results:
        return message
    return f"{results} ({message.rstrip('.')} before I could wrap up.)"
