# 10 features worth adding to Azleem

Ranked by impact ÷ effort. Each is scoped against the code as it stands, so the
"where" column points at the file that would actually change.

`open_url` isn't on this list — it shipped as part of the speed fix, not as a
feature.

A note on ordering: **#1 and #2 are the two that change how Azleem feels day to
day.** Everything from #6 down is capability breadth, which matters less than
the loop being fast and closed.

---

## 1. Clipboard tools — `read_clipboard` / `write_clipboard`

**Effort: ~1 hour. Impact: high.**

"Summarise what I just copied", "translate this", "fix the grammar in what I
copied", "put the answer on my clipboard". The clipboard is the cheapest
possible channel between you and the assistant — no screenshot, no vision call,
no OCR, and the text arrives perfectly accurate instead of via a model reading
pixels.

This also makes several *existing* tools better: `solve_with_python` could take
its problem statement from the clipboard rather than needing `read_screen`,
which costs a vision round trip and can misread.

*Where:* new `tools/clipboard.py`, register in `llm_agent._TOOLS`. `pyperclip`
is a two-line dependency; `pyautogui` already pulls in most of what's needed.

---

## 2. Spoken replies (TTS)

**Effort: ~half a day. Impact: high.**

Right now Azleem has **no voice output at all** — every reply is text on the HUD
and in the console. You talk to it and it writes back. That's half a loop, and
it's why a finished task can still feel unfinished: you have to look at the
overlay to learn anything.

Windows SAPI is already on the machine (`tests/make_fixtures.ps1` uses it), so a
zero-dependency version is genuinely small. `edge-tts` sounds dramatically
better if you're willing to take the dependency and the network call.

Two things to get right:
- **Interruptibility** — Esc must cut the speech, same as it cancels a task.
- **Don't fight the mic.** `speaker_mute.py` already mutes output while the mic
  is open; TTS has to respect that or Azleem will transcribe itself.

*Where:* new `tools/speech.py` (or a `speak()` on the HUD path), called from
`main._process` where `show_reply` happens.

---

## 3. Conversation memory (a short rolling history)

**Effort: ~half a day. Impact: high.**

`JarvisAgent` is explicitly stateless per command — every request starts from
nothing. So "open youtube" → "now search it for jazz" can't work, and neither
can "no, the other one" or "do that again". Follow-ups are the most natural
thing to say to an assistant and currently the one thing it can't hear.

Keep it small: the last ~5 turns, dropped after a few minutes idle. A full
transcript would cost tokens and confuse routing.

*Where:* `llm_agent.JarvisAgent` — hold a deque of `types.Content`, pass it as
`contents` instead of a bare string. Watch the interaction with the side-effect
tracking in `_tracked` / `keep_falling_back`.

---

## 4. Local fast-path intents (no model call at all)

**Effort: ~half a day. Impact: high, and it's the biggest remaining latency win.**

About fifteen commands cover most real use: "open X", "take a screenshot",
"what time is it", "lock the screen". Matching those with a regex table and
dispatching straight to the tool skips the model entirely — that's **~1–3
seconds saved per command**, per the routing numbers in `tests/bench.py`.

The second benefit matters more than the speed: `logs/azleem.log` shows the free
tier's 5-requests-per-minute limit being hit regularly, with the fallback chain
walking model to model. A local fast path means the commands you use most keep
working when quota is gone.

Rule: only ever fast-path an *unambiguous* match, and fall through to Gemini on
any doubt. A wrong instant answer is worse than a right slow one.

*Where:* new `intents.py`, consulted at the top of `JarvisAgent.handle`.

---

## 5. System control — volume, brightness, lock, sleep, battery, Wi-Fi

**Effort: ~half a day. Impact: medium-high.**

"Turn it down", "mute", "lock the screen", "how's my battery". These are the
things you most want to say without reaching for the mouse, and Azleem currently
can't do any of them. `speaker_mute.py` already talks to the audio endpoint via
pycaw, so volume is nearly free.

Keep sleep/shutdown behind a confirmation — a misheard "shut down" that's
actually obeyed is a bad day.

*Where:* new `tools/system.py`.

---

## 6. Window management — snap, switch, close, minimise

**Effort: ~half a day. Impact: medium.**

"Put this on the left", "switch to Chrome", "close this window", "minimise
everything". `pygetwindow` is already installed (`open_url`'s WhatsApp wait uses
it), so this is mostly wiring.

Worth it partly because it takes work *away* from `perform_computer_task`: window
juggling is currently a multi-step vision loop, and it doesn't need to be.

*Where:* new `tools/windows.py`.

---

## 7. Calendar and email *reading*

**Effort: ~1 day. Impact: medium, but high if you live in your calendar.**

`add_calendar_event` can only write — it emits an `.ics` and opens it. Azleem
can't answer "what's on today" or "when's my next meeting", which is probably
the single most common thing people ask an assistant.

Reading means real auth (Google Calendar API / Graph API), which is the bulk of
the work and the reason this isn't higher. A cheap first version: parse the
`.ics` files Azleem itself wrote into `events/`, so at least *its own* entries
are queryable.

*Where:* extend `tools/productivity.py`, plus a new auth module.

---

## 8. File operations — move, rename, copy, delete

**Effort: ~half a day. Impact: medium.**

`search_and_open_file` can find and open, but not act. "Move that to Documents",
"rename it", "delete the file I just downloaded" all fail today.

Delete needs care: never a hard delete. Route to the Recycle Bin (`send2trash`)
and confirm the specific filename in the reply so a mistake is recoverable and
visible. The existing search logic already picks "the most recently modified
match", which is the right anchor for "the file I just downloaded".

*Where:* extend `tools/os_tools.py`, reusing `search_and_open_file`'s matching.

---

## 9. Custom voice macros

**Effort: ~half a day. Impact: medium, high if you have routines.**

A user-defined phrase → fixed tool chain, in a JSON file. "Start my work setup"
→ open three apps, open two URLs, set a timer. No model call, so it's instant
and quota-free.

This is #4's machinery pointed at a config file instead of a builtin table —
build them together and the second one is nearly free.

*Where:* `.env` / `macros.json` + the same dispatcher as #4.

---

## 10. A real web-search-and-answer tool

**Effort: ~1 day. Impact: medium.**

For "what's the weather", "who won last night", "what's the exchange rate",
Azleem's options today are answer from stale model knowledge, or drive a browser
with the vision loop. Neither is good: one is confidently out of date, the other
takes 20 seconds and leaves tabs open.

A search API (Brave, Tavily, SerpAPI) returning text the model summarises would
be both faster and more accurate. Lower priority than it looks because
`open_url` now puts a results page on screen in about a second — often that's
genuinely what you wanted.

*Where:* new `tools/web.py`.

---

## Suggested order

1. **Clipboard** (#1) — an hour, immediately useful, makes existing tools better.
2. **TTS** (#2) — closes the loop; the biggest change in how Azleem *feels*.
3. **Fast-path intents** (#4) — the last big latency win, and quota insurance.
4. **Conversation memory** (#3) — unlocks follow-ups, which changes how you talk to it.

Then pick from #5–#10 by what you actually reach for.

Whatever gets built: add its routing fixtures to `tests/test_routing_live.py`
and a row to `tests/LIVE_CHECKLIST.md`. Every new tool is one more thing the
model can pick by mistake, and the routing tests are what catch that.
