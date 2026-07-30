# Azleem — voice-controlled desktop assistant

Hold **Ctrl+Space**, speak, release. Azleem transcribes locally, decides what
you meant, and does it — launching apps, finding files, setting alarms, taking
notes, solving coding problems, or driving your screen click by click.

Runs on Windows 11. Starts automatically when you log in.

---

## Quick reference

| | |
|---|---|
| **Talk to it** | Hold `Ctrl+Space` — mic opens instantly (HUD appears), speak, release |
| **Cancel a running task** | Press `Esc` |
| **Emergency stop** | Slam the mouse into a screen corner (aborts clicking) |
| **Log file** | `logs/azleem.log` |
| **Settings** | `.env` in this folder |
| **Stop it** | Task Manager → end the `pythonw.exe` running `main.py` |
| **Start it manually** | `python main.py` |

---

## What it can do

Ask in plain speech; Azleem picks the right capability.

**Apps and files**
- Launch applications — *"open Chrome"*, *"open Notepad"*
- Find and open files by partial name across Downloads, Documents and Desktop —
  *"open my resume"*

**Screen control**
- Take a screenshot (saved to Pictures) — *"take a screenshot"*
- Read text off the screen — used internally to understand exercises, questions
  and errors before acting
- Click one thing — *"click the submit button"*
- **Autonomous multi-step tasks** — *"find the latest MKBHD video on YouTube"*.
  Screenshots the screen, decides one action, performs it, looks again; up to
  12 steps. It can click, double-click, type, navigate to URLs, press keys and
  scroll.

**Productivity**
- Alarms via Windows Task Scheduler, surviving reboots — *"set an alarm for
  7am"*. Fires a popup window (`alarm_popup.pyw`) even if Azleem isn't running.
- Calendar events as `.ics` files that open in your default calendar app —
  *"add a dentist appointment tomorrow at 3pm"*
- Notes written to `Documents\Azleem Notes` and opened in Notepad —
  *"take a note: buy milk"*
- WhatsApp messages to known contacts (needs `WHATSAPP_CONTACTS` in `.env` —
  currently empty)

**Coding**
- *"solve this coding exercise and put the link in the submission field"*
- Writes a Python script, runs it locally (30s limit), repairs it once if it
  crashes, then uploads **solution.py + output.txt + solution.ipynb** as a
  secret GitHub gist and returns a **Google Colab link**
  (`colab.research.google.com/gist/...`) that opens the notebook directly —
  which is what course platforms asking for a "shared Colab notebook" accept.
- It will type the link into a submission field for you, but **never clicks the
  final Submit button** — you review and submit.

---

## The HUD

A dark graphite glass panel, bottom-centre, click-through and non-focus-stealing
so it can't intercept your typing or Azleem's own clicks.

| State | Shows |
|---|---|
| **Listening** | Live waveform driven by real microphone level, elapsed timer. A flat line genuinely means no audio — it warns about a muted mic. |
| **Thinking** | Animated ellipsis plus a live detail line streaming what the agent is doing right now (*"Step 3/12: clicking the search box"*). |
| **Reply** | Azleem's actual answer, auto-dismissing after a reading-time delay. |

---

## Architecture

| File | Role |
|------|------|
| `main.py` | Entry point: push-to-talk lifecycle, hotkey hook, Esc cancel, single-instance guard, headless logging |
| `overlay.py` | The HUD (tkinter). Thread-safe queue API; Tk owns the main thread |
| `stt_engine.py` | Microphone capture (sounddevice) + local transcription (faster-whisper) |
| `llm_agent.py` | System prompt, tool registration, routing via Gemini function calling |
| `gemini_client.py` | Shared client, model fallback chain, cooldowns, network retry |
| `config.py` | `.env` loading and settings |
| `cancellation.py` | Cooperative cancel flag shared by long-running tools |
| `speaker_mute.py` | Mutes the speakers while listening so the mic hears only you |
| `alarm_popup.pyw` | Standalone alarm window launched by Task Scheduler |
| `diagnose_keys.py` | Raw keyboard-event logger for debugging hotkey problems |
| `tools/os_tools.py` | File search, app launching, WhatsApp |
| `tools/productivity.py` | Screenshots, alarms, calendar, notes |
| `tools/vision_tools.py` | `read_screen`, single-click `execute_screen_task` |
| `tools/computer_use.py` | The autonomous screenshot→decide→act loop |
| `tools/coding.py` | Script generation, local execution, gist/Colab publishing |
| `tools/screen.py` | Shared capture, DPI coordinate math, screen-diff, JSON parsing |

**Threads:** Tk event loop on the main thread; the pynput keyboard hook on its
own thread; a timer thread for the hold gate; one worker thread per command.

---

## Configuration (`.env`)

| Key | Default | Notes |
|-----|---------|-------|
| `GEMINI_API_KEY` | — | Required. Free key from aistudio.google.com/apikey |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Primary model for routing and vision |
| `GEMINI_FALLBACK_MODELS` | *(built-in chain)* | Comma-separated override |
| `GITHUB_TOKEN` | — | Gist scope. Enables shareable Colab/gist solution links |
| `HOTKEY` | `caps_lock` | **Set to `ctrl_space`** — see gotchas |
| `HOLD_SECONDS` | `0.25` | Hold time before the mic opens. Keep small — see gotchas |
| `SHOW_OVERLAY` | `true` | `false` runs headless without the HUD |
| `WHISPER_MODEL` | `base` | `tiny`…`large-v3`; bigger = more accurate, slower |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` | `cpu` / `int8` | `cuda`/`float16` for NVIDIA GPUs |
| `DOWNLOADS_DIR` | `%USERPROFILE%\Downloads` | File-search root |
| `WHATSAPP_CONTACTS` | `{}` | JSON map of name → E.164 phone number |

`.env` holds secrets and is gitignored. Never commit it.

---

## Auto-start

A shortcut at
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Azleem.lnk`
runs `pythonw.exe main.py` (no console window) from this folder at login.

