# CLAUDE.md — orientation for a fresh session

Azleem: a push-to-talk Windows desktop assistant. Hold a hotkey, speak, release.
Local Whisper transcribes, Gemini picks a tool, the tool does the thing, the HUD
shows the reply.

Read this before changing anything. The gotchas section is not padding — every
item cost real debugging time.

---

## THE ONE THING THAT WILL WASTE YOUR TIME

**Python loads modules into memory once at startup. Editing a `.py` file does
nothing to a running Azleem.** And the single-instance mutex
(`AzleemAssistantSingleton` in `main.py`) means launching a "fresh" copy exits
with *"already running"* and silently hands you back the stale process.

This cost a full round: fixes were verified on disk, all tests green, and the
user kept reproducing the old bug because the process predated the changes by
three hours. Nothing in the logs revealed it.

**After any source change:**

```bash
powershell -NoProfile -ExecutionPolicy Bypass -File restart.ps1
```

It prints the live build stamp beside the on-disk one. They must match:

```
Azleem is live:
  ===== Azleem started 2026-07-26 20:12:31 | build 79eda29 (17 files, newest 20:06:41) =====
Source on disk:
  build 79eda29 (17 files, newest 2026-07-26 20:06:41)
```

Never report a behaviour fix as working without seeing those match.

---

## Architecture

Single process, no async, four threads (documented at `main.py:11`).

```
hotkey (pynput listener thread)
  └─ hold timer ──> Recorder.start()          stt_engine.py
       [speak]     sounddevice callback thread appends frames
  └─ release ────> Recorder.stop() -> float32 array
                   claim _busy, HUD -> "thinking"
                   worker thread:
                     Transcriber.transcribe()  faster-whisper, local, offline
                     JarvisAgent.handle()      llm_agent.py
                       └─ gemini_client.ModelRouter -> Gemini
                            └─ SDK calls a tool from _TOOLS directly
                     HUD -> reply (auto-dismisses)
```

| File | Role |
|---|---|
| `main.py` | Hotkey, threading, command pipeline, HUD wiring, `--check-mic` / `--selftest` |
| `llm_agent.py` | System prompt + tool registry (`_TOOLS`); one Gemini call per command |
| `gemini_client.py` | Model fallback chain, cooldowns, retries. **Well-built — leave alone** |
| `overlay.py` | Tk HUD. States: `listening` / `thinking` / `reply` (+ hidden = idle) |
| `stt_engine.py` | `Recorder` (sounddevice) + `Transcriber` (faster-whisper) |
| `build_info.py` | Source fingerprint — the staleness guard |
| `config.py` | All `.env` settings |
| `tools/` | The callables Gemini can invoke (20, incl. `accomplish_with_code`) |
| `intents.py` | Local fast paths — commands answered with no model call |
| `providers.py` | Which provider serves vision vs routing |
| `openrouter_client.py` | Free-model chain; same shape as `gemini_client` |

---

## Gotchas that will bite you

**1. Never add `from __future__ import annotations` to anything in `tools/`.**
It turns type hints into strings, and google-genai does `isinstance(value,
annotation)` when invoking a tool. Every call dies with *"isinstance() arg 2
must be a type"* — and some models then claim success anyway. Guarded by
`tests/test_prompts.py::test_no_future_annotations_in_tools`.

**2. Tool docstrings are prompt text.** The SDK converts each docstring into the
function declaration Gemini reads. A worked example in a docstring is as
load-bearing as the system prompt — **this is exactly how the MKBHD bug
happened**: `perform_computer_task`'s docstring said *"search YouTube for the
latest MKBHD video"*, so "open YouTube" drifted to that channel. Never put a
real brand, creator, channel or product in a prompt or docstring. Guarded by
`TestNoBrandContamination`.

**3. `thinking` is the only HUD state nothing else expires.** Any new code path
that sets it must guarantee a terminal `show_reply`. There is a 45 s watchdog
backstop (`overlay._THINKING_STALL_S`) but hitting it means something upstream
is broken.

**4. `_busy` is claimed in `_on_release`, not in `_process`.** The worker only
releases it. Claiming it inside the worker (the original design) meant a dropped
overlapping command left the HUD spinning forever.

**5. `Recorder.start()`/`stop()` must hold `_lock` across the *whole*
transition** — guard, buffer, stream creation, and flag. Setting `_recording`
outside the lock let two callers open two streams, and let a `stop()` land
between the flag and the stream assignment. Caught by
`TestRecorderThreadSafety` (intermittent — run it several times).

**6. The pynput listener thread must not block.** Blocking it also blocks Esc,
which is the cancel key. Speaker restore is already handed to a thread for this
reason.

**7. Free-tier Gemini is ~5 req/min per model.** `logs/azleem.log` shows the
fallback chain walking models regularly. Live tests are paced and opt-in.

**8. Whisper benchmarks need real speech.** Random noise makes Whisper
hallucinate long output, so decode time tracks output length rather than the
setting under test — it reported `beam_size=1` as *slower* than 5, the opposite
of the truth. Fixtures come from `tests/make_fixtures.ps1` (Windows SAPI).

