"""Shared screen-capture and coordinate helpers for the vision tools.

Used by both the single-click ``execute_screen_task`` and the autonomous
``perform_computer_task`` loop, so the screenshot path and the DPI-aware
coordinate math live in exactly one place.

NOTE: no ``from __future__ import annotations`` in any tools module — string
annotations break google-genai's automatic function calling (see os_tools.py).
"""

import ctypes
import io
import json
import re

import mss
import pyautogui
from PIL import Image

# Make this process DPI-aware so screenshot pixels map to real screen pixels.
try:
    ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass  # Non-Windows or already set — safe to ignore.

pyautogui.FAILSAFE = True


# Vision payload size: a full-res 1920x1080 PNG is 2-4 MB per request, which
# dominates step latency on a free-tier key. 1280-wide JPEG is ~10x smaller and
# UI text stays perfectly legible to Gemini at that scale.
_MAX_UPLOAD_WIDTH = 1280
_JPEG_QUALITY = 72

MIME_TYPE = "image/jpeg"


def capture_screen() -> "tuple[Image.Image, bytes]":
    """Screenshot the primary monitor.

    Returns:
        (PIL image at ORIGINAL physical resolution, downscaled JPEG bytes for
        the vision request). Coordinate math must use the original image's
        size — model boxes are normalised 0-1000 over the picture, so the
        downscale does not affect them.
    """
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # index 0 is the "all monitors" virtual screen
        shot = sct.grab(monitor)
    image = Image.frombytes("RGB", shot.size, shot.rgb)

    upload = image
    if image.width > _MAX_UPLOAD_WIDTH:
        ratio = _MAX_UPLOAD_WIDTH / image.width
        # BILINEAR, not LANCZOS. Measured on a 1920x1080 desktop: 17 ms vs
        # 38 ms, and the JPEG comes out *smaller* (82 KB vs 93 KB) because
        # LANCZOS sharpening adds high-frequency detail the encoder then has to
        # store. This is only a 1.5x downscale, where the two are visually
        # equivalent — the quality gap only opens up on large reductions.
        # Fewer bytes also means a faster upload on every vision request.
        upload = image.resize(
            (_MAX_UPLOAD_WIDTH, int(image.height * ratio)), Image.BILINEAR
        )
    buf = io.BytesIO()
    upload.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return image, buf.getvalue()


# Change detection. The metric is "what fraction of pixels moved", NOT the mean
# difference over the frame — that distinction cost a whole round of debugging.
#
# The old check (mean absolute difference of 64px thumbnails vs 2.0) was blind on
# any text-heavy page. Measured on a quiz screenshot: clicking an option scored
# 0.616 and advancing to an entirely different question scored 0.631, both far
# under 2.0, so every action was reported as "SCREEN DID NOT CHANGE". The agent
# was then told its correct answer had done nothing and picked a different option
# to make something happen — that is how right answers became wrong ones.
#
# Averaging is the flaw: a page is mostly unchanged background, which dilutes any
# local edit toward zero no matter how obvious it is to a human. Counting pixels
# that moved does not dilute. Measured separation with the values below:
#   identical frames      0.00000
#   clicked an option     0.00957      (~480x the noise floor)
#   next question shown   0.00987
# A blinking text caret is ~0.00002, comfortably under the threshold.
_DIFF_WIDTH = 384        # thumbnail width; 64 was too coarse to see text change
_PIXEL_TOLERANCE = 24    # per-pixel greyscale delta that counts as "moved"
_CHANGED_FRACTION = 0.002


def changed_fraction(a: "Image.Image", b: "Image.Image", region=None) -> float:
    """Fraction of pixels (0..1) that differ between two screenshots.

    Args:
        a, b: Screenshots to compare. May be different sizes; ``b`` is matched
            to ``a``.
        region: Optional ``(ymin, xmin, ymax, xmax)`` in Gemini's normalised
            0-1000 space, to check just one element (e.g. the option that was
            clicked) rather than the whole screen.
    """
    from PIL import ImageChops, ImageStat

    if b.size != a.size:
        b = b.resize(a.size)
    if region is not None:
        ymin, xmin, ymax, xmax = region
        box = (
            max(0, int(xmin / 1000.0 * a.width)),
            max(0, int(ymin / 1000.0 * a.height)),
            min(a.width, int(xmax / 1000.0 * a.width)),
            min(a.height, int(ymax / 1000.0 * a.height)),
        )
        if box[2] - box[0] < 2 or box[3] - box[1] < 2:
            return 0.0  # degenerate crop — treat as "no evidence of change"
        a, b = a.crop(box), b.crop(box)

    width = min(_DIFF_WIDTH, a.width)
    size = (width, max(1, int(width * a.height / a.width)))
    ta = a.convert("L").resize(size)
    tb = b.convert("L").resize(size)
    diff = ImageChops.difference(ta, tb).point(
        lambda p: 255 if p > _PIXEL_TOLERANCE else 0
    )
    return ImageStat.Stat(diff).mean[0] / 255.0


