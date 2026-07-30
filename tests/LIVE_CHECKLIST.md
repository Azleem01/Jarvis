# Azleem live checklist

The automated suite can't press your hotkey or watch your screen. These are the
runs you do by hand. Say each command, watch what happens, tick the box.

Start Azleem, then work down the list. **Row 1 is the acceptance test** — it's
the exact command that produced the original bug report.

Time budgets are wall-clock from releasing the hotkey to the HUD showing a
reply. They assume the offline benchmark is passing (`python tests/bench.py`);
STT is ~0.7s and model routing ~1–3s of every figure below.

---

## Tier 1 — the reported bugs

| # | Say this | Expected | Budget | Pass |
|---|----------|----------|--------|------|
| 1 | "open youtube on my chrome browser" | Chrome opens on the **youtube.com home page** — not a channel, not a video, no search results. One action. HUD shows a reply and **dismisses itself**. | < 4 s | ☐ |
| 2 | "open youtube" | Default browser, YouTube home page. | < 4 s | ☐ |
| 3 | "search youtube for lofi hip hop" | Goes straight to a YouTube **results page** for that phrase. No clicking around. | < 4 s | ☐ |
| 4 | Say a command, then immediately say another while the first is still running | Second one answers **"Still working on the last one."** The HUD must never sit on "Thinking" with nothing coming. | instant | ☐ |
| 5 | Hold the hotkey and release without speaking | "That take was silent — check the mic or mute key." Dismisses on its own. | < 2 s | ☐ |
| 5b | With a quiz open on screen: "solve the question on my screen and move to the next one" | Answers **in place** — no browser opens, no web search, the quiz never leaves the screen — then clicks Next/Check so the **next question is showing**. Reply is one readable line, not a wall of text. | < 30 s | ☐ |
| 5c | With a 16-question quiz open: "answer the quiz on my screen" | Works through **every** question. HUD shows "Q1…Q16" ticking. Ends at the final Submit **without clicking it**, reporting "Answered 16 questions… Stopped because the last control submits the whole quiz". Check the log for the per-question record. | < 5 min | ☐ |
| 5d | Read back the answers it flagged as uncertain (`logs/azleem.log`) | Anything about recent events should be flagged. **Verify those yourself** — a model cannot know what was "recently announced". | — | ☐ |

## Tier 2 — the routes that must not have regressed

| # | Say this | Expected | Budget | Pass |
|---|----------|----------|--------|------|
| 6 | "open notepad" | Notepad launches. Not a browser. | < 3 s | ☐ |
| 7 | "take a screenshot" | PNG saved and opened from `~/Pictures/Screenshots`. | < 4 s | ☐ |
| 8 | "write a note that says buy milk" | Notepad opens with that text, saved under `~/Documents/Azleem Notes`. | < 5 s | ☐ |
| 9 | "set an alarm for 20 minutes from now" | Confirms a specific clock time — check the arithmetic against the real time. | < 5 s | ☐ |
| 10 | "what is the capital of France" | A one-line spoken answer. **No tool call**, no browser. | < 3 s | ☐ |

## Tier 3 — the slow paths and the edges

| # | Say this / do this | Expected | Budget | Pass |
|---|--------------------|----------|--------|------|
| 11 | With a form on screen: "fill in the name field with my name" | Uses the multi-step screen loop. HUD shows "Step *n*/12" ticking up — each step should feel ~1–2 s, not 4 s. | < 25 s | ☐ |
| 12 | Press **Esc** during row 11 | Stops within a step or two. HUD says "Cancelled." | < 3 s | ☐ |
| 13 | Turn off Wi-Fi, then "open notepad" | Notepad still opens (it's a local tool, no model round trip needed for the action itself). | < 5 s | ☐ |
| 14 | Turn off Wi-Fi, then "what is the capital of France" | A clear "can't reach the internet" reply — not a hang, not a stack trace. | < 15 s | ☐ |
| 15 | Fire ~8 commands back to back to exhaust the free-tier quota | Falls through the model chain; when all are spent, says so plainly. Never claims work it didn't do. | — | ☐ |

---

## Tier 4 — machine control and follow-ups

Rows 16–19 are the ones a unit test cannot reach: they depend on the real audio
endpoint, the real window manager, and on two utterances in a row.

| # | Say this / do this | Expected | Budget | Pass |
|---|---|---|---|---|
| 16 | "mute" | Speakers mute — **and stay muted** once the HUD reply has gone. Watch the Windows volume icon for a few seconds after. If they unmute themselves ~half a second later, the take-restore is landing on top of the command (see `tools/system.py`). | < 1 s | ☐ |
| 17 | "set volume to 30" | Windows mixer reads 30%. If the speakers were muted, they unmute — a new level you cannot hear is not a working command. | < 1 s | ☐ |
| 18 | "what's my battery" | Percentage and charge state. Check `logs/azleem.log` shows **no model call** for this — it should be a fast-path line. | instant | ☐ |
| 19 | "turn the brightness down to 20" | On a laptop panel, brightness drops. On an external monitor, it must say it **couldn't** — never "brightness set" for something that didn't move. | < 3 s | ☐ |
| 20 | "switch to \<an app you have open\>" | That window comes forward. It must not launch a **second copy**. | < 2 s | ☐ |
| 21 | "put this on the left" | Current window snaps to the left half. Not maximised, not moved to another monitor. | < 2 s | ☐ |
| 22 | "shut down" | **Nothing happens.** Reply asks you to say "confirm shutdown". | < 2 s | ☐ |
| 23 | Then wait 30 s and say "confirm shutdown" | Refused as expired. Still nothing happens. | < 2 s | ☐ |
| 24 | "shut down", then immediately "confirm shutdown" | It shuts down. **Do this last** — and only if you mean it. | — | ☐ |
| 25 | "open \<a site\>", then "now search it for \<something\>" | The follow-up resolves without you naming the site again. | < 4 s | ☐ |
| 26 | Wait 4 minutes after row 25, then "do that again" | History has expired: it asks what you mean rather than repeating the search. | < 4 s | ☐ |
| 27 | "open notepad", then "and make it full screen" | The second command acts on Notepad. This is the fast path recording its own turn — without it there is no antecedent. | < 4 s | ☐ |

## What counts as a failure

Beyond a red box above, treat any of these as a bug worth reporting:

- **The HUD stays on "Thinking"** after the thing you asked for has visibly happened. This was the original complaint; the watchdog now caps it at 45 s, but hitting the watchdog at all means something upstream is wrong.
- **Azleem substitutes something you didn't say** — a channel, a creator, a search term, a different site. This is the MKBHD class of bug. Note the exact command.
- **It claims success for something that didn't happen.** Worse than failing.
- **A website command takes more than ~4 s** or you see "Step 1/12" for it — that means it fell into the vision loop instead of `open_url`.
- **A browser opens for a question that was already on screen.** Searching the web for it navigates away from the very screen the task is about, so everything after acts on the wrong window. Answering happens in place.
- **Only the first half of a two-part command happens.** "Answer it *and* move on" is not done until the next question is on screen.

## Recording a run

```bash
python tests/bench.py
```

Run that before and after any change and keep both tables. For the live rows,
note the date, which model answered (the console prints `[gemini] using ...`),
and anything that missed its budget.