**9. Git root is `C:/Users/aleem`, the whole home directory, with no commits.**
`git add .` here would stage thousands of unrelated files. Don't commit unless
the user runs `git init` inside `JARVIS/` first.

**10. Other `pythonw` apps run on this machine** (`clip smart`, `my cluely`),
and *they do not all pass a qualified script path*. `restart.ps1` used to treat
a bare `pythonw.exe main.py` as ours by elimination — Win32_Process exposes no
working directory — on the stated assumption that everything else on the machine
launches qualified. `-DryRun` on 2026-07-28 disproved it: two "my cluely"
processes (a parent and the child it spawns) were both bare, both marked
**WOULD STOP**. Azleem now writes `logs/azleem.pid` at startup and the script
trusts that claim; a bare unclaimed process is reported and spared. Never
identify a process by what it *isn't* — have it say what it is.
`restart.ps1 -DryRun` still shows the selection, with a reason per process.

**11. A rule only works at the level where the decision is made.**
`_AGENT_PROMPT` said *"answer from your OWN knowledge … never search the web"* —
correctly — but that string is only in context *inside* `perform_computer_task`.
The choice of tool happens one level up in `_SYSTEM_PROMPT`, which said nothing,
so a quiz question on screen was routed to `open_url` and a Google search. The
rule was present and irrelevant. Before writing a directive, ask which model
call actually reads it.

**12. Don't write a guardrail an explicit instruction can't override.**
The blanket *"NEVER click a final Submit button"* also blocked a quiz's **Next**
control, so "answer it and move to the next one" was structurally impossible —
the loop typed the answer and declared done. Guardrails against acting
*unprompted* must carve out the case where the user prompted it.

**13. The HUD cuts replies at 220 chars** (`overlay._fit_reply`), and what
overflows is the tail — where the outcome lives. `llm_agent._summarise` budgets
the reply and drops read-only tool results once something has actually been
done; the full log still goes to `logs/azleem.log`.

**14. Never measure screen change by *mean* pixel difference.** A page is mostly
unchanged background, which averages any real edit down to nothing. The old
`screens_differ` (mean diff of 64px thumbnails vs 2.0) scored **0.616 for
clicking a quiz option and 0.631 for advancing to a completely different
question** — both reported as "no change". The agent was then told its correct
answer had done nothing and picked a different option to make something happen.
`screen.changed_fraction` counts pixels that moved instead; separation is ~5×
either side of the threshold. Guarded by `tests/test_screen_change.py`.

**15. Ask a vision model for one thing per call.** Requesting the question,
the options, the answer *and* the bounding boxes in a single call located the
right option **2 times in 5** (off by up to 344/1000 — clicking a different
answer than the one it named). Asking "the box of the option whose text is
exactly X" on its own: **3/3, within ~5 px**. `tools/quiz.py` is built around
this; do not "optimise" the two calls back into one.

**16. Not every OpenRouter model can point.** The free Gemma models return
click-accurate boxes; the Nvidia Nemotron ones answer questions correctly but
mislocate by up to 231/1000 — they would select the wrong answer *while
reporting the right one*, which looks exactly like success. Hence two chains:
`OPENROUTER_POINTING_MODELS` and `OPENROUTER_VISION_MODELS`. Guarded by
`tests/test_openrouter.py::TestModelChains`.

**17. Models put the option label in the answer text.** `"C. Canberra"`, not
`"Canberra"` — and that string is used verbatim to find the option on screen.
`quiz._strip_option_label` removes it. Found by the live test, not the offline
one; it is the kind of thing only a real call surfaces.

**18. Fixed settle sleeps pay the worst case on every action.** The quiz loop
spent **19.2 s of a 16-question run** sleeping, almost all of it after the UI
had already finished reacting. `screen.wait_for_change` polls instead and
returns on the first change — and hands back the frame it settled on, so the
caller doesn't screenshot again. When you replace a sleep, use its return value
rather than capturing afresh.

**19. Anything cached to avoid a model call must keep a cheap safety signal.**
Caching the Next button's box removed a third of the quiz's requests, but it
also removed the only thing that noticed a **Next turning into Submit** on the
last question — Azleem would have clicked it. The fix was to have the answering
call (already made every question) report the button's *label*: text, not
coordinates, so it can't repeat gotcha 15. Guarded by
`test_a_cached_next_still_notices_it_became_submit`.

**20. The fast path may only match what is unambiguous.** `intents.py` answers
"open notepad" without a model call, but a wrong instant answer is worse than a
right slow one — the user never gets routed properly. Any second clause
("open notepad **and** write my list"), any pronoun target ("open **up**"), and
anything touching the screen must return None and fall through. Guarded by
`tests/test_intents.py::TestRefusals`, which is deliberately larger than the
matching tests.

