# Azleem — specification and acceptance criteria

The contract for this round of work. Every requirement traces to a root cause, a
fix at a specific location, and a test that fails if the fix is reverted.

Written to be checked, not admired: if a row's test doesn't exist or doesn't
actually prove the claim, the row is wrong and should be reported.

---

## 1. Complaints (verbatim)

The user's own words. These are the acceptance criteria.

> **C1.** "when i asked it to 'open youtube on my chrome browser', it did the job but it went to MKBHD youtube channel for some reason i dont know(fix that)"

> **C2.** "then it kept saying 'thinking' like the task wasnt done already"

> **C3.** "it needs to be able to be concise in its delivery of certain tasks without overthinking it...i was looking at youtube and it still felt like the job wasnt done for some reason"

> **C4.** "i need you to stress test Azleem to make sure its operations are tight, optimal, and effective. it should be able to do what it's told to do with record time, every time"

> **C5.** "also suggest 10 extra features to add to azleem arsenal"

And, after the first attempt was reported as ineffective:

> **C6.** "wow, whatever you though you did especially with the 'open youtube on chrome browser' did not work, still going to mkbhd and still taking too long to implement tasks i ask of it"

**C6 is a requirement, not just a bug report.** The first round's code was
correct and tested, but was never running. "The fix must be verifiably loaded"
is now an explicit requirement (R6).

---

## 2. Requirements

Status legend: **DONE** = implemented and covered by a passing test.

### R1 — Azleem must never substitute a target the user did not name
*Serves C1.*

| | |
|---|---|
| **Root cause** | Two hard-coded few-shot examples naming a real YouTube channel sat in the model's context on *every* request: the system prompt (`llm_agent._SYSTEM_PROMPT`) and the `perform_computer_task` docstring — which the google-genai SDK converts into the tool schema Gemini reads. The docstring was the stronger contaminant: it is attached to the exact tool chosen for "open YouTube in Chrome", so the example was maximally salient at the moment of deciding what to type. |
| **Fix** | Both examples replaced with neutral, non-branded ones. Anti-substitution rule added to `llm_agent._SYSTEM_PROMPT` and `computer_use._AGENT_PROMPT`: *never substitute a site, channel, creator, product or search term the task did not name; "open YouTube" means the home page.* |
| **Files** | `llm_agent.py` (`_SYSTEM_PROMPT`), `tools/computer_use.py` (`_AGENT_PROMPT`, `perform_computer_task` docstring) |
| **Tests** | `tests/test_prompts.py::TestNoBrandContamination` — screens **all seven** model-facing prompts (including `stt_engine._CONTEXT_PROMPT`, which biases the Whisper decoder) and every tool docstring. `test_every_model_facing_prompt_is_checked` guards the guard: a new `_*_PROMPT` constant that isn't registered fails the suite. `TestPromptDirectives` — asserts the anti-substitution rule is present. `tests/test_routing_live.py::test_open_youtube_in_chrome_goes_to_the_home_page` — live: rejects `@`, `/c/`, `/channel/`, `/watch`, `results?` in the URL and any invented `search_query`. |
| **Evidence** | Live run: `'open youtube on my chrome browser'` → `[('open_url', {'url': 'youtube', 'browser': 'chrome'})]`. Held on the fallback model too (the run hit a rate limit and switched to `gemini-3.5-flash`). |
| **Status** | **DONE** |

### R2 — The HUD must always reach a terminal state
*Serves C2, C3.*

`thinking` was the only state with no expiry. Four independent paths latched it:

| # | Root cause | Fix | Test |
|---|---|---|---|
| a | HUD switched to "thinking" on the listener thread, then the worker checked the busy lock and returned early on overlap — **without ever replying**. Nothing was left to clear the panel. | Claim the worker slot *before* showing "thinking"; always reply on the drop path. `main.py` `_on_release` | `test_concurrency.py::test_dropped_overlapping_command_still_replies`, `::test_dropped_command_never_shows_thinking` |
| b | `computer_use` progress callback is a module global with no command identity, so a straggler step from an abandoned task re-asserted "thinking" over a delivered reply. | Generation counter in `threading.local`; stale generations ignored. `main.py` `_report_progress` | `test_concurrency.py::TestStaleProgress` (4 tests) |
| c | The `reply` branch of `Overlay._drain` never called `deiconify()`, unlike `show`. A reply arriving while hidden was invisible **and** never animated, so its expiry timer never ran. | `reply` now deiconifies exactly as `show` does. `overlay.py` | `test_overlay.py::TestReplyAlwaysShows` |
| d | No watchdog of any kind. | 120 s stall cap (`_THINKING_STALL_S`), reset by every fresh progress update so genuinely long tasks are not cut off. Wording is deliberately non-committal ("Still working — no update for a while") because only `perform_computer_task` emits progress, so a slow-but-healthy command can reach the cap. `overlay.py` `_animate` | `test_overlay.py::TestThinkingWatchdog` (4 tests) |
| e | Esc keyed off `_busy`, which stays held until the worker's `finally` — *after* the reply is delivered. Pressing Esc in that window repainted "Cancelling…" over a finished reply with no worker left to clear it. Found in review. | Esc now keys off `_working`, set only while the agent call is actually in flight. `main.py` `_on_press` / `_process` | `test_concurrency.py::TestEscCancel` (5 tests) |

Also: `_AGENT_PROMPT` now instructs the agent to declare `done` the moment the
goal is visible rather than continuing to explore — the "overthinking" in C3.

**Status: DONE.** `test_concurrency.py::test_every_path_ends_in_a_reply` asserts
the property directly across five scenarios.

### R3 — Common commands must be fast
*Serves C3, C4.*

| | |
|---|---|
| **Root cause** | There was **no URL tool at all**. Every website command was forced through `perform_computer_task` — a screenshot → Gemini → click loop, up to 12 steps. |
| **Fix** | New `open_url(url, browser="", search_query="")` in `tools/os_tools.py`: spoken-name site map, direct search-results URLs, optional browser targeting, `http(s)`-only scheme validation. Registered in `llm_agent._TOOLS` and placed above `perform_computer_task` in the prompt. Browsers are launched via `CreateProcess` directly (executable resolved through `shutil.which` then the App Paths registry) — **never** through `cmd /c start`; see review finding #1. |
| **Tests** | `tests/test_open_url.py` (32 tests): resolution, manglings, deep-link preservation, search encoding, scheme refusal, shell-injection, `open_application` regression. `test_routing_live.py::test_only_one_tool_call_for_a_simple_site_open`. |
| **Measured** | `tests/bench.py`: url resolution < 1 ms, `open_url` pre-launch dispatch 14 ms (includes the registry lookup; the process launch itself is stubbed), transcribe a real command 0.672 s, screen capture 0.067 s. All within budget. |
| **Status** | **DONE** |

Secondary latency/robustness work, all configurable via `.env`:
`WHISPER_BEAM_SIZE` (default 1), `AGENT_SETTLE_SECONDS` (0.8 → 0.45),
`AGENT_MAX_STEPS`; `subprocess.run` timeout on the alarm PowerShell call;
WhatsApp fixed `sleep(6)` → poll-for-window; `Recorder._recording` guarded by its
lock; frame buffer capped; blocking speaker restore moved off the listener
thread; `.env.example` reconciled with `config.py`.

### R4 — Stress testing
*Serves C4.*

65 offline tests, stdlib `unittest` (no new dependencies), run via
`python main.py --selftest`.

