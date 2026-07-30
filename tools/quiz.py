"""Answer a multiple-choice quiz on screen, question after question.

Why this is a dedicated tool and not a ``perform_computer_task`` prompt: the
generic loop screenshots, decides, acts, and re-decides from scratch every step,
capped at 12 actions. A sixteen-question quiz needs ~32 actions of the *same two
kinds*, and measurement showed the generic loop failing at every level — it
answered 3 of 16, got 1 right, and aborted. This is the same reasoning that
produced ``open_url``: a repetitive job deserves a purpose-built path.

Two findings from benchmarking free vision models against generated quiz pages
shape the whole design:

  1. **Ask one question per call.** A single call returning the question, the
     options, the answer *and* the bounding boxes located the right option only
     2 times in 5 — off by as much as 344/1000, i.e. clicking a different
     answer than the one it named. Splitting it into an "answer" call and a
     "point at this exact text" call scored 3/3 within 7/1000. So the answer
     call below never asks for coordinates.

  2. **Anchor clicks to option text, never to a layout.** Pointing is asked for
     by the option's verbatim text, so a radio list, a card grid, a dark-theme
     row and a numbered list all work without knowing anything about the site.

Every click is verified against the screen afterwards (``screen.screens_differ``
scoped to that element) and re-pointed once if it did not register, because a
silent no-op is what previously convinced the agent to change a correct answer.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import re

import pyautogui
from google.genai import types

import cancellation
import config
import providers
from tools import progress as progress_mod
from tools import screen

# A selection highlights one row; a new question repaints most of the page.
# Scoped to the option's own box, a registered click is far above this.
_OPTION_CHANGE = 0.004


_QUIZ_PROMPT = (
    "The image is a screenshot of a quiz on the user's screen.\n"
    "First transcribe the question and EVERY answer option exactly as written "
    "— do not paraphrase, and do not skip an option. Then decide which option "
    "is correct using your own knowledge, reasoning carefully.\n"
    "Respond with ONLY a compact JSON object and nothing else:\n"
    '{"question": "<verbatim question>",\n'
    ' "options": {"A": "<verbatim>", "B": "<verbatim>"},\n'
    ' "answer_letter": "A",\n'
    ' "answer_text": "<the verbatim text of the option you chose>",\n'
    ' "confidence": "high|medium|low",\n'
    ' "needs_current_info": true|false,\n'
    ' "kind": "single_choice|multi_select|text_entry|other",\n'
    ' "advance_label": "<verbatim text of the button that moves on>",\n'
    ' "advance_is_final": true|false,\n'
    ' "finished": true|false}\n'
    "Rules:\n"
    "- answer_text MUST be copied character-for-character from the option you "
    "chose. It is used to find that option on screen; a paraphrase selects the "
    "wrong answer.\n"
    "- Include every option that is visible, however many there are. If the "
    "options are numbered rather than lettered, map them to A, B, C... in order.\n"
    "- confidence is 'low' unless you actually know the answer. A guess "
    "labelled 'high' is worse than an honest 'low'.\n"
    "- needs_current_info is true when the answer depends on recent events, "
    "this year's news, or anything after your training data — for example a "
    "question asking what was 'recently announced'.\n"
    "- kind describes the question. Use 'single_choice' only when exactly one "
    "option is to be chosen.\n"
    "- finished is true only when this is no longer a question — a results "
    "page, a score summary, or the end of the quiz.\n"
    "- advance_label is the text on the button that moves past this question "
    "('Next', 'Continue', 'Submit'...). advance_is_final is true when that "
    "button hands in the WHOLE quiz for grading rather than going to another "
    "question. Report the label exactly; it is how a Next button turning into "
    "a Submit button is noticed.\n"
    "- Do NOT include bounding boxes or coordinates in this response."
)


_POINT_PROMPT = (
    "The image is a screenshot. Find the single clickable element described "
    'below and return ONLY compact JSON: {{"found": true, "box": [ymin, xmin, '
    'ymax, xmax]}} with integers 0-1000 normalised over the image (Gemini '
    "bounding-box convention), covering the element's full clickable area. "
    'If it is not visible, return {{"found": false}}.\n'
    "ELEMENT: {description}"
)


_ADVANCE_PROMPT = (
    "The image is a screenshot of a quiz. Find the control that moves on from "
    "this question — labelled Next, Continue, Submit, Check, an arrow, or "
    "similar. Respond with ONLY compact JSON:\n"
    '{"found": true, "box": [ymin, xmin, ymax, xmax], "label": "<its text>", '
    '"is_final_submit": true|false}\n'
    "is_final_submit is true when this control submits the WHOLE quiz for "
    "grading rather than advancing to another question — for example it is the "
    "last question, or the button says 'Submit Quiz' / 'Finish'. "
    'If there is no such control, return {"found": false}.'
)


# "C. Canberra", "3) Saturn", "(B) India" — the option's own label, which the
# model sometimes includes in answer_text despite being asked for the text only.
# It has to come off: that string is handed to the locator as the thing to find
# on screen, and searching for the label as well as the text makes the match
# worse. Observed live on a radio-button layout.
# The optional space before the delimiter covers "C - Canberra" as well as
# "C. Canberra"; the trailing \s+ means a bare "B." is left alone, since the
# locator still needs something to search for.
_OPTION_PREFIX = re.compile(r"^\s*\(?([A-Za-z]|\d{1,2})\)?\s*[.):–-]\s+")


def _strip_option_label(text):
    """Remove a leading 'A. ' / '3) ' / '(B) ' label from an option's text."""
    cleaned = _OPTION_PREFIX.sub("", str(text or "").strip(), count=1)
    # Never strip the whole thing: a genuine one-word answer like "A" would
    # otherwise vanish and the locator would have nothing to search for.
    return cleaned if cleaned else str(text or "").strip()