**21. A capture class containing a space captures a *sentence*.** The fast
path's app rule was `(?P<app>[\w .+-]{2,30})`, so *"open youtube on my chrome
browser"* was captured whole and launched as an application name. The only
thing limiting the damage was the length cap — at 28 characters it matched, and
the 35-character *"…on my **google** chrome browser"* fell through correctly.
A bug that depends on how many characters the user said is not a bug you can
reason about from the rule. The two existing guards both looked right and both
missed it: `_MULTI_CLAUSE` only knows `and`/`then`, and `_NOT_A_TARGET`
compared the **whole** capture against stopwords. A phrase guard has to be
**word-level** — `_NOT_IN_A_NAME` now rejects a capture containing any
preposition or pronoun, because no real application name has one.

**22. `Popen` succeeding is not the launch succeeding.** `open_application`
returned `f"Launched {app_name}."` on the next line after
`subprocess.Popen(["cmd", "/c", "start", "", target])`. Spawning `cmd` always
succeeds; `start` fails *inside the child*, asynchronously, and with
`CREATE_NO_WINDOW` there is no console for it to complain to — so Windows
raises a modal dialog the tool never learns about. That is why the user saw
*"Windows cannot find 'youtube on my chrome browser'"* next to *"Launched
YouTube on my Chrome browser."* Verify the target **before** spawning
(`_launchable`); never infer success from the fact that a process was created.
`send_whatsapp_message` already had this right via `_wait_for_window`.

**23. `winreg.QueryValue` cannot read `REG_EXPAND_SZ`.** The legacy call reads
only `REG_SZ` and raises `OSError(22, 'The data is invalid')` otherwise — and
`REG_EXPAND_SZ` is how Windows registers many of its own apps
(`%ProgramFiles(x86)%\Windows Media Player\wmplayer.exe`). `_resolve_executable`
swallowed that in a bare `except OSError: continue`, so installed programs
looked absent: `open_url(browser=…)` would silently use the default browser
while reporting the named one "not found". Use `QueryValueEx` plus
`os.path.expandvars`. Found only because the new launch gate reused the helper
and refused an app that was plainly installed.

**24. Esc is cooperative — you cannot interrupt a blocking call, so decouple the
UI instead.** A Python thread can't be killed and an in-flight HTTP/Whisper call
can't be aborted mid-request, so pressing Esc used to set the flag and then wait
out the whole call (up to the timeout) before the HUD reacted. The fix is
two-part: `_cancel_current` (shared by Esc and the corner watcher) makes the
*experience* instant — it bumps `_active_seq` so the worker is immediately stale
(its late reply is discarded by the same guard `_report_progress` uses) and drops
a terminal "Cancelled." on the HUD — while the *worker* frees its `_busy` slot
within a few seconds because the request timeout is now 20 s and
`gemini_client`/`quiz`/`computer_use` poll `cancellation.cancelled()` between
calls. Never try to make Esc "kill" the work; make the UI stop waiting on it.

**25. A corner cancel needs BOTH axes at an extreme — `and`, not `or`.** The
middle of a screen edge is a place the cursor sits constantly; only a true corner
(both x and y pinned) is an intentional gesture. `main._in_corner` uses `and`;
an `or` there cancels the running task on ordinary mousing. Guarded by
`test_concurrency.TestCornerDetection`.

**26. Cancellable code execution needs a subprocess, not `exec`.**
`accomplish_with_code` (the action-capable escape hatch) runs model-written
Python that may act on the machine. It runs the script as a child process so a
cancel can `terminate()` it — an in-process `exec` could not be interrupted, the
same wall Esc hits everywhere else. It also does **no** auto-repair (unlike
`solve_with_python`): re-running a script that may have already caused a side
effect could redo it. Output streams straight to `output.txt`, never a pipe, so
a chatty child can't deadlock on a full buffer.

**27. Self-modification lives in `tools/generated/`, never the core.**
`add_capability` lets Azleem write a *new tool into itself*. Every self-written
tool lands in the `tools/generated/` package, loaded there behind a `try/except`
guard (`llm_agent` imports it defensively; the loader skips any module that fails
to import). The hand-written core (`llm_agent.py`, `main.py`) is never edited by
Azleem. That isolation is the whole safety story: a bad generated tool degrades to
"no extra tools", it cannot brick startup, and rollback is deleting one file. Each
generated module follows a fixed convention — one typed `def`, a docstring, and
`TOOL = fn` + `ROUTING = "- name: …"`. No `from __future__ import annotations`
either (gotcha 1 applies to self-written tools too; guarded by the extended
`test_no_future_annotations_in_tools` and `TestSelfExtension`).

**28. A self-restart must be spawned DETACHED, or it kills itself mid-call.**
`restart.ps1`'s first act is `Stop-Process -Force` on the running Azleem. So
`self_restart.spawn_restart` launches it with `DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP` and a brief `Start-Sleep` (so the HUD paints its
"restarting" reply first). An in-process `subprocess.run` would stop the very
process making the call before the relaunch line runs, and nothing would come
back up. The singleton-mutex wait a naive self-restart would deadlock on is
already handled inside `restart.ps1` — don't reimplement it.

