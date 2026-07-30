# AZLEEM — handoff / debugging brief

**Status: the app starts, the Gemini layer is verified working, and the HUD has
been screenshotted rendering on screen — but the user has never once
successfully triggered it with a physical Caps Lock press.**

This document is for a second opinion on the one remaining failure. It records
what was changed, what was actually verified (and how), and what remains
unexplained. Written 2026-07-25.

---

## 1. What the project is

A Windows push-to-talk desktop assistant.

```
Hold Caps Lock ──▶ record ──▶ faster-whisper (local STT)
                                    │
                                    ▼
                        Gemini (function calling)
             ┌──────────────┬──────────────┬─────────────────┐
    open/search files   launch apps   WhatsApp message   click on screen
```

| File | Role |
|------|------|
| `main.py` | Push-to-talk lifecycle, hold gate, `--check-mic` diagnostic |
| `overlay.py` | Blue recording HUD (tkinter), driven by real mic level |
| `stt_engine.py` | Mic capture (sounddevice) + local faster-whisper transcription |
| `llm_agent.py` | System prompt, tool registration (function calling) |
| `gemini_client.py` | Shared Gemini client + model fallback / cooldowns |
| `config.py` | `.env` loading, settings, validation |
| `tools/os_tools.py` | File search, app launching, WhatsApp |
| `tools/vision_tools.py` | Screen capture, Gemini Vision coords, pyautogui clicks |
| `diagnose_keys.py` | **Raw key-event logger — the next thing to run** |

Environment: Windows 11 Pro 26200, Python 3.14.6 (global install, no venv),
1920×1080 at 125% scaling (logical 1536×864), Intel Smart Sound mic array.
All dependencies were already installed; nothing needed installing.

---

## 2. THE OPEN PROBLEM