| Tier | File | Covers |
|---|---|---|
| Prompt integrity | `test_prompts.py` | Brand contamination, required directives, all 12 tool schemas build, no `from __future__ import annotations` in `tools/`, build fingerprint |
| Fast path | `test_open_url.py` | URL resolution, search, scheme safety, `open_application` regression |
| HUD | `test_overlay.py` | State machine; `thinking` always terminates (fake clock) |
| Concurrency | `test_concurrency.py` | Busy slot, stale progress, recorder thread safety, frame cap |
| Live routing | `test_routing_live.py` | Real model routing with **non-executing** tool twins. Opt-in via `AZLEEM_LIVE_TESTS=1` |
| Latency | `bench.py` | Per-stage timings vs budgets, beam-size sweep |
| Manual | `LIVE_CHECKLIST.md` | 15 spoken commands with time budgets |

**Tests are mutation-verified** — reverting a fix makes its test fail:

| Mutation | Result |
|---|---|
| Remove the thinking watchdog | `test_thinking_always_terminates` FAILED |
| Undo the reply `deiconify` | 2 tests FAILED |
| Restore busy-check after "thinking" + silent drop | 2 tests FAILED |
| Restore the `cmd /c start` browser launch | 4 tests FAILED |
| Restore the path-swallowing alias lookup | 4 tests FAILED |

**Status: DONE.** 78 tests.

### R5 — 10 feature suggestions
*Serves C5.* Delivered in `FEATURES.md`, ranked by impact ÷ effort, with a
suggested build order. `open_url` is deliberately excluded — it is a bug fix
under R3, not a feature. **Status: DONE.**

### R6 — The running Azleem must be verifiably the code on disk
*Serves C6.*

| | |
|---|---|
| **Root cause** | Python loads modules into memory once at startup; editing a `.py` does nothing to a running process. The single-instance mutex (`AzleemAssistantSingleton`) made a naive relaunch exit as a duplicate, silently returning the user to the stale instance. Nothing in the logs revealed which build was live. Measured: the process under test started **16:11:44**, the fixes landed **19:12–19:34**. |
| **Fix** | `build_info.py` fingerprints runtime source (content + relative paths, excluding `tests/`, `__pycache__`, `logs/`). Printed in the startup banner and log. `restart.ps1` stops the running instance, waits for the mutex to clear, relaunches, and prints the live banner beside the on-disk stamp. |
| **Safety** | `restart.ps1` matches on the command line referencing *this* directory, and explicitly spares any process naming a different `main.py` — the machine also runs `clip smart` and `my cluely` under `pythonw`. Verified with `-DryRun`: 4 processes marked "leave alone", 1 "WOULD STOP". |
| **Tests** | `test_prompts.py::TestBuildFingerprint` (7 tests): stable across calls; changes on runtime-source edit and on module addition; ignores test files and `__pycache__`; `describe()` never raises. |
| **Evidence** | After restart — log: `build 79eda29 (17 files, newest 2026-07-26 20:06:41)`; disk: `build 79eda29`. Identical. |
| **Status** | **DONE** |

---

## 3. Acceptance test

The one that closes the loop, to be run by the user against the **restarted**
process:

> Say: **"open youtube on my chrome browser"**
>
> Expect: Chrome opens on the **youtube.com home page** — not a channel, not a
> video, no search results. One action. The HUD shows a reply and dismisses
> itself within a few seconds.

Full manual matrix in `tests/LIVE_CHECKLIST.md` (15 rows with time budgets).

---

## 4. Out of scope

Flag anything in these areas as scope creep:

- TTS / voice replies (Azleem has no speech output; proposed in `FEATURES.md` #2)
- Conversation memory / follow-up commands (`FEATURES.md` #3)
- Any new tool other than `open_url`
- Refactors not required by a requirement above
- Changes to `gemini_client.py` model-fallback logic
- Changes to the STT or hotkey pipeline beyond the listed latency items

---

## 5. Known gaps — stated plainly

1. **The old end-to-end path was never clocked.** The "15–40 s" figure in the
   original plan was inferred from `_MAX_STEPS` × per-step cost, not measured.
   What *is* measured: the command is now a single tool call with sub-millisecond
   dispatch.
2. **`WHISPER_BEAM_SIZE=1` is ~12% faster, not "2–3×"** as first estimated.
   Measured on real SAPI-generated speech: beam=1 0.639 s vs beam=5 0.726 s,
   identical transcription. The first benchmark used random noise, which makes
   Whisper hallucinate long output so decode time tracked output length rather
   than beam width — it reported beam=1 as *slower*. Fixtures are now real
   speech (`tests/make_fixtures.ps1`).
3. **No git baseline.** The repo has no commits and its git root is the user's
   home directory, so "nothing outside scope changed" cannot be proven by diff —
   only by inspecting the file list in §6.
4. ~~**`restart.ps1` process matching is by elimination**~~ — **CLOSED
   2026-07-28, after the "in principle" happened.** The gap said a different app
   launched bare from another directory "could in principle match". On this
   machine two did: `-DryRun` marked PID 29872
   (`my cluely\.venv312\Scripts\pythonw.exe main.py`) and PID 33684
   (`Python312\pythonw.exe main.py`) as WOULD STOP. They are the same app —
   33684's parent is 29872, and it loads `my cluely\.venv312\...\PySide6`. The
   script's own comment, that the machine's other python apps "are all launched
   with a fully-qualified script path", was simply untrue.

   Azleem now identifies itself rather than being deduced. `main.py`
   `_claim_pid_file` writes `logs/azleem.pid` (PID on line 1, the `main.py` it
   is running on line 2) once the singleton mutex is held; `restart.ps1` trusts
   that claim when the PID is still a live python running a `main.py` and does
   not post-date the file — a recycled PID would. Failing that it matches on
   this directory, now segment-wise so `C:/…/JARVIS/main.py` matches too.
   Anything still bare and unclaimed is **reported and left alone**, never
   killed by elimination. The Windows auto-start shortcut was the reason the
   bare case existed at all; it launched `pythonw.exe main.py` from
   `WorkDir=JARVIS` and now passes the fully-qualified path.

   Traded a silent wrong kill for a loud missed one: an Azleem predating this
   change is skipped with a message saying how to stop it by hand.
   Guarded by `tests/test_restart.py`, which drives the real `restart.ps1` via
   `-DryRun -ProcessSource` over the observed process list. Six mutations
   caught, including restoring rule 3.
4b. **The watchdog cannot run while the HUD is hidden.** `_animate` is gated on
   `_visible`, and `set_state("thinking")` does not deiconify. Every current path
   into "thinking" is followed by a `show_reply` that does deiconify, so this is
   latent rather than live — but it makes the watchdog a weaker backstop than it
   appears, and `test_overlay.py`'s `pump()` reproduces the same gate, so no test
   would catch a regression here.
4c. **Esc only aborts promptly inside `perform_computer_task`**, the one tool
   that polls `cancellation.cancelled()`. For the other 11, Esc marks the intent
   and the HUD says "Cancelling…", but the tool runs to completion first.
5. **Live routing tests cost API quota** and are rate-limited (~5 req/min on the
   free tier), so they are opt-in and paced.
6. **The HUD watchdog is a backstop, not a cure.** Hitting the 45 s cap means
   something upstream is wrong; it exists so the symptom is bounded.

---

## 6. Files changed

Anything modified outside this list is scope creep.

**New:** `build_info.py`, `restart.ps1`, `spec.md`, `CLAUDE.md`, `FEATURES.md`,
`tests/__init__.py`, `tests/test_prompts.py`, `tests/test_open_url.py`,
`tests/test_overlay.py`, `tests/test_concurrency.py`,
`tests/test_routing_live.py`, `tests/bench.py`, `tests/LIVE_CHECKLIST.md`,
`tests/make_fixtures.ps1`, `tests/fixtures/*.wav`

**Modified:** `main.py`, `llm_agent.py`, `overlay.py`, `config.py`,
`stt_engine.py`, `tools/os_tools.py`, `tools/computer_use.py`,
`tools/productivity.py`, `.env.example`, `requirements.txt`

**Untouched:** `gemini_client.py`, `cancellation.py`, `speaker_mute.py`,
`tools/screen.py`, `tools/vision_tools.py`, `tools/coding.py`,
`alarm_popup.pyw`, `diagnose_keys.py`

---

## 7. How to verify

```bash
python main.py --selftest      # 65 offline tests
python tests/bench.py          # latency table vs budgets
python -m build_info           # fingerprint of source on disk
```

```bash
AZLEEM_LIVE_TESTS=1 python -m unittest tests.test_routing_live -v
```

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File restart.ps1 -DryRun
```

After any source change, restart before testing behaviour, and confirm the
banner's build stamp matches `python -m build_info`.

---

## 8. Independent review

An adversarial review was run against this spec by a separate agent, instructed
to verify every claim from source rather than trust the document. It found 11
issues. All CRITICAL and MAJOR findings are fixed; the MINOR ones are fixed or
recorded above as known gaps.

| # | Severity | Finding | Resolution |
|---|---|---|---|
| 1 | CRITICAL | `open_url` launched browsers via `cmd /c start`. `cmd` splits on `& \| ^ < >` and Python only quotes arguments containing spaces, so `…/watch?v=a&t=90` lost its tail **and executed the remainder as a shell command**. The URL comes from the model, which is influenced by on-screen text. `open_application` had the same hole. | Browsers now launch through `CreateProcess` directly, executable resolved via `shutil.which` then App Paths. `open_application` refuses shell metacharacters. `TestNoShellInjection` (4 tests) + `test_shell_metacharacters_are_refused`. Mutation-verified. |
| 2 | MAJOR | Bare domains with a path collapsed to the site home page for all 26 aliased hosts — `github.com/torvalds/linux` → `https://github.com` — while the reply claimed success. The spec's "deep-link preservation" test only covered URLs already starting with `https://`. | Alias lookup now applies only when there is no path. `test_bare_domain_with_a_path_keeps_the_path`. Mutation-verified. |
| 3 | MAJOR | The running process was stale *again* at review time — `stt_engine.py` was edited three minutes after the restart. | Restarted after the final edit; stamps re-verified. This is the failure R6 exists to catch, and it caught it. |
| 4 | MAJOR | Esc keyed off `_busy`, reintroducing the stuck-"thinking" symptom for 45 s. | Fixed via `_working`; see R2(e). |
| 5 | MAJOR | The brand guard checked 2 of 7 model-facing prompts. All were clean, so this was guard strength, not a live bug. | Extended to all 7, plus a meta-test that fails when a new prompt constant is unregistered. |
| 6 | MINOR | Watchdog inert while the HUD is hidden. | Recorded as known gap 4b; latent, all current paths verified terminal. |
| 7 | MINOR | Watchdog could claim failure during a slow-but-healthy command. | Raised to 120 s and reworded to "Still working — no update for a while." |
| 8 | MINOR | `build_info` globbed only `*.py`, missing `alarm_popup.pyw`. | Now globs `*.py` and `*.pyw`. |
| 9 | MINOR | `restart.ps1` relaunched with a bare `main.py`, perpetuating its own weakest match; directory regex was unanchored. | Relaunches with the qualified path; regex anchored to a path boundary. |
| 10 | MINOR | Doc drift: test count, `config.py`'s stale "2-3x" beam claim, `GITHUB_TOKEN` missing from `.env.example`, `CLAUDE.md` absent from §6. | All corrected. |
| 11 | MINOR | The "dispatch" benchmark stubbed the process launch, so the figure did not substantiate an end-to-end speed claim. | Relabelled "pre-launch" and annotated with what it excludes. |

The reviewer's verdict on the user's original complaints, after these fixes: C1
resolved (contamination gone from every surface; finding #2's adjacent
regression now fixed), C2 substantially resolved (five paths fixed, remaining
gaps recorded), C3 addressed on both the routing and the agent-behaviour halves.