**29. `add_capability` is armed, not fired — like `power_action`.** It is a
two-step: the first call *writes and describes* the candidate tool; only a second
call with `confirm=True`, after the user actually says "confirm", installs and
restarts. A single mis-transcription cannot rewrite the codebase. And the tool is
*verified before it goes live* — on confirm the module is imported in a
subprocess, built into a Gemini function declaration, and run through
`tests/test_prompts`; any failure rolls the file back and refuses to restart
(`test_validation_failure_rolls_back_and_does_not_restart`,
`test_confirm_without_arming_never_restarts`). The subprocess import is
deliberately NOT the guarded package loader, which would swallow a broken module
and report success.

**30. Self-extension writes the *tool*, it cannot conjure *credentials*.** A
generated email/API tool still needs an account: the generator is told to read
secrets from environment variables and return the missing variable's name when it
is absent. "Azleem can grow a new tool" is a different claim from "Azleem can do
anything without setup" — say so honestly rather than implying the capability is
live when only the code exists.

**31. The overlay tick must idle when the HUD is hidden.** `_tick` used to
reschedule at 33 ms (~30 fps) *forever*, even hidden — a constant CPU wakeup
source that blocked deep idle and cost real battery, since the panel is hidden the
vast majority of the time. `_tick_delay_ms` now returns 33 ms only while visible
and 150 ms while hidden; a queued show/reply is still picked up within one slow
tick and snaps back to full speed. Guarded by `TestIdleTickThrottle`. (The audio
stream is already closed at idle and the resident Whisper model is a RAM cost, not
CPU — this tick was the one always-on CPU drain.)

**32. WhatsApp messaging no longer needs a saved number — but the cache stores
names, not numbers.** `send_whatsapp_message` tries a known/cached number first
(the instant deep link, unchanged) and otherwise falls through to
`tools/whatsapp.py`'s vision path: type the name into WhatsApp's search box,
`screen.locate` the matching contact row and click it, `screen.locate` the
message box, type, Enter — every step verified with `screen.wait_for_change`,
every failure reported honestly (gotcha 22 applies to each UI step). The WhatsApp
UI never exposes a phone number, so a UI-discovered contact is cached by *name*
only (canonical spelling seen on screen). That makes the next send more reliable
(exact search term) but does NOT let it skip to the deep link — only a real
number, from `.env` or a future capability, does. Don't advertise the cache as a
speed-up it isn't.

**33. Any new `_…PROMPT` constant is auto-discovered and must be registered.**
`test_prompts.test_every_model_facing_prompt_is_checked` globs every `*.py` in
the root and `tools/` for a `^_[A-Z0-9_]*PROMPT` assignment and fails if it is
not listed in `_model_facing_prompts()`. Promoting the quiz pointer to the shared
`screen.locate` created `screen._POINT_PROMPT`, and the WhatsApp bulk-capture
added `whatsapp._LIST_PROMPT`; both had to be added there or the guard-the-guard
test fails. This is deliberate — an unscreened prompt is one that could smuggle a
brand name past the contamination check (gotcha 2).

**34. On-screen text is DATA, not instructions to Azleem.** Confirmed from
`logs/azleem.log`: an assignment page said *"include a screenshot"*, the router
obeyed and called `take_screenshot` 7× — which `os.startfile`'d each PNG so image
windows popped up mid-task, and the distraction lost the paste target. Fixed two
ways: `take_screenshot` no longer opens the image (saves + returns the path only),
and both `_SYSTEM_PROMPT` and `computer_use._AGENT_PROMPT` state that a form's or
assignment's own text is content to work on, never commands to obey. Guarded by
`test_prompts` + `test_productivity`.

**35. Contact names are resolved, never taken literally.**
`tools/contacts.resolve_contact` maps a spoken name to a real saved contact:
relationship groups (mom/mum/mommy/mother; dad/daddy/father) normalise first,
then stdlib `difflib` fuzzy-matches against `WHATSAPP_CONTACTS` + the learnt cache
+ taught aliases. A clear winner → `("match", name)`; two close → `("ambiguous",
…)` and `send_whatsapp_message` ASKS instead of messaging the wrong person (the
user's #1 rule). Keep the user's spoken casing when the match is the same name;
switch to the saved spelling only when genuinely different (mom→Mum), else the
lower-cased `.env` keys give "Sent to alex". `link_contact_alias` teaches "my dad
is <name>".

**36. STT accuracy = model + hotwords + correction, and hotwords are dynamic.**
Whisper default is now `small.en` (`base` mangled proper nouns). `stt_engine`
biases decoding with `hotwords` built at load from the user's real contact names
+ a command vocab, and a conservative post-decode `_correct` map fixes name
mishears ("diabetes"→"Jarvis"). The contact hotwords are built dynamically (not a
static `_*PROMPT`), so they never trip the brand-contamination guard; a restart
refreshes them.

**37. Persisted history uses wall-clock, and is gated so tests don't write it.**
The rolling history saves to `logs/history.json` so it survives the self-restart.
Freshness is checked on load with `time.time()`, NOT `time.monotonic()` (which
resets each process and is meaningless across a restart). Persistence is a no-op
unless `self._history_file` is set — only `__init__` sets it, so the
`__new__`-built agents in the history tests never clobber the real file.

**38. The agency demo is proven offline, not live.** `perform_computer_task` now
runs to 25 steps and, once a step stalls (the oscillation guard trips), escalates
to a real thinking budget so it reasons out instead of aborting.
`tools/send_file.send_file_to_phone` finds a file and drives WhatsApp's
attach→pick→send UI, verified each step. Both have fakes-based tests; the real
proof is a spoken test on the user's actual WhatsApp/file dialog (gotcha 22
applies to every UI step). Fully autonomous job-application autopilot is NOT
claimed — this is the robust foundation, and it pauses before a final submit.

