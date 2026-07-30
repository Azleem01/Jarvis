# Azleem — Push-to-Talk Desktop Assistant (Windows)

A modular, local-first voice assistant for Windows. Hold a hotkey, speak a
command, and Azleem transcribes it **on your machine** (zero API cost, private),
routes the intent through the free **Google Gemini** API, and executes OS or
vision-based automation.

```
Hold Caps Lock ──▶ record ──▶ faster-whisper (local STT)
                                     │
                                     ▼
                         Gemini (function calling)
              ┌──────────────┬──────────────┬─────────────────┐
     open/search files   launch apps   WhatsApp message   click on screen
```

## Features / Tools

| Tool | What it does |
|------|--------------|
| `search_and_open_file` | Finds a file by partial name in Downloads (or another folder) and opens it. |
| `open_application` | Launches apps by common name: Chrome, WhatsApp, Notepad, Explorer, media player… |
| `send_whatsapp_message` | Opens a WhatsApp deep link to a known contact and auto-sends the message. |
| `execute_screen_task` | Screenshots the screen, asks Gemini Vision to locate a UI target, and clicks it. |

## Project layout

```
main.py              Push-to-talk lifecycle, hold-gate, --check-mic diagnostic
overlay.py           Blue recording HUD (tkinter), driven by real mic level
stt_engine.py        Mic capture (sounddevice) + local transcription (faster-whisper)
llm_agent.py         System prompt, tool registration (function calling)
gemini_client.py     Shared Gemini client + automatic model fallback / cooldowns
config.py            Loads .env, exposes settings, validates the API key
tools/os_tools.py    File search, app launching, WhatsApp
tools/vision_tools.py Screen capture, Gemini Vision coords, pyautogui clicks
.env.example         Configuration template
requirements.txt     Dependencies
```

## Setup

Requires **Python 3.10+** on **Windows**.

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
copy .env.example .env
#   then edit .env and set GEMINI_API_KEY
```

Get a **free Gemini API key** at <https://aistudio.google.com/apikey>.

> **First run downloads the Whisper model** (`base` ≈ 140 MB) automatically and
> caches it. Later runs start fast. Use a smaller model (`tiny`) for speed or a
> bigger one (`small`/`medium`) for accuracy via `WHISPER_MODEL` in `.env`.

## Run

```powershell
python main.py
```

Then:

1. **Hold Caps Lock.** After `HOLD_SECONDS` (2s by default) the mic opens and a
   blue HUD appears at the bottom of the screen — that's your confirmation it's
   live. A quicker tap is ignored, so Caps Lock still works as Caps Lock.
2. **Keep holding and speak**, e.g. *"open notepad"*, *"search downloads for
   invoice"*, *"message mum I'm on my way"*, *"click the submit button"*. The
   waveform is driven by your actual mic input, so it moves when you talk.
3. **Release** Caps Lock — the HUD hides, then Azleem transcribes, decides, and
   acts. The transcript and reply print in the console.

The HUD is click-through and never takes focus, so it can't steal your typing or
intercept the clicks `execute_screen_task` makes.

### Is the mic actually working?

```powershell
python main.py --check-mic
```

Records 4 seconds, reports the input device and signal level, then transcribes.
It tells you whether the mic is dead or merely didn't pick up words — from the
outside those look identical.

Move the mouse to any screen corner to trigger the pyautogui **fail-safe** and
abort an in-progress click. Press **Ctrl+C** in the console to quit.

## Configuration (`.env`)

| Key | Default | Notes |
|-----|---------|-------|
| `GEMINI_API_KEY` | — | **Required.** Free key from AI Studio. |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Primary model, used for routing **and** vision. |
| `GEMINI_FALLBACK_MODELS` | *(built-in chain)* | Comma-separated models to fall back to. See below. |
| `MODEL_COOLDOWN_SECONDS` | `60` | How long to skip a model after it reports quota exhaustion. |
| `HOTKEY` | `caps_lock` | `caps_lock` (hold) or `ctrl_space` (hold Ctrl+Space). |
| `HOLD_SECONDS` | `2.0` | How long to hold before the mic opens. Shorter taps are ignored. |
| `SHOW_OVERLAY` | `true` | Show the blue recording HUD. `false` to run headless. |
| `WHISPER_MODEL` | `base` | `tiny`/`base`/`small`/`medium`/`large-v3`. |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you have an NVIDIA GPU. |
| `WHISPER_COMPUTE_TYPE` | `int8` | `int8` (CPU) or `float16` (GPU). |
| `DOWNLOADS_DIR` | `%USERPROFILE%\Downloads` | Folder for file search. |
| `WHATSAPP_CONTACTS` | `{}` | JSON name→phone map, e.g. `{"mum":"+15551234567"}`. |

WhatsApp needs the **WhatsApp desktop app** installed (it handles the
`whatsapp://` deep link) and phone numbers in **E.164** format.

## Model fallback (never pinned to one model)

Free-tier keys hit per-minute and per-day quotas constantly, and individual
model IDs get retired without notice. Azleem therefore never depends on a single
model: every request walks an ordered chain until one answers.