def _answer_config():
    """Generation settings for the answering call — reasoning stays ON.

    The opposite of the computer-use loop's config, deliberately: that one
    disables thinking to keep clicks snappy, which is exactly why quizzes must
    not run through it.
    """
    try:
        return types.GenerateContentConfig(temperature=0.0, max_output_tokens=2000)
    except Exception:  # very old SDK
        return None


def _ask(prompt, jpeg, gen_config=None, needs_pointing=False):
    """One vision request, returning parsed JSON or None."""
    contents = [
        types.Part.from_bytes(data=jpeg, mime_type=screen.MIME_TYPE),
        prompt,
    ]
    try:
        response = providers.vision_generate(
            contents, gen_config, needs_pointing=needs_pointing
        )
    except Exception as exc:
        print(f"[quiz] vision request failed: {exc}")
        return None
    return screen.parse_json(response.text or "")


def _locate(description, jpeg):
    """Bounding box of one element, asked for on its own.

    Single-purpose by design: bundling this into the answering call was
    measured at 2/5 accuracy versus 3/3 when asked separately.
    """
    data = _ask(_POINT_PROMPT.format(description=description), jpeg,
                needs_pointing=True)
    if not data or not data.get("found"):
        return None
    box = data.get("box")
    return box if screen.valid_box(box) else None


def _click_box(box, image_size):
    """Click the centre of a normalised box. Returns False on the fail-safe."""
    cx, cy = screen.box_to_logical(box, *image_size)
    try:
        pyautogui.moveTo(cx, cy, duration=0.12)
        pyautogui.click()
    except pyautogui.FailSafeException:
        return False
    return True