def screens_differ(a: "Image.Image", b: "Image.Image", threshold: float = None,
                   region=None) -> bool:
    """Whether two screenshots are meaningfully different.

    Args:
        a, b: Screenshots to compare.
        threshold: Optional override for ``_CHANGED_FRACTION``.
        region: Optional normalised 0-1000 box to restrict the comparison to.
    """
    limit = _CHANGED_FRACTION if threshold is None else threshold
    return changed_fraction(a, b, region=region) > limit


def wait_for_change(before, timeout: float = 1.4, region=None,
                    threshold: float = None, poll: float = 0.06):
    """Wait until the screen differs from ``before``, then return immediately.

    Replaces ``time.sleep(SETTLE)`` after an action. A fixed sleep pays the
    worst case every time: measured over a 16-question quiz, the old 0.6s
    settle spent 19.2 seconds waiting, almost all of it after the UI had
    already finished reacting. Most clicks land visibly within ~100 ms.

    Doubles as the verification step, so the caller does not capture the screen
    again afterwards — that halves the number of screenshots per action.

    Args:
        before: The screenshot taken before the action.
        timeout: Give up after this long and report no change.
        region: Optional normalised 0-1000 box to watch (e.g. the option that
            was clicked), so unrelated animation elsewhere doesn't count.
        threshold: Optional override for the change threshold.
        poll: Gap between checks.

    Returns:
        ``(changed, image, jpeg)`` — the last frame captured, so the caller can
        reuse it rather than taking another shot.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    image, jpeg = capture_screen()
    while True:
        if screens_differ(before, image, threshold=threshold, region=region):
            return True, image, jpeg
        if _time.monotonic() >= deadline:
            return False, image, jpeg
        _time.sleep(poll)
        image, jpeg = capture_screen()


def box_to_logical(box, phys_w: int, phys_h: int) -> "tuple[float, float]":
    """Centre of a Gemini bounding box -> logical screen coordinates.

    Gemini boxes are [ymin, xmin, ymax, xmax] normalised to 0-1000 over the
    screenshot. pyautogui works in logical coordinates, which differ from
    physical pixels under Windows display scaling (125%/150%), so the physical
    centre is divided back down by the measured scale ratio.
    """
    ymin, xmin, ymax, xmax = box
    cx_phys = ((xmin + xmax) / 2.0) / 1000.0 * phys_w
    cy_phys = ((ymin + ymax) / 2.0) / 1000.0 * phys_h

    logical_w, logical_h = pyautogui.size()
    scale_x = phys_w / logical_w if logical_w else 1.0
    scale_y = phys_h / logical_h if logical_h else 1.0
    return cx_phys / scale_x, cy_phys / scale_y


def valid_box(box) -> bool:
    """Whether ``box`` looks like a usable [ymin, xmin, ymax, xmax] list."""
    return (
        isinstance(box, list)
        and len(box) == 4
        and all(isinstance(v, (int, float)) for v in box)
    )


def parse_json(text: str):
    """Extract the first JSON object from a possibly noisy model response."""
    text = (text or "").strip()
    # Strip ```json fences if present.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


# Ask-one-thing-per-call pointing. Bundling a locate into a bigger vision call
# was measured at 2/5 accuracy versus 3/3 when asked on its own (see the design
# notes in tools/quiz.py), so this prompt requests exactly one box and nothing
# else. Shared here so any tool that needs "find this element and click it"
# (the quiz loop, WhatsApp contact search, ...) uses the same proven path.
_POINT_PROMPT = (
    "The image is a screenshot. Find the single clickable element described "
    'below and return ONLY compact JSON: {{"found": true, "box": [ymin, xmin, '
    'ymax, xmax]}} with integers 0-1000 normalised over the image (Gemini '
    "bounding-box convention), covering the element's full clickable area. "
    'If it is not visible, return {{"found": false}}.\n'
    "ELEMENT: {description}"
)


def locate(description, jpeg):
    """Bounding box of one on-screen element, described in words.

    Args:
        description: What to find, anchored to visible text where possible
            (e.g. 'the contact row whose name is exactly <the name>').
        jpeg: The screenshot bytes from ``capture_screen``.

    Returns:
        A ``[ymin, xmin, ymax, xmax]`` list (0-1000) or ``None`` if the element
        is not visible or the request failed. Never raises.
    """
    # Lazy imports: keep this module importable without the network stack, and
    # avoid any import cycle with providers.
    import providers
    from google.genai import types

    contents = [
        types.Part.from_bytes(data=jpeg, mime_type=MIME_TYPE),
        _POINT_PROMPT.format(description=description),
    ]
    try:
        response = providers.vision_generate(contents, None, needs_pointing=True)
    except Exception as exc:
        print(f"[screen] locate request failed: {exc}")
        return None
    data = parse_json(response.text or "")
    if not data or not data.get("found"):
        return None
    box = data.get("box")
    return box if valid_box(box) else None


def click_box(box, image_size) -> bool:
    """Click the centre of a normalised box. Returns False on the fail-safe."""
    cx, cy = box_to_logical(box, *image_size)
    try:
        pyautogui.moveTo(cx, cy, duration=0.12)
        pyautogui.click()
    except pyautogui.FailSafeException:
        return False
    return True