**Symptom (user's words):** *"i held the caps lock button and no pop up"*.

Earlier, before the HUD existed, the same underlying report was *"it's not
working, i think the mic is not even on"*.

So **the app has never worked for the user, on any version.** That through-line
matters — it suggests one persistent environmental/behavioural cause rather than
a regression in the recent changes.

### What the code does on a Caps Lock hold

`main.py` (state machine, all under a single lock):

1. `_on_press(caps_lock)` → sets `_held = True`, starts a
   `threading.Timer(HOLD_SECONDS=2.0, _begin_capture)`, prints
   `[Azleem] hotkey down — keep holding 2s...`
2. At +2.0s `_begin_capture()` → `hud.show()`, `recorder.start()`, prints
   `[Azleem] listening... (release to send)`
3. `_on_release(caps_lock)` → cancels the timer; if capture had started, stops
   the recorder, hides the HUD, resets the Caps Lock toggle, and spawns a worker
   to transcribe + route through Gemini.

A press shorter than 2.0s is deliberately ignored so Caps Lock keeps working
normally as Caps Lock.

### Ranked hypotheses

**H1 — The user held it for under 2 seconds.** Mundane but the leading
candidate. Two seconds is genuinely longer than it feels, and there was
previously *no* feedback on key-down, so a 1.5s hold looked identical to a
totally broken app. Mitigation added: a console line now prints the instant the
key-down is seen (see step 1 above). If the user sees that line but no HUD, they
released too early. If they don't see it, the hook never got the key.

**H2 — The user wasn't running the current code.** They may have had an older
process still running, or launched from a different directory/terminal. Not yet
ruled out.

**H3 — Physical Caps Lock reports as an instant toggle (down+up together).**
Some Windows keyboards/drivers emit key-down and key-up back-to-back for Caps
Lock regardless of how long it's physically held. That would cancel the hold
timer instantly and the HUD could *never* appear — matching the symptom exactly.
**This is the hypothesis `diagnose_keys.py` was written to test and it has NOT
been tested against a real keypress yet.** Every test so far used synthetic
`keybd_event` injection, which controls down/up timing artificially and
therefore cannot detect this class of failure. If confirmed, the fix is
`HOTKEY=ctrl_space` in `.env` (already supported), or dropping the hold gate
for Caps Lock.

**H4 — Low-level keyboard hook blocked (antivirus/EDR).** **Considered and
largely ruled out.** While `diagnose_keys.py` was running, it captured the
user's own physical typing in another window — `ctrl_l` with ~31ms auto-repeat,
`alt_l`, `'h'`, `\x01` (Ctrl+A), `\x03` (Ctrl+C), `'c'`. So the hook does
receive real hardware input in this session.

**H5 — HUD is created but invisible.** Considered; evidence is against it. The
HUD was screenshotted visibly on screen during a real `python main.py` run
(over VS Code, correctly DPI-scaled). Worth noting a latent Win32 risk anyway:
`_make_click_through()` applies `WS_EX_LAYERED | WS_EX_TRANSPARENT |
WS_EX_NOACTIVATE` to `GetParent(tk_hwnd)`. Applying `WS_EX_LAYERED` to a window
with no layered attributes set can make it invisible; here Tk's
`-transparentcolor` already establishes the colour key, so it works — but it is
fragile and a plausible failure point on different hardware/driver combos.

### Next diagnostic step

```powershell
python diagnose_keys.py     # hold Caps Lock ~3s, release, press Esc
```

It logs every key event with timestamps and then reports how long each Caps Lock
press *appeared* to be held:

- `held 3.0s` → events fine, hold gate should work → problem is elsewhere (H1/H2)
- `held 0.0s` → **H3 confirmed** → use `HOTKEY=ctrl_space`
- DOWN with no UP → pynput never reports key-up for Caps Lock
- no events at all → hook not receiving (contradicts H4 evidence)

Verified working against injected input: correctly reported `press 1: 3.001s`
and `press 2: 0.001s`.

---

## 3. Verified working

### Gemini + model fallback (the original request)

The user asked that requests not be pinned to one model, and that it switch when
rate-limited. **The app could not have run as originally shipped** — its
hardcoded `gemini-2.0-flash` was already 429 on this key:

| Model | Status on this key |
|-------|--------------------|
| `gemini-2.0-flash` (was the hardcoded default) | **429 rate-limited** |
| `gemini-2.5-flash`, `gemini-2.5-flash-lite` | **404 retired** (still listed by the API) |
| all `pro` models | **429** (no free-tier pro quota) |

New `gemini_client.py` walks an ordered chain until one answers:

```
gemini-3.6-flash → gemini-3.5-flash → gemini-3-flash-preview
  → gemini-3.5-flash-lite → gemini-3.1-flash-lite
  → gemini-flash-latest → gemini-flash-lite-latest
```

All seven were individually confirmed to support **both** function calling and
vision (Azleem needs both; a text-only fallback would silently break screen
clicking).

- Falls back on `429`, `404`, `5xx`.
- Deliberately does **not** fall back on `401`/`403`/`400` — identical failure on
  every model, so retrying would bury the real error.
- Cooldowns: a rate-limited model is skipped for `MODEL_COOLDOWN_SECONDS` (or the
  API's own `retryDelay`); last-working model is tried first. If everything is
  cooling down they're retried anyway, soonest-recovering first.
- **No duplicated side effects.** Tools launch apps and send WhatsApp messages.
  With automatic function calling, a 429 on the *follow-up* turn happens after
  the tool already ran, so naive fallback would send a message twice. Tool calls
  are tracked; fallback halts once anything has executed and reports what
  actually happened.

Observed live: `gemini-2.5-flash` was briefly set as primary before it was known
to be retired; the router caught the 404 and moved to `gemini-3.6-flash`
unaided.

### End-to-end (bypassing the microphone)

Feeding a TTS-generated WAV directly into the pipeline:

```
TRANSCRIBED: 'Open Notepad.'
[gemini] using gemini-3.6-flash.
AZLEEM REPLY: Notepad has been opened.
TOOL CALLS: ['Notepad']
```

### The HUD

Screenshotted rendering live over VS Code during a real `python main.py` run:
blue/cyan panel, bottom-centre, transparent rounded corners, pulsing dot,
`J A R V I S` / `LISTENING`, elapsed timer, 44-bar waveform, correctly scaled at
125% DPI. Click-through and non-focus-stealing (`WS_EX_TRANSPARENT |
WS_EX_NOACTIVATE`) so it can't steal typing or intercept the clicks
`execute_screen_task` makes.

Waveform is driven by real RMS from the audio callback, not an animation — so a
flat line genuinely means no input. Also renders a
`NO INPUT DETECTED - CHECK MIC / MUTE KEY` state.

### Test suites (29/29, all offline)

| Suite | Count | Covers |
|-------|-------|--------|
| `test_fallback.py` | 13 | Each error class, cooldowns, ordering, preference, all-exhausted, `retryDelay` parsing |
| `test_integration.py` | 5 | Side-effect guard (tool runs exactly once under mid-conversation 429), real vision call + coordinate math |
| `test_hold.py` | 11 | Sub-threshold tap ignored, hold opens mic, release stops/processes, auto-repeat, injected-caps guard, timer cancellation |

Test files live in the session scratchpad, not the repo.

### The mic itself is fine

```
python main.py --check-mic
[check] default input device: Microphone Array (Intel® Smart
[check] blocks=153  rms mean=0.00670 max=0.01806
[check] captured 4.0s, peak=0.0600
[check] VERDICT: the mic is delivering audio.
```

Measured noise floor: ambient ≈ 0.008–0.02 RMS; a muted device delivers exact
zeros.

---

## 4. Bugs found and fixed along the way

**1. Whisper's VAD was discarding real audio.** A live run captured 2.5s at peak
0.031 — audio was present — and `vad_filter=True` threw it away, printing
`(heard nothing)`. VAD is tuned for continuous streams and routinely rejects
short/quiet push-to-talk takes. Now: VAD first, and if it yields nothing while
the take clearly wasn't silence, retry unfiltered. True silence still
short-circuits, so Whisper can't hallucinate `"Thank you."` into an empty room.
**This is very likely what broke the user's earlier attempts, separately from the
HUD problem.**

**2. Phantom recordings from the Caps Lock reset.** `_reset_caps_lock` injects a
synthetic Caps Lock tap to preserve the user's caps state. The keyboard hook saw
that injection as a *new* keypress and started a second recording. Now marked
and ignored, with a timed clear so a swallowed event can't strand the guard and
eat the next real keypress (`test_hold.py` case 4).

**3. `UnicodeEncodeError` on non-ASCII output.** Console is cp1252; the banner's
em dash printed as `�` and Gemini replies containing curly quotes/em
dashes/degree signs could raise. Inside a `try/except` so it wouldn't crash, but
the reply was lost and replaced by a charmap error. `main.py` now forces UTF-8 on
stdout/stderr.

**4. Pydantic strict-fields crash.** The router originally tagged the response
object via `setattr(response, "jarvis_model", model)`;
`GenerateContentResponse` rejects extra attributes. Replaced with
`router.last_model`.

**5. False `NO INPUT DETECTED` warning.** Threshold was set on a guess (0.002
RMS, warn after 1.4s) and fired against genuinely-present audio. Recalibrated
against measured data to 0.0008 RMS after 2.0s.

---

## 5. NOT verified — please don't assume these work

1. **A physical Caps Lock press has never successfully triggered a recording.**
   Every successful trigger in testing used synthetic `keybd_event` injection.
   This is the open problem in §2 and injection cannot detect H3.
2. **Speech recognition with a real voice.** Never tested. Attempts to validate
   acoustically were invalid: ambient noise measured *higher* (0.0197 RMS) than
   TTS played through the speakers (0.0138), i.e. the speakers aren't audibly
   reaching the mic. An earlier 0.339 peak that looked like success was ambient
   noise, not the clip. Whisper transcribed correctly only when audio was fed
   directly as an array, bypassing the mic.
3. **`send_whatsapp_message`** — needs the WhatsApp desktop app and
   `WHATSAPP_CONTACTS` in `.env`, still `{}`.
4. **`execute_screen_task` clicking for real.** The Gemini Vision call and
   coordinate math are verified (clicked 800.5,130.6 against a true centre of
   800,130 on a synthetic screenshot), but `pyautogui.click` was stubbed
   throughout — no real click was ever issued.
5. **`search_and_open_file` / `open_application` against the real OS.** Stubbed
   in tests to avoid launching apps.
6. **Multi-monitor.** `execute_screen_task` uses `sct.monitors[1]` only.

---

## 6. Config reference (`.env`)

| Key | Default | Notes |
|-----|---------|-------|
| `GEMINI_API_KEY` | — | Required. `.env` is gitignored; the key is not committed. |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Primary; used for routing **and** vision |
| `GEMINI_FALLBACK_MODELS` | *(built-in chain)* | Comma-separated override |
| `MODEL_COOLDOWN_SECONDS` | `60` | Skip duration after quota exhaustion |
| `HOTKEY` | `caps_lock` | `caps_lock` or `ctrl_space` — **`ctrl_space` is the fix if H3 is confirmed** |
| `HOLD_SECONDS` | `2.0` | Hold duration before the mic opens |
| `SHOW_OVERLAY` | `true` | Blue HUD; `false` for headless |
| `WHISPER_MODEL` | `base` | `tiny`…`large-v3` |
| `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` | `cpu` / `int8` | `cuda`/`float16` for GPU |
| `DOWNLOADS_DIR` | `%USERPROFILE%\Downloads` | File-search root |
| `WHATSAPP_CONTACTS` | `{}` | JSON name→E.164 map |

---

## 7. Specific questions for review

1. **Is H3 (Caps Lock as instant toggle) real on Windows 11?** Does the low-level
   hook reliably report a *sustained* key-down for Caps Lock, or is
   hold-to-talk on Caps Lock fundamentally unreliable and better abandoned for
   `ctrl_space`?
2. **Is a 2-second gate the wrong design?** It was explicitly requested, but it
   makes a working app indistinguishable from a broken one for the first two
   seconds. Would arming the HUD immediately on key-down (with a visible
   countdown, mic opening at 2s) be better feedback without violating the intent?
3. **Is the `WS_EX_LAYERED` re-application in `_make_click_through()` safe?** It
   works here, but is layering on `GetParent(tk_hwnd)` after Tk's
   `-transparentcolor` a latent invisible-window bug?
4. **Is the VAD retry the right call**, or should `vad_filter` be off entirely
   for push-to-talk?

### Already ruled out — please don't re-litigate

- **"The listener moved off the main thread when the HUD was added."** Checked:
  `pynput.keyboard.Listener` is itself a `threading.Thread` subclass
  (`MRO: Listener → ListenerMixin → Listener → AbstractListener → Thread`), so
  the `WH_KEYBOARD_LL` hook has *always* run on its own dedicated thread. The
  change from `with Listener(...) as l: l.join()` to `l.start()` only affects
  whether the main thread blocks — it does not move the hook. Not a suspect.
- **Antivirus/EDR blocking the hook** — see H4 in §2; the hook demonstrably
  captured the user's physical typing.
