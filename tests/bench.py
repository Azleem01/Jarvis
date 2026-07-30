"""Latency scoreboard: where a command's wall-clock time actually goes.

    python tests/bench.py            # offline stages only (no API quota)
    python tests/bench.py --live     # adds real model routing latency

Every stage has a budget. The point is not a single number but a table you can
re-run after a change to see whether it helped, and which stage is dominating.
"""

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

import config  # noqa: E402

# stage name -> seconds we consider acceptable for a snappy assistant
BUDGETS = {
    "whisper load (once)": 12.0,
    "transcribe a real command": 1.2,
    "whisper decode (VAD miss, 2 passes)": 2.5,
    "url resolution": 0.001,
    "open_url dispatch (pre-launch)": 0.05,
    "screen capture + encode": 0.60,
    "model routing (live)": 3.0,
    "full open-url command (live)": 4.0,
}


class Timer:
    def __init__(self):
        self.rows = []

    def measure(self, name, fn, repeat=1, warmup=0):
        for _ in range(warmup):
            try:
                fn()
            except Exception:
                pass
        samples = []
        error = None
        for _ in range(repeat):
            start = time.perf_counter()
            try:
                fn()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            samples.append(time.perf_counter() - start)
        self.rows.append((name, samples, error))
        self._print_row(self.rows[-1])
        return samples

    @staticmethod
    def _print_row(row):
        name, samples, error = row
        budget = BUDGETS.get(name)
        if error:
            print(f"  {name:32} {'ERROR':>10}   {error[:44]}")
            return
        median = statistics.median(samples)
        worst = max(samples)
        if budget is None:
            verdict = ""
        elif worst <= budget:
            verdict = "PASS"
        elif median <= budget:
            verdict = "MARGINAL"
        else:
            verdict = "OVER BUDGET"
        budget_text = f"budget {budget:.3f}s" if budget else ""
        print(
            f"  {name:32} {median:8.3f}s  (worst {worst:6.3f}s)  "
            f"{budget_text:16} {verdict}"
        )

    def summary(self):
        over = [
            n for n, s, e in self.rows
            if not e and BUDGETS.get(n) and max(s) > BUDGETS[n]
            and statistics.median(s) > BUDGETS[n]
        ]
        errors = [n for n, _s, e in self.rows if e]
        print()
        if errors:
            print(f"  {len(errors)} stage(s) errored: {', '.join(errors)}")
        if over:
            print(f"  OVER BUDGET: {', '.join(over)}")
        else:
            print("  All measured stages within budget.")
        return not over


def bench_offline(t):
    print("\n-- Tool dispatch (no network, nothing launched) " + "-" * 24)

    from unittest import mock
    from tools import os_tools
    from tools.os_tools import _resolve_site, open_url

    t.measure(
        "url resolution",
        lambda: [_resolve_site(s) for s in
                 ("youtube", "you tube", "https://x.com/a/b", "example.com")],
        repeat=200,
    )

    def dispatch():
        with mock.patch.object(os_tools.os, "startfile", lambda *_a: None, create=True), \
             mock.patch.object(os_tools.subprocess, "Popen", lambda *_a, **_k: None):
            open_url("youtube", browser="chrome")

    # Note what this does and does not include: URL resolution, search-URL
    # construction and the browser-executable lookup are real; the process
    # launch itself is stubbed. It is the work Azleem does before handing off,
    # not the time for a window to appear.
    t.measure("open_url dispatch (pre-launch)", dispatch, repeat=50)

    print("\n-- Vision payload " + "-" * 53)
    try:
        from tools import screen
        t.measure("screen capture + encode",
                  lambda: screen.capture_screen(), repeat=5, warmup=1)
    except Exception as exc:
        print(f"  screen capture unavailable: {exc}")

    print("\n-- Speech to text " + "-" * 53)
    print(f"  model={config.WHISPER_MODEL} device={config.WHISPER_DEVICE} "
          f"compute={config.WHISPER_COMPUTE_TYPE} beam={config.WHISPER_BEAM_SIZE}")
    try:
        from stt_engine import Transcriber
        holder = {}
        t.measure("whisper load (once)",
                  lambda: holder.setdefault("t", Transcriber()), repeat=1)
        transcriber = holder["t"]

        clip = _load_fixture("open_youtube_chrome.wav")
        if clip is None:
            print("  no speech fixture found; skipping decode timings "
                  "(run tests/make_fixtures.ps1 to create them)")
            return

        t.measure("transcribe a real command",
                  lambda: transcriber.transcribe(clip), repeat=3, warmup=1)
        # The worst case in production: VAD discards a quiet push-to-talk take
        # and the clip is decoded a second time, unfiltered. Both passes are
        # forced here — with real speech the first one succeeds, so a
        # short-circuit would quietly measure only half the work.
        def vad_miss():
            transcriber._run(clip, vad=True)
            transcriber._run(clip, vad=False)

        t.measure("whisper decode (VAD miss, 2 passes)", vad_miss, repeat=2)

        _compare_beam_sizes(transcriber, clip)
    except Exception as exc:
        print(f"  whisper unavailable: {exc}")


