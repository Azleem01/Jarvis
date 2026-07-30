"""Single-shot vision click: locate one UI element and click it.

``execute_screen_task`` captures the screen, asks Gemini Vision to locate a
UI target described in natural language, then moves the mouse and clicks it via
pyautogui. One vision request, one click — for anything multi-step use
``perform_computer_task`` in tools/computer_use.py instead.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import pyautogui
from google.genai import types

import providers
from tools import screen

_VISION_PROMPT = (
    "You are a UI locator. The image is a screenshot. Find the single UI element "
    "that best matches this instruction: \"{instruction}\".\n"
    "Respond with ONLY a compact JSON object and nothing else, in the form "
    '{{"found": true, "box": [ymin, xmin, ymax, xmax], "label": "<what it is>"}} '
    "where the box coordinates are normalised integers from 0 to 1000 relative to "
    "the image (Gemini bounding-box convention). If no element matches, respond "
    '{{"found": false}}.'
)


_READ_PROMPT = (
    "The image is a screenshot of the user's screen. Extract exactly what is "
    "requested below, verbatim where possible, as plain text with no "
    "commentary. If it is not on screen, reply with just: NOT FOUND.\n\n"
    "REQUESTED: {query}"
)


def read_screen(query: str) -> str:
    """Read specific text or information off the user's current screen.

    Use before acting on things shown on screen — e.g. to get the full text
    of a coding exercise, a quiz question, an error message, or a code snippet
    so it can be passed to another tool.

    Args:
        query: What to extract, e.g. "the full coding exercise statement
            including examples" or "the error message in the terminal".

    Returns:
        The extracted text, or a message that it wasn't found.
    """
    _image, jpeg = screen.capture_screen()
    try:
        response = providers.vision_generate(
            [
                types.Part.from_bytes(data=jpeg, mime_type=screen.MIME_TYPE),
                _READ_PROMPT.format(query=query),
            ],
        )
    except Exception as exc:
        return f"Could not read the screen: {exc}"
    text = (response.text or "").strip()
    if not text or text.upper().startswith("NOT FOUND"):
        return f"Nothing matching '{query}' is visible on screen."
    return text


def execute_screen_task(instruction: str) -> str:
    """Locate a UI element on screen by description and click it once.

    Takes a screenshot, uses Gemini Vision to find the element matching the
    instruction (e.g. "the submit button", "the correct quiz answer"), then
    moves the mouse there and clicks. For tasks needing several actions in a
    row, use perform_computer_task instead.

    Args:
        instruction: Natural-language description of what to click.

    Returns:
        A status string describing what was clicked, or why nothing was.
    """
    image, png = screen.capture_screen()

    # Locating an element is read-only, so falling back across models here is
    # free of side effects — the click only happens once we have coordinates.
    try:
        response = providers.vision_generate(
            [
                types.Part.from_bytes(data=png, mime_type=screen.MIME_TYPE),
                _VISION_PROMPT.format(instruction=instruction),
            ],
            needs_pointing=True,
        )
    except Exception as exc:  # network / API errors — report, don't crash
        return f"Vision request failed: {exc}"

    data = screen.parse_json(response.text or "")
    if not data or not data.get("found"):
        return f"Could not find anything matching '{instruction}' on screen."

    box = data.get("box")
    if not screen.valid_box(box):
        return f"Vision returned an unexpected result for '{instruction}'."

    cx, cy = screen.box_to_logical(box, *image.size)
    label = data.get("label", instruction)
    try:
        pyautogui.moveTo(cx, cy, duration=0.25)
        pyautogui.click()
    except pyautogui.FailSafeException:
        return "Aborted: fail-safe triggered (mouse moved to a screen corner)."
    return f"Clicked '{label}' at ({int(cx)}, {int(cy)})."