- **To disable:** delete that shortcut.
- **To re-create it:** run PowerShell —
  ```powershell
  $s=[Environment]::GetFolderPath('Startup');$w=New-Object -ComObject WScript.Shell;$l=$w.CreateShortcut("$s\Azleem.lnk");$l.TargetPath="C:\Users\aleem\AppData\Local\Programs\Python\Python314\pythonw.exe";$l.Arguments="main.py";$l.WorkingDirectory="C:\Users\aleem\JARVIS";$l.Save()
  ```

Because there is no console under `pythonw`, all output goes to
`logs/azleem.log`. A **named mutex** prevents a second copy from running — if
you launch it manually while the auto-started copy is alive, the new one exits
immediately rather than creating a second keyboard hook.

---

## Gotchas (hard-won — please don't undo these)

**Never add `from __future__ import annotations` to any `tools/*` module.**
It turns type hints into strings, and google-genai's automatic function calling
does `isinstance(value, annotation)` when invoking a tool. Every tool call then
fails with *"isinstance() arg 2 must be a type"* — and some models cheerfully
report success anyway. This silently broke every tool for a whole session.

**Caps Lock as a hold-to-talk key is unreliable.** Some keyboards report it as
an instant toggle (key-down and key-up together) no matter how long it's held,
which cancels the hold timer instantly. `HOTKEY=ctrl_space` is the working
configuration. `diagnose_keys.py` exists to test this on any machine.

**Free-tier Gemini quota is the main performance limit.** `gemini-3.6-flash`
allows ~5 requests/minute and 20/day per model. The router walks a seven-model
chain, applies cooldowns, prefers the last model that worked, and honours the
API's own `retryDelay`. Expect "rate limited; trying next model" in the log —
that is normal, not a bug.

**The SDK has no default HTTP timeout.** Without the explicit 60s timeout in
`gemini_client.py`, one wedged connection freezes the whole assistant forever.

**Network errors are retried, not failed over.** Every model shares one host,
so a DNS blip gets two quick same-model retries; a real outage returns a plain
"I can't reach the internet" instead of `getaddrinfo failed`.

**Console output must stay line-buffered.** Otherwise the log looks frozen
mid-command when redirected to a file.

**The computer-use loop needs its feedback signals.** Two mechanisms stop it
looping forever: each step is told whether the screen actually changed, and
repeated *no-op* actions trip a warning then an abort. Repetition that visibly
works (scrolling down a long page) is explicitly allowed.

**Scrolling sends individual wheel detents,** not one large delta — some pages
ignore the latter. When scrolling doesn't move the screen, the agent is told to
press `End`/`PageDown` instead.

**`HOLD_SECONDS` must stay small.** It was 2.0 to protect Caps Lock's normal
behaviour, but with `ctrl_space` that gate is pure harm: people start speaking
the instant they press, so their command lands in the dead zone and is thrown
away. The take then contains only background audio — which is how Azleem ended
up answering the narration of a YouTube video instead of the user.

**The speakers are muted while the mic is open** (`speaker_mute.py`), because
anything playing through them is transcribed as if the user said it. Two rules
in that module, both learned by breaking them: open the microphone *before*
muting (COM calls cost milliseconds that would otherwise delay capture), and
never decide *whether* to unmute by reading the device — `GetMute()` can return
a stale value right after a `SetMute()`, and an earlier version misread its own
mute as the user's, refused to undo it, and left the speakers silent. It now
records the prior state and drives the device back to it explicitly, with a
verified read-back and one retry.

**Whisper mishears app names** ("Open Nutspad", "Open Note 5"). Two mitigations:
a vocabulary hint in `stt_engine.py`, and the system prompt telling Gemini to
expect mangled proper nouns and infer intent. Also, Whisper's VAD discards
quiet push-to-talk takes, so the code retries unfiltered when VAD finds nothing
but the audio clearly wasn't silence.

---

## Tests

Offline suites (no network, no real clicks, never touching real speakers) live
in the session scratchpad, not the repo: hotkey release ordering and speaker
muting (11), computer-use loop (15), tool layer (12), network handling (4). All
42 pass.

Live diagnostics that ship with the project:

```bash
python main.py --check-mic     # is the microphone actually delivering audio?
python diagnose_keys.py        # raw key events — for hotkey problems
```

---

## Known limitations

- WhatsApp is unconfigured (`WHATSAPP_CONTACTS` is empty) and never tested with
  a real message.
- Screen automation targets the primary monitor only.
- `solve_with_python` executes model-written code on your machine at your
  spoken request, bounded by a 30s timeout and confined to its own solution
  folder. Both the code and its output are saved for inspection.
- Heavy data-science coursework (pandas/matplotlib against a remote dataset)
  produces a plausible notebook, but open the Colab link and Run All before
  submitting real coursework.