def _load_fixture(name, target=None):
    """Load a fixture WAV as mono float32 at Whisper's sample rate.

    The fixtures are real synthesised speech rather than noise: random noise
    makes Whisper hallucinate long transcripts, so decode time tracks output
    length instead of the setting under test, and every measurement lies.
    """
    import wave

    target = target or config.SAMPLE_RATE
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", name)
    if not os.path.exists(path):
        return None
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != target:
        count = int(len(audio) * target / rate)
        audio = np.interp(
            np.linspace(0, len(audio) - 1, count), np.arange(len(audio)), audio
        ).astype(np.float32)
    return audio


def _compare_beam_sizes(transcriber, clip):
    """Show what the beam_size default is actually buying, in seconds."""
    print("\n  beam_size sweep (decode cost, and what it transcribes):")
    original = config.WHISPER_BEAM_SIZE
    try:
        for beam in (1, 5):
            config.WHISPER_BEAM_SIZE = beam
            transcriber.transcribe(clip)               # warm up this path
            start = time.perf_counter()
            text = transcriber.transcribe(clip)
            elapsed = time.perf_counter() - start
            marker = "  <- default" if beam == original else ""
            print(f"    beam={beam}: {elapsed:6.3f}s  {text!r}{marker}")
    finally:
        config.WHISPER_BEAM_SIZE = original


def bench_live(t):
    print("\n-- Model routing (uses API quota) " + "-" * 37)
    import datetime
    import functools
    from google.genai import types
    import gemini_client
    import llm_agent

    config.validate()

    def inert(fn):
        @functools.wraps(fn)
        def wrapper(*_a, **_k):
            return f"{fn.__name__} completed successfully."
        return wrapper

    now = datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M")
    gen_config = types.GenerateContentConfig(
        system_instruction=f"{llm_agent._SYSTEM_PROMPT}\nCurrent date and time: {now}.",
        tools=[inert(fn) for fn in llm_agent._TOOLS],
        temperature=0.2,
    )
    router = gemini_client.get_router()

    def route():
        router.generate_content(
            contents="open youtube on my chrome browser", gen_config=gen_config
        )

    # One request per call and a pause between them: the free tier allows about
    # five per minute per model, and a 429 here would measure the retry, not us.
    t.measure("model routing (live)", route, repeat=1)
    time.sleep(13)
    t.measure("full open-url command (live)", route, repeat=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="also time real model routing (spends API quota)")
    args = parser.parse_args()

    print("=" * 86)
    print("  AZLEEM LATENCY BENCHMARK")
    print("=" * 86)

    t = Timer()
    bench_offline(t)
    if args.live:
        bench_live(t)
    else:
        print("\n  (skipping live model timing; pass --live to include it)")

    print("\n" + "=" * 86)
    ok = t.summary()
    print("=" * 86)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
