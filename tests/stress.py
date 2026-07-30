"""Stress and efficiency scoreboard for every capability Azleem has.

``bench.py`` times the pipeline stages. This times the *capabilities* — all
thirteen tools plus the routing layer — and counts the two things that actually
dominate a command's wall clock:

    model calls   ~1-4 s each, and one free-tier request apiece
    screenshots   ~50-150 ms each

Wall-clock alone hides regressions, because a change that adds a model call
looks fine on a fast day and terrible on a slow one. Counting the calls makes
the cost visible whatever the network is doing.

    python tests/stress.py              # offline; nothing launched, no quota
    python tests/stress.py --soak       # + concurrency and repeat-abuse checks

Nothing here opens an app, clicks the screen, or sends a request: every
side-effecting call is stubbed. Safe to run at any time.
"""

import argparse
import contextlib
import json
import os
import statistics
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

# Per-capability ceilings. These are budgets for the work Azleem itself does —
# model latency and process launches are excluded (and counted separately),
# because they are not what a code change can regress.
BUDGETS = {
    "route: fast path (no model)": 0.010,
    "route: falls through to model": 0.002,
    "open_url": 0.050,
    "open_application": 0.050,
    "search_and_open_file (hit)": 1.500,
    "search_and_open_file (miss)": 1.500,
    "take_screenshot": 0.400,
    "set_alarm (validation only)": 0.005,
    "add_calendar_event": 0.050,
    "write_note": 0.050,
    "screen capture + encode": 0.200,
    "change detection": 0.030,
}

# Model calls and screenshots a whole 16-question quiz should cost.
QUIZ_BUDGET = {"model": 34, "capture": 40}


class Board:
    def __init__(self):
        self.rows = []
        self.failed = []

    def time(self, name, fn, repeat=1, warmup=0):
        for _ in range(warmup):
            with contextlib.suppress(Exception):
                fn()
        samples, error = [], None
        for _ in range(repeat):
            start = time.perf_counter()
            try:
                fn()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:60]
                break
            samples.append(time.perf_counter() - start)
        self._row(name, samples, error)

    def _row(self, name, samples, error):
        budget = BUDGETS.get(name)
        if error:
            print(f"  {name:34} {'ERROR':>10}   {error}")
            self.failed.append(name)
            return
        median, worst = statistics.median(samples), max(samples)
        verdict = ""
        if budget:
            verdict = "PASS" if worst <= budget else (
                "MARGINAL" if median <= budget else "OVER")
            if verdict == "OVER":
                self.failed.append(name)
        print(f"  {name:34} {median*1000:8.2f} ms  (worst {worst*1000:8.2f})  "
              f"{('budget ' + format(budget*1000, '.0f') + 'ms') if budget else '':16}"
              f"{verdict}")

    def count(self, name, got, budget, unit):
        verdict = "PASS" if got <= budget else "OVER"
        if verdict == "OVER":
            self.failed.append(name)
        print(f"  {name:34} {got:8} {unit:<10}  (budget {budget})   {verdict}")


# ---- stubs -----------------------------------------------------------------
@contextlib.contextmanager
def no_side_effects():
    """Neutralise every way a tool can touch the machine."""
    import subprocess

    from tools import os_tools, productivity
    with contextlib.ExitStack() as st:
        for mod in (os_tools, productivity):
            st.enter_context(mock.patch.object(mod.os, "startfile",
                                               lambda *_a: None, create=True))
            st.enter_context(mock.patch.object(mod.subprocess, "Popen",
                                               lambda *_a, **_k: None))
        st.enter_context(mock.patch.object(
            subprocess, "run",
            lambda *_a, **_k: mock.Mock(returncode=0, stdout="", stderr="")))
        yield


def bench_capabilities(board):
    print("\n-- Routing " + "-" * 62)
    import intents

    board.time("route: fast path (no model)",
               lambda: intents.match("open notepad"), repeat=500)
    board.time("route: falls through to model",
               lambda: intents.match("solve the quiz on my screen and move on"),
               repeat=500)

    covered = sum(1 for c in FAST_PATH_SAMPLES if intents.match(c))
    print(f"\n  fast path covers {covered}/{len(FAST_PATH_SAMPLES)} everyday "
          f"commands — each one skips a model call entirely")

    print("\n-- Tools (nothing actually launched) " + "-" * 36)
    from tools.os_tools import open_application, open_url, search_and_open_file
    from tools.productivity import add_calendar_event, set_alarm, take_screenshot, write_note

    with no_side_effects():
        board.time("open_url", lambda: open_url("youtube", browser="chrome"),
                   repeat=30, warmup=2)
        board.time("open_application", lambda: open_application("notepad"),
                   repeat=30, warmup=2)
        board.time("search_and_open_file (hit)",
                   lambda: search_and_open_file("cv"), repeat=3, warmup=1)
        board.time("search_and_open_file (miss)",
                   lambda: search_and_open_file("zzz-nothing-matches-this"),
                   repeat=3, warmup=1)
        board.time("take_screenshot", take_screenshot, repeat=3, warmup=1)
        board.time("set_alarm (validation only)",
                   lambda: set_alarm("not-a-time"), repeat=100)
        board.time("add_calendar_event",
                   lambda: add_calendar_event("bench", "2030-01-01", "09:00"),
                   repeat=10, warmup=1)
        board.time("write_note", lambda: write_note("bench body", "bench note"),
                   repeat=10, warmup=1)

    print("\n-- Vision primitives " + "-" * 52)
    from tools import screen

    board.time("screen capture + encode", screen.capture_screen,
               repeat=5, warmup=1)
    a = Image.new("RGB", (1920, 1080), "white")
    b = a.copy()
    b.paste(Image.new("RGB", (300, 60), "blue"), (100, 300))
    board.time("change detection", lambda: screen.screens_differ(a, b), repeat=20)