```
gemini-3.6-flash → gemini-3.5-flash → gemini-3-flash-preview
   → gemini-3.5-flash-lite → gemini-3.1-flash-lite
   → gemini-flash-latest → gemini-flash-lite-latest
```

Every model in the chain supports both **function calling** and **vision**, the
two things Azleem needs. The `-lite` and `-latest` aliases sit at the end because
they draw on separate quota pools, so they're still likely to work once the
numbered models are exhausted.

Azleem switches to the next model on:

| Error | Meaning |
|-------|---------|
| `429 RESOURCE_EXHAUSTED` | Rate limited or out of quota |
| `404 NOT_FOUND` | Model retired or not enabled for your key |
| `500` / `502` / `503` / `504` | Transient server-side trouble |

It deliberately does **not** fall back on `401`/`403` (bad key) or `400`
(malformed request) — those fail identically on every model, so retrying would
just hide the real problem.

Two refinements worth knowing:

- **Cooldowns.** A rate-limited model is skipped for `MODEL_COOLDOWN_SECONDS`
  (or the API's own `retryDelay` if it sends one), so the next command doesn't
  pay the same 429 again. The model that last worked is tried first. Once every
  model is cooling down they're retried anyway, soonest-to-recover first — a
  stale cooldown is better than refusing to answer.
- **No repeated side effects.** Azleem's tools *do things* — launch apps, send
  messages, click the screen. If the API fails on the follow-up turn, after a
  tool has already run, retrying on another model would perform the action
  twice. So tool calls are tracked and fallback stops once anything has
  executed; Azleem reports what actually happened instead of repeating it.

Override the chain in `.env` if you'd rather choose yourself:

```ini
GEMINI_MODEL=gemini-3.5-flash
GEMINI_FALLBACK_MODELS=gemini-3.6-flash,gemini-flash-latest
```

To see what your own key can reach:

```powershell
python -c "import config; from google import genai; [print(m.name) for m in genai.Client(api_key=config.GEMINI_API_KEY).models.list()]"
```

## How it handles Windows display scaling

`execute_screen_task` captures the screen at **physical** resolution via `mss`,
asks Gemini for coordinates in its normalised 0–1000 space, then converts them
to physical pixels and finally divides by the physical-to-logical ratio so the
click lands correctly at **100% / 125% / 150%** scaling. The process is marked
DPI-aware at startup so the screenshot and the desktop align.

## Caps Lock note

Windows toggles Caps Lock on key-down, and `pynput` can't cleanly suppress that
without swallowing every other key too. Azleem therefore lets the toggle happen
and **resets it on release**, so your caps state is preserved. Two details make
that safe:

- The reset is only applied when a recording actually happened. A tap under
  `HOLD_SECONDS` is left completely alone.
- Resetting means injecting a synthetic Caps Lock tap, which the keyboard hook
  would otherwise see as a *new* keypress and start a phantom recording. Azleem
  marks those injected events and ignores them (with a timed clear, so a
  swallowed event can't strand the guard and eat your next real keypress).

If you'd rather leave Caps Lock entirely alone, set `HOTKEY=ctrl_space` in `.env`.

## Why transcription retries

Whisper's VAD (voice-activity detection) is tuned for continuous audio streams
and regularly discards short or quietly-recorded push-to-talk takes as
"no speech" — which shows up as Azleem hearing nothing at all. Since you held a
key and spoke, throwing that audio away is the worst outcome, so Azleem runs VAD
first and, if it yields nothing while the take clearly wasn't silence, retries
unfiltered. True silence still short-circuits, which keeps Whisper from
hallucinating phrases like "Thank you." into an empty room.

## Smoke-testing without a mic

```powershell
python -c "from tools import os_tools; print(os_tools.open_application('notepad'))"
python -c "from tools import os_tools; print(os_tools.search_and_open_file('report'))"
python -c "import stt_engine, llm_agent; from tools import vision_tools; print('imports OK')"
```

## Troubleshooting

- **`GEMINI_API_KEY is not set`** — copy `.env.example` to `.env` and add your key.
- **`All Gemini models are unavailable`** — every model in the chain is rate
  limited or out of quota. Check usage at <https://aistudio.google.com/apikey>;
  free-tier daily quota resets at midnight Pacific.
- **`[gemini] <model> unavailable (...); trying next model`** — informational,
  not an error. Fallback is working; your command still ran on another model.
- **No audio / `PortAudioError`** — check your default input device; `sounddevice`
  uses the system default microphone.
- **First transcription is slow** — the Whisper model is downloading/loading; it's
  cached afterward.
- **Clicks land in the wrong place** — confirm you're on the primary monitor;
  multi-monitor targeting uses monitor 1.
- **WhatsApp doesn't send** — ensure the desktop app is installed and the contact's
  number is in `WHATSAPP_CONTACTS`. Increase the wait in `os_tools.py` on slow PCs.

## Safety

- `pyautogui.FAILSAFE = True` — slam the mouse to a corner to abort automation.
- WhatsApp auto-send only fires for a **known** contact you configured.
- All speech recognition runs locally; only the transcribed text (and, for
  screen tasks, a screenshot) is sent to Gemini.
```