---

## Latest round — the screenshot bug, smarter contacts, better ears, real agency

Six asks; one had a logged root cause. See gotchas 34-38.

- **W1 — quiz "screenshot" break.** The model obeyed on-screen "include a
  screenshot" text and popped image windows, losing the paste target. Fixed at
  the routing level (gotcha 34).
- **W2 — fuzzy + semantic contacts.** `tools/contacts.py` resolver + confirm-when-
  unsure; `link_contact_alias` (gotcha 35).
- **W3 — transcription.** `small.en` + contact hotwords + correction map (gotcha
  36). New config `STT_CORRECTIONS`.
- **W4 — context across restarts.** History persists to `logs/history.json`
  (gotcha 37); `HISTORY_TURNS` 5→8, new `HISTORY_FILE`.
- **W5 — stronger agent + demo.** 25 steps, adaptive reasoning on stall, form
  guidance; `send_file_to_phone` (gotcha 38). New config
  `AGENT_STALL_THINKING_BUDGET`, `WHATSAPP_SELF_NAME`, `CONTACT_MATCH_THRESHOLD`,
  `CONTACT_MATCH_MARGIN`.

Verified: **378 offline tests** (up from 352); three mutations caught and restored
(take_screenshot auto-open, ambiguous-never-sends, stall-escalation).

Out of scope / honest limits: `small.en` adds a little STT latency (chosen over
`distil`/`large` to stay fast); the WhatsApp file-attach + self-chat flow and the
resolver's reliability depend on the user's real layout/contacts and need a live
spoken test; job-application autopilot is a foundation, not a finished product.

---

## Latest round — WhatsApp by name, and reaching for code when blocked

Two asks: stop making WhatsApp depend on a hand-written `WHATSAPP_CONTACTS` map —
Azleem should find the contact in the app itself and message them, and capture the
contact list into a store it manages — and let Azleem write its own code to get
past a barrier instead of replying that it can't.