FAST_PATH_SAMPLES = [
    "open notepad", "open chrome", "take a screenshot", "screenshot",
    "what's the time", "what's the date", "open youtube", "launch calculator",
    "open file explorer", "go to gmail", "hey azleem open notepad please",
    "start spotify",
]


def bench_quiz_efficiency(board):
    """How many model calls and screenshots does a 16-question quiz cost?"""
    print("\n-- Quiz efficiency (16 questions, fake model) " + "-" * 27)
    import cancellation
    from tools import quiz

    counts = {"model": 0, "capture": 0}

    def fake_generate(contents, gen_config=None, needs_pointing=False):
        counts["model"] += 1
        prompt = next((c for c in contents if isinstance(c, str)), "")
        if "answer_letter" in prompt:
            return mock.Mock(text=json.dumps({
                "question": "Q", "options": {"A": "x", "B": "India"},
                "answer_letter": "B", "answer_text": "India",
                "confidence": "high", "needs_current_info": False,
                "kind": "single_choice", "advance_label": "Next",
                "advance_is_final": False, "finished": False}))
        return mock.Mock(text=json.dumps(
            {"found": True, "box": [300, 100, 360, 700], "is_final_submit": False}))

    tick = [0]

    def fake_capture():
        counts["capture"] += 1
        tick[0] += 1
        return Image.new("RGB", (1920, 1080), (tick[0] * 3 % 255, 0, 0)), b"jpeg"

    cancellation.reset()
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(quiz.providers, "vision_generate",
                                           side_effect=fake_generate))
        st.enter_context(mock.patch.object(quiz.screen, "capture_screen",
                                           side_effect=fake_capture))
        st.enter_context(mock.patch.object(quiz.screen, "wait_for_change",
                                           side_effect=lambda *a, **k:
                                           (True, *fake_capture())))
        st.enter_context(mock.patch.object(quiz, "pyautogui", mock.Mock()))
        st.enter_context(mock.patch.object(quiz.config, "QUIZ_MAX_QUESTIONS", 16))
        quiz.answer_quiz("")

    board.count("quiz: model calls (16 questions)", counts["model"],
                QUIZ_BUDGET["model"], "calls")
    board.count("quiz: screenshots (16 questions)", counts["capture"],
                QUIZ_BUDGET["capture"], "shots")
    print(f"    -> {counts['model']/16:.2f} model calls per question "
          f"({counts['model']} total; at ~4 s each that is "
          f"~{counts['model']*4/60:.1f} min of model time)")


def soak(board):
    """Abuse the paths that broke before: concurrency and repetition."""
    print("\n-- Soak " + "-" * 65)
    from stt_engine import Recorder

    # The recorder race that opened two streams (gotcha 5).
    errors = []

    def hammer(rec):
        for _ in range(60):
            try:
                rec.start()
                rec.stop()
            except Exception as exc:
                errors.append(exc)

    with mock.patch("sounddevice.InputStream") as stream:
        stream.return_value = mock.Mock()
        rec = Recorder()
        threads = [threading.Thread(target=hammer, args=(rec,)) for _ in range(4)]
        start = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - start
    print(f"  240 concurrent recorder start/stop      {elapsed*1000:8.1f} ms  "
          f"{len(errors)} errors")
    if errors:
        board.failed.append("recorder concurrency")

    # Fast-path determinism: the same command must always resolve the same way.
    import intents
    unstable = []
    for command in FAST_PATH_SAMPLES:
        results = {json.dumps(intents.match(command), default=str)
                   for _ in range(50)}
        if len(results) > 1:
            unstable.append(command)
    print(f"  fast path determinism (50x each)        "
          f"{'stable' if not unstable else 'UNSTABLE: ' + str(unstable)}")
    if unstable:
        board.failed.append("fast path determinism")

    # Change detection must not drift with image size.
    from tools import screen
    sizes = [(1280, 720), (1920, 1080), (2560, 1440), (3840, 2160)]
    verdicts = []
    for w, h in sizes:
        a = Image.new("RGB", (w, h), "white")
        b = a.copy()
        b.paste(Image.new("RGB", (w // 6, h // 18), "blue"), (w // 10, h // 3))
        verdicts.append(screen.screens_differ(a, b))
    print(f"  change detection across 4 resolutions   "
          f"{'consistent' if all(verdicts) else 'INCONSISTENT ' + str(verdicts)}")
    if not all(verdicts):
        board.failed.append("change detection resolution")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--soak", action="store_true",
                        help="also run concurrency and repetition abuse")
    args = parser.parse_args()

    print("=" * 88)
    print("  AZLEEM CAPABILITY STRESS TEST")
    print("=" * 88)

    board = Board()
    bench_capabilities(board)
    bench_quiz_efficiency(board)
    if args.soak:
        soak(board)

    print("\n" + "=" * 88)
    if board.failed:
        print(f"  OVER BUDGET / FAILED: {', '.join(sorted(set(board.failed)))}")
    else:
        print("  Every capability within budget.")
    print("=" * 88)
    return 1 if board.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