def answer_quiz(scope: str = "") -> str:
    """Answer the multiple-choice quiz currently on screen, question by question.

    Reads each question and its options off the screen, works out the correct
    answer, clicks it, then clicks Next and repeats until the quiz runs out of
    questions. Use this for any on-screen quiz, test or question set — it is
    much faster and more accurate than driving the screen step by step.

    Never clicks a final "Submit"/"Finish" button that would hand the whole
    quiz in: it stops there and reports what it answered, so the user reviews
    and submits.

    Args:
        scope: Optional. What the user asked for, e.g. "just this question" or
            "all of them". Leave empty to work through the whole quiz.

    Returns:
        A summary of how many questions were answered, which ones were
        uncertain, and why it stopped.
    """
    only_one = any(
        phrase in (scope or "").lower()
        for phrase in ("just this", "only this", "this one", "current question",
                       "one question", "next one")
    )
    limit = 1 if only_one else config.QUIZ_MAX_QUESTIONS

    answered = []          # (question, answer_text, confidence)
    uncertain = []         # 1-based indices needing review
    advance_box = None     # cached: the Next control rarely moves
    advance_label = None   # its text, so a Next turning into Submit is noticed
    next_frame = None      # the settled frame the last advance handed back
    stopped = ""

    for number in range(1, limit + 1):
        if cancellation.cancelled():
            stopped = "you pressed Esc"
            break

        # Advancing already waited for the next question to paint and handed
        # back that frame, so re-shooting the screen here would photograph the
        # same pixels twice. Only the first question pays for a capture.
        if next_frame is not None:
            image, jpeg = next_frame
            next_frame = None
        else:
            image, jpeg = screen.capture_screen()
        data = _ask(_QUIZ_PROMPT, jpeg, _answer_config())
        if not data:
            stopped = "I couldn't read the screen"
            break
        if data.get("finished"):
            stopped = "the quiz is finished"
            break

        kind = str(data.get("kind", "single_choice")).lower()
        if kind not in ("single_choice", ""):
            # Guessing at a multi-select or a text box would silently produce a
            # wrong answer; say so instead.
            stopped = f"question {number} is a {kind.replace('_', ' ')}, which I don't answer"
            break

        question = str(data.get("question", "")).strip()
        answer_text = _strip_option_label(data.get("answer_text", ""))
        letter = str(data.get("answer_letter", "")).strip().upper()[:1]
        if not answer_text:
            stopped = f"I couldn't make out the options on question {number}"
            break

        confidence = str(data.get("confidence", "")).strip().lower()
        if data.get("needs_current_info"):
            # Training data can't answer "what was recently announced"; the
            # answer still gets given, but it is flagged rather than trusted.
            confidence = "low"
        if confidence != "high":
            uncertain.append(number)

        progress_mod.progress(
            f"Q{number}: {answer_text[:40]}"
            + ("" if confidence == "high" else f" ({confidence or 'unsure'})"),
            prefix="quiz",
        )
        print(f"[quiz] Q{number}: {question}")
        print(f"[quiz]   -> {letter}. {answer_text}  (confidence: {confidence})")

        # -- select the answer, and confirm it actually took ------------------
        option_box = _locate(f'the answer option whose text is exactly "{answer_text}"',
                             jpeg)
        if option_box is None:
            stopped = f"I couldn't find '{answer_text[:40]}' on screen"
            break
        if not _click_box(option_box, image.size):
            stopped = "the fail-safe triggered (mouse in a screen corner)"
            break

        # Returns the instant the option visibly changes, and hands back the
        # frame it settled on so no extra screenshot is needed.
        took, after, after_jpeg = screen.wait_for_change(
            image, region=option_box, threshold=_OPTION_CHANGE)
        if not took:
            # The click landed somewhere inert. Re-point against a fresh frame
            # once — layouts shift, and a stale box is the likeliest cause.
            option_box = _locate(
                f'the answer option whose text is exactly "{answer_text}"', after_jpeg)
            if option_box is not None and _click_box(option_box, after.size):
                _took, after, after_jpeg = screen.wait_for_change(
                    image, region=option_box, threshold=_OPTION_CHANGE)

        answered.append((question, answer_text, confidence))

        if only_one:
            stopped = "you asked for this question only"
            break

        # -- advance ----------------------------------------------------------
        # The Next control does not move between questions, so locating it once
        # and reusing the box removes a whole model call per question — a third
        # of the requests in a 16-question quiz, and a third of the wall clock.
        # Correctness is preserved by verifying the click below: if the cached
        # box has gone stale the page won't change, and the next pass re-locates
        # from scratch. Clicking a stale Next is safe in a way that clicking a
        # stale *option* is not, which is why only this one is cached.
        # The answering call already reads the whole screen, so it reports the
        # advance button's LABEL — text, never coordinates, so it cannot repeat
        # the accuracy problem that made bundling boxes a mistake. This is what
        # keeps the cached box safe: a "Next" that has become "Submit" is caught
        # here, without spending a request to re-locate it every question.
        if data.get("advance_is_final"):
            stopped = ("the last control submits the whole quiz — "
                       "that one is yours to click")
            break
        label = str(data.get("advance_label", "")).strip().lower()
        if label and advance_label is not None and label != advance_label:
            advance_box = None      # the control changed; don't trust the cache
        advance_label = label or advance_label

        moved_on = False
        # Attempt 1 uses the cached box (no model call). If the page does not
        # react, attempt 2 locates the control properly. A fresh locate happens
        # on the first question and only again when the cache goes stale.
        for attempt in (1, 2):
            if advance_box is None:
                control = _ask(_ADVANCE_PROMPT, after_jpeg, needs_pointing=True)
                if not control or not control.get("found"):
                    stopped = "there was nothing left to click to move on"
                    break
                if control.get("is_final_submit"):
                    stopped = ("the last control submits the whole quiz — "
                               "that one is yours to click")
                    break
                box = control.get("box")
                if not screen.valid_box(box):
                    stopped = "I couldn't locate the Next control"
                    break
            else:
                box = advance_box

            if not _click_box(box, after.size):
                stopped = "the fail-safe triggered (mouse in a screen corner)"
                break

            moved_on, after, after_jpeg = screen.wait_for_change(after)
            if moved_on:
                advance_box = box     # confirmed working; reuse it next time
                next_frame = (after, after_jpeg)   # this is the next question
                break
            # Stale cache, or the page really is stuck. Drop the cache so the
            # second attempt pays for a proper locate.
            advance_box = None
            if attempt == 2:
                stopped = f"the page stopped responding after question {number}"

        if not moved_on:
            if not stopped:
                stopped = f"the page stopped responding after question {number}"
            break
    else:
        stopped = f"I reached the {limit}-question limit"

    return _report(answered, uncertain, stopped)


def _report(answered, uncertain, stopped):
    """An honest summary: what was answered, what to double-check, why it ended."""
    if not answered:
        return f"I didn't answer anything — {stopped}." if stopped else \
               "I couldn't find a quiz question on screen."

    parts = [f"Answered {len(answered)} question{'s' if len(answered) != 1 else ''}"]
    if uncertain:
        listed = ", ".join(f"Q{n}" for n in uncertain[:5])
        more = "" if len(uncertain) <= 5 else f" and {len(uncertain) - 5} more"
        parts.append(f"but I'm not confident about {listed}{more} — worth checking")
    summary = ", ".join(parts)
    return f"{summary}. Stopped because {stopped}." if stopped else f"{summary}."