- **WhatsApp by name (`tools/whatsapp.py`, new).** Gotcha 32. `send_whatsapp_message`
  is now cache/deep-link-first, else it drives WhatsApp's own UI by vision. Names
  it reaches are saved to a private, git-ignored cache
  (`logs/whatsapp_contacts.json`) so it learns the user's contacts itself.
  `capture_whatsapp_contacts` scroll-scans the chat list into that cache ("save my
  whatsapp contacts"), stopping when `wait_for_change` reports the list stopped
  moving (end-of-list sentinel).
- **Shared pointer.** The quiz's `_locate`/`_click_box` are now
  `screen.locate`/`screen.click_box`, reused by the WhatsApp path; `quiz.py`'s own
  tuned copies are left untouched to avoid regressing the measured quiz accuracy.
  New prompt constants `screen._POINT_PROMPT` + `whatsapp._LIST_PROMPT` registered
  (gotcha 33).
- **Self-unblocking (routing only).** `_SYSTEM_PROMPT`'s `accomplish_with_code`
  bullet went from a discouraged "LAST RESORT … never use it" to: when no
  dedicated tool fits an action the user wants done, write and run code *rather
  than giving up*. The rule lives in the system prompt because that is where the
  tool choice is made (gotcha 11). `add_capability` (permanent self-rewrites) keeps
  its spoken-"confirm" gate + rollback unchanged — by the user's choice, only
  one-off code is automatic, self-modification still confirms.
- **Config:** `WHATSAPP_CONTACT_CACHE`, `WHATSAPP_SEND_TIMEOUT`,
  `WHATSAPP_SETTLE_SECONDS`, `WHATSAPP_MAX_SCROLLS` (the send timings were
  hardcoded before).

Verified: **352 offline tests** (up from 337), and the mutation on the
send-confirmation guard was caught — forcing `sent = True` made
`test_unconfirmed_send_is_not_claimed_as_success` fail, then restored.

Out of scope / honest limits: the UI path's reliability depends on the vision
model locating the search box, the right contact row and the message box on the
user's actual WhatsApp layout — proven offline with fakes, but the real proof is
a live spoken test only the user can run. The cache remembers names, not numbers,
so repeat sends still drive the UI (gotcha 32).

---

## Latest round — self-extension, and a quieter idle

Two asks: Azleem should be able to **write its own code to edit its own codebase**
when a task exceeds its tools (the example: emailing someone), gaining the
capability *permanently*; and it was suspected of **draining the laptop battery**,
which needed optimising and attributing.

- **Self-extension (`add_capability`).** Gotchas 27–30. A new tool that writes a
  fresh tool module into the isolated `tools/generated/` package, verifies it
  (subprocess import + Gemini-schema build + `tests/test_prompts`), and — gated
  behind a spoken "confirm", with automatic rollback on failure — restarts Azleem
  detached to load it. The original request is carried across the restart in
  `logs/pending_command.json` and run automatically on startup
  (`main.dispatch_pending`), so the new capability is used immediately. New files:
  `tools/self_extend.py`, `tools/generated/__init__.py`, `self_restart.py`. The
  core edit to `llm_agent.py` is minimal and guarded: fold `tools.generated.TOOLS`
  and `.ROUTING` into the per-request tool list and prompt, register
  `add_capability`, add one confirm-gated routing bullet. `main._process` was
  refactored into `_begin_worker` + `_answer` so a text command (the pending one)
  reuses the exact command path minus transcription.
- **Idle battery.** Gotcha 31. The 30 fps overlay tick now idles at ~7 Hz while
  hidden. Everything else was already power-clean. For zero idle cost with no
  visual HUD, `SHOW_OVERLAY=false` swaps in `_NullOverlay` (blocks on an event,
  no tick at all).

Verified: **337 offline tests** (up from 313), the real validation gate exercised
both ways against an actual generated module (clean → `(True, '')`, syntax error →
refused), three mutations caught and restored (overlay idle interval, the
validation-failure rollback, arm-must-not-install). Config: `SELF_EXTEND_ENABLED`
(kill switch) and `SELF_EXTEND_TEST_TIMEOUT`.

Out of scope / honest limits: a generated tool that *runs* is not proof it
*works* live (credentials, gotcha 30); the pending command re-run uses the model's
paraphrase of the request, not the raw utterance; and self-extension is a genuine
power tool — the confirm gate and rollback are what make it safe, not a claim that
the generated code is always correct.

---

## Latest round — responsiveness, paid Gemini, and a code escape hatch

Reported: "what's on my screen" thought for ~a minute; Esc took 5–30 s (or never)
to cancel; and two feature asks — a code-writing escape hatch for tasks no tool
covers, and a cursor-to-corner cancel gesture. Now on paid Google AI Studio
credits.

- **R16 — instant screen-read.** "what's on my screen" wasn't fast-pathed, so it
  paid two Gemini routing round-trips + a vision call. `intents.py` gained the
  one screen-touching fast path — a closed, anchored set of bare phrasings
  ("what's on my screen", "read my screen") dispatched straight to `read_screen`.
  Anything with a target, a second clause or a pronoun fails the anchor and falls
  through (gotcha 20). Refusals in `test_intents.TestReadScreen` outweigh matches.
- **R17 — rebalanced chain + Gemini-first vision.** The `gemini-3.x` IDs are
  real (verified against live `models.list()` — the earlier "fictional" worry was
  a stale knowledge cutoff). Chain is now fastest-first with a `gemini-pro-latest`
  escalation mid-chain and lite aliases last. Vision flipped to `gemini` first
  with **free OpenRouter kept as a genuine fallback** (`providers.vision_generate`
  restructured; `VISION_PROVIDER=gemini` no longer means "gemini only"). Request
  timeout cut 60 s → 20 s.
- **R18 — instant Esc + corner cancel.** Gotchas 24–25. Esc and a screen-corner
  gesture share `_cancel_current`; the overlay also slides to the opposite slot
  when the cursor nears it (`overlay._avoid_cursor`).
- **R19 — `accomplish_with_code`.** Gotcha 26. A last-resort tool (kept distinct
  from `solve_with_python`) that writes and runs system-acting Python in a
  cancellable subprocess. Arbitrary code execution, by the user's explicit
  choice, bounded by subprocess isolation + a 60 s timeout + saved scripts.

Verified: **313 offline tests** (up from 305), four mutations caught and
restored (corner `and`→`or`, the cancel seq-bump, the subprocess cancel check,
the read-screen rule), build stamps matched after `restart.ps1`, and the live
banner shows `vision: gemini -> openrouter fallback`.

Out of scope, unchanged: the free-tier quiz burn is moot on paid credits; Esc
during a single in-flight request still waits out that one request (capped at
20 s now) — only the chain-walk after it is cut short.

---

## Commands

```bash
python main.py --selftest    # 378 offline tests, no network, nothing launched
```

```bash
python tests/bench.py        # pipeline-stage latency vs budgets
```

```bash
python tests/stress.py --soak   # every capability: latency, model-call and screenshot counts
```

```bash
python -m build_info         # fingerprint of the source on disk
```

```bash
python main.py --check-mic   # 4s mic diagnostic
```

Live routing tests (spend API quota, opt-in):

```bash
AZLEEM_LIVE_TESTS=1 python -m unittest tests.test_routing_live -v
```

---

## Tests

| File | Covers |
|---|---|
| `test_prompts.py` | Brand contamination, required directives, on-screen-question routing, all 12 tool schemas build, no `__future__` annotations, build fingerprint |
| `test_reply_summary.py` | Reply budgeting — the outcome survives the HUD's 220-char cut |
| `test_screen_change.py` | The change detector; the defect that corrupted quiz answers |
| `test_quiz.py` | The answer/point/click/verify loop, with a fake mouse |
| `test_whatsapp.py` | By-name send: cache, known/cached/UI routing, the vision search→click→send loop, bulk contact capture, and fuzzy/relationship contact resolution with confirm-when-unsure — fake mouse + fake vision |
| `test_contacts` (in test_whatsapp) | Relationship aliases (mom↔mum), difflib fuzzy match, ambiguous→asks-not-sends, taught aliases |
| `test_productivity.py` | take_screenshot saves quietly and never opens the image (the on-screen-instruction bug) |
| `test_stt.py` | Transcription correction map + contact hotword biasing (offline, no model load) |
| `test_computer_use.py` | Adaptive reasoning: a stalled step escalates the thinking budget |
| `test_send_file.py` | send_file_to_phone: find file, attach→send flow, honest failures — fake mouse + fake vision |
| `test_openrouter.py` | Payload translation, fallback, and the pointing/vision model split |
| `test_quiz_live.py` | **Opt-in:** real models, five layouts, measured click accuracy |
| `test_open_url.py` | URL resolution, manglings, deep links, search encoding, scheme safety |
| `test_overlay.py` | HUD state machine with a fake clock; `thinking` always terminates |
| `test_concurrency.py` | Busy slot, stale progress, recorder races, frame cap |
| `test_routing_live.py` | Real model routing with **non-executing** tool twins (opt-in) |
| `bench.py` | Per-stage latency vs budgets, beam-size sweep |
| `stress.py` | All 13 capabilities: latency budgets, plus model-call/screenshot counts |
| `test_intents.py` | The local fast path, and everything it must refuse to match |
| `test_restart.py` | Which processes `restart.ps1` stops — drives the real script via `-DryRun -ProcessSource`, weighted towards what must survive |
| `LIVE_CHECKLIST.md` | 15 spoken commands for manual verification |

**Tests are mutation-verified.** When claiming a test proves a fix, revert the
fix and confirm it fails. Three mutations were checked: removing the watchdog,
undoing the reply `deiconify`, and restoring the old busy-check order — each
made the right tests fail.

Live routing tests use `functools.wraps` twins that record arguments and return
a canned string, so routing is tested without launching anything.

---

## Latest round — the fast path swallowed a sentence

Reported: *"open youtube on my chrome browser"* produced a Windows error box
— **"Windows cannot find 'youtube on my chrome browser'"** — while the HUD
said **"Launched YouTube on my Chrome browser."** Nothing opened, and Azleem
claimed it had. Two independent defects, gotchas 21–23.

The routing had been **correct before `intents.py` existed** (`spec.md` records
a live run producing `open_url(url='youtube', browser='chrome')`), and
`_SYSTEM_PROMPT` addresses this command by name — *"'open YouTube in Chrome' is
a single open_url call"*. The fast path intercepted before that prompt was ever
read. Gotcha 11 again, from the other direction: a rule is irrelevant if the
decision has already been made somewhere else.

- **R13 — the misroute.** `intents.py` gained a site-plus-browser rule
  (`open <known site> on/in <known browser>` → `open_url`, both halves closed
  sets), and `_open_app` gained the word-level `_NOT_IN_A_NAME` guard. Fixed
  three commands in the same class as a side effect: *"open gmail in edge"*,
  *"open youtube in chrome"*, *"open my downloads folder"* — all were being
  handed to `cmd /c start` as application names.
- **R14 — the false success.** `open_application` now resolves the target
  through `_launchable` before spawning anything, and refuses honestly instead
  of reporting a launch it never observed. A site name arriving here now falls
  back to `open_url`, mirroring the URL fallback that was already there.
- **R15 — `_resolve_executable` fixed** (gotcha 23). Not in the original plan;
  found because R14 reused the helper and it refused Windows Media Player,
  which is installed. It had been silently failing for every `REG_EXPAND_SZ`
  App Paths entry, which also affected `open_url`'s `browser=` targeting.

Verified: 198 offline tests (up from 188), **four mutations caught** — one of
which initially wasn't. The registry test called `skipTest` when
`_resolve_executable` returned `None`, but `None` means *both* "not installed"
and "the lookup is broken", so the mutation was masked by a skip rather than
caught by a failure. It now establishes the app's existence by reading the
registry independently, and skips PATH-resolvable entries so the registry
branch is actually the code under test. A test that can skip its way past the
bug it guards is not a guard.

---

## Previous round — quizzes that actually work, and a second provider

Reported: against a real 16-question MCQ site, Azleem answered 3 and got 1
right, ending in *"encountered a loop"*. Root cause was gotcha 14 — the
screen-change detector is blind on text pages, so the agent was told every
action had done nothing and **overwrote answers it had already got right**.

- **R10 — `screens_differ` rewritten** to count moved pixels instead of
  averaging them (`screen.changed_fraction`), with an optional `region` so a
  click can be verified on the element it landed on.
- **R11 — `tools/quiz.py` / `answer_quiz`.** A dedicated loop: answer call →
  point call → click → verify → advance, anchored to option *text* rather than
  any layout. Never clicks a final Submit. Flags anything it isn't confident
  about, and always flags "recently announced" questions — those are past every
  model's training cutoff, which is exactly what this quiz asks.
- **R12 — OpenRouter as a second provider.** `openrouter_client.py` +
  `providers.py`. Vision goes to free models there first, Gemini as fallback;
  routing stays on Gemini, which routes measurably better. Two model chains
  (gotcha 16).

Measured live across five layouts (radio list, card grid, dark row, true/false,
numbered rows): **5/5 answers correct, 5/5 clicks landing on the right option**
— and mid-run the free models rate-limited and it fell through to Gemini without
dropping a question. 165 offline tests, ten mutations caught.

Still open by agreement: Whisper mangling (*"Solve the"* → *"So,"*) and
`spec.md` known gap 4c (Esc only aborts promptly inside the computer-use loop;
`answer_quiz` polls it too, between questions).

---

## Earlier round — on-screen quiz routing

Reported: *"solve the quiz question on my screen and move to the next one"* did
neither. `logs/azleem.log` shows `read_screen` → `open_url` (a **Google search
for the question**) → `read_screen` (the results page) → `perform_computer_task`
typed an answer → done. Four defects, see gotchas 11–13:

- **R7 — Web-searched an on-screen question.** The rule forbidding it lived only
  in `_AGENT_PROMPT`; routing happens in `_SYSTEM_PROMPT`. Lifted it up, plus a
  ban on navigating away from the screen the task refers to.
- **R8 — Second clause dropped.** The unconditional no-Submit rule blocked the
  quiz's Next control. Now scoped: coursework submissions stay guarded, an
  explicit "submit / move on" in the task overrides, and a task with more than
  one clause is not done until every clause is visibly satisfied.
- **R9 — Unreadable reply.** `_summarise` budgets the reply to the HUD's window
  and drops read-only results once a side-effect tool has run. On the logged
  run this took the visible text from *"Which international organization
  recently announced…"* (the question, echoed back) to *"Opened … The answer
  'Union for the Mediterranean' has been typed into the provided field…"*.

Verified: 91 offline tests, six mutations caught, build stamps matched after
`restart.ps1`, and the live routing test passed on `gemini-3.5-flash-lite` —
one `perform_computer_task` call carrying *"click the next button to move to
the next one"*, no `open_url`.

Out of scope by agreement, still open: free-tier quota burn (`FEATURES.md` #4),
Whisper mangling *"Solve the"* → *"So,"*, and `spec.md` known gap 4c (Esc only
aborts promptly inside `perform_computer_task`).

---

## What was done in the previous round

Full traceability matrix in **`spec.md`**. Summary:

- **R1 — MKBHD substitution.** Branded few-shot examples in `_SYSTEM_PROMPT` and
  the `perform_computer_task` docstring were in context on every request.
  Replaced with neutral ones + an anti-substitution rule.
- **R2 — Stuck "Thinking".** Four independent bugs: busy-drop path never
  replied; stale progress callbacks repainted after a reply; the `reply` branch
  never called `deiconify()` so a hidden reply never expired; no watchdog.
- **R3 — Speed.** There was **no URL tool at all** — every website command went
  through a 12-step vision loop. Added `open_url`. Plus configurable
  `WHISPER_BEAM_SIZE`, `AGENT_SETTLE_SECONDS`, a missing `subprocess` timeout,
  and the WhatsApp fixed `sleep(6)` replaced with a poll.
- **R4 — Stress testing.** 65 offline tests, mutation-verified, plus a benchmark
  and a manual checklist.
- **R5 — `FEATURES.md`,** 10 ranked suggestions.
- **R6 — Staleness guard.** `build_info.py` + `restart.ps1`.

Corrections worth remembering: `beam_size=1` is ~12% faster (not the "2–3×"
first estimated), and the old end-to-end path was never actually clocked — only
inferred from the step count.

---

## Working style that paid off here

- **Verify from outside your own claims.** "The code is correct" and "the fix is
  running" are different statements. The second is what the user experiences.
- **Mutation-test.** A passing test proves nothing until you've seen it fail.
- **Check the fixture before trusting the measurement.** The noise-vs-speech
  benchmark inverted a real result.
- **Run the real script, not a re-typed approximation.** A bash-escaped
  transcription of `restart.ps1`'s matching logic looked like it would kill the
  user's other apps; the actual script was correct.
