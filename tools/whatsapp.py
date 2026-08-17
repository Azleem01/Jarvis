"""Message WhatsApp contacts by name, with no saved number required.

The number-based deep link (``tools/os_tools.send_whatsapp_message``) is fast but
needs an E.164 number configured up front in ``WHATSAPP_CONTACTS``. This module
removes that requirement: it opens the WhatsApp desktop app, types the name into
WhatsApp's own search box, and uses the shared vision pointer
(``tools/screen.locate``) to click the matching chat and the message field — the
same answer→point→click→verify pattern proven in ``tools/quiz.py``. Names it
successfully reaches are remembered in a local JSON cache (under ``logs/``, so it
stays private and out of git) so the assistant learns the user's contacts by
itself instead of depending on a hand-written list.

Honest limits: the WhatsApp UI does not hand back a phone number, so a
UI-discovered contact is cached by *name* (the canonical spelling seen on
screen). That makes the next send more reliable — it searches for the exact
name — but it still drives the UI rather than skipping to the instant deep link,
which only a known number allows. And a running send is only as good as the
vision model's ability to find the search box, the right row and the message box
on the user's WhatsApp layout; every step is verified and any failure is
reported plainly rather than claimed as success.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see ``tools/os_tools.py``).
"""

import json
import os
import time

import pyautogui

import cancellation
import config
from tools import screen
from tools.windows import wait_for_window

# What to hand the vision pointer for each step. Anchored to WhatsApp's stable
# layout language ("Search or start new chat", the bottom message box) rather
# than pixel positions, so it survives light/dark themes and window sizes.
_SEARCH_DESC = (
    'the "Search or start new chat" text input box at the very top of the '
    "left-hand chat list"
)
_MESSAGE_BOX_DESC = (
    "the message text input box at the bottom of the open chat, the field that "
    'says "Type a message"'
)

# Read (not point) prompt for bulk contact capture. Vision models that answer
# well but mislocate boxes are fine here — we only read text, never click.
_LIST_PROMPT = (
    "The image is a screenshot of the WhatsApp desktop app. Look ONLY at the "
    "left-hand list of chats/contacts and return ONLY compact JSON: "
    '{"names": ["<exact display name of each visible row>", ...]} in '
    "top-to-bottom order, copying each name exactly as shown. If the left list "
    'is not visible, return {"names": []}.'
)


# ---------------------------------------------------------------------------
# Local contact cache
# ---------------------------------------------------------------------------
def _load_cache() -> dict:
    """Read the contact cache, tolerating a missing or corrupt file."""
    try:
        with open(config.WHATSAPP_CONTACT_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_cache(cache: dict) -> None:
    """Write the contact cache, creating logs/ if needed. Never raises."""
    path = config.WHATSAPP_CONTACT_CACHE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"[whatsapp] could not write contact cache: {exc}")


def remember(name: str, phone: str = "") -> None:
    """Record a contact Azleem has reached, so it knows them next time."""
    key = str(name or "").strip().lower()
    if not key:
        return
    cache = _load_cache()
    entry = cache.get(key)
    if not isinstance(entry, dict):
        entry = {}
    entry["name"] = str(name).strip()
    if phone:
        entry["phone"] = str(phone).strip()
    cache[key] = entry
    _save_cache(cache)


def cached_phone(name: str) -> str:
    """The number cached for ``name``, or '' if only the name is known."""
    entry = _load_cache().get(str(name or "").strip().lower())
    if isinstance(entry, dict):
        return str(entry.get("phone", "")).strip()
    return ""


def link_contact_alias(alias: str, contact_name: str) -> str:
    """Teach Azleem that a nickname or relationship word means a saved contact.

    So "text my dad" reaches the right person even when he is saved under a real
    name. For example, say "remember my dad is" followed by the name he is
    actually saved under in WhatsApp.

    Args:
        alias: The word you use for them, e.g. "dad", "boss", "the landlord".
        contact_name: The name the contact is actually saved under.

    Returns:
        A status string confirming the link.
    """
    alias_clean = str(alias or "").strip()
    name = str(contact_name or "").strip()
    if not alias_clean or not name:
        return "I need both a nickname and the real contact name to link them."
    cache = _load_cache()
    key = name.lower()
    entry = cache.get(key)
    if not isinstance(entry, dict):
        entry = {"name": name}
    entry["name"] = name
    aliases = entry.get("aliases") or []
    if alias_clean.lower() not in [str(a).strip().lower() for a in aliases]:
        aliases.append(alias_clean)
    entry["aliases"] = aliases
    cache[key] = entry
    _save_cache(cache)
    return f"Got it — I'll treat '{alias_clean}' as {name} on WhatsApp."


# ---------------------------------------------------------------------------
# Driving the WhatsApp desktop UI
# ---------------------------------------------------------------------------
def _open_whatsapp(timeout: float) -> bool:
    """Open/focus the WhatsApp desktop app and wait for its window."""
    try:
        os.startfile("whatsapp://")  # type: ignore[attr-defined]
    except OSError as exc:
        print(f"[whatsapp] could not launch WhatsApp: {exc}")
        return False
    return wait_for_window("whatsapp", timeout=timeout)


def _find_and_open_chat(name: str, settle: float):
    """Search WhatsApp for ``name`` and open the best-matching chat.

    Returns ``(opened: bool, detail: str)``.
    """
    image, jpeg = screen.capture_screen()
    box = screen.locate(_SEARCH_DESC, jpeg)
    if box is None:
        return False, "couldn't find WhatsApp's search box on screen"
    if not screen.click_box(box, image.size):
        return False, "the mouse fail-safe triggered (cursor in a screen corner)"

    # Clear anything already in the box, then type the name.
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.press("backspace")
    pyautogui.write(name, interval=0.02)
    # Let the results list filter down before pointing at a row.
    _changed, image, jpeg = screen.wait_for_change(image, timeout=settle * 3)

    if cancellation.cancelled():
        return False, "cancelled"

    result_box = screen.locate(
        "the first contact or chat row in the left-hand results list whose name "
        f'best matches "{name}" (not the search box itself)',
        jpeg,
    )
    if result_box is None:
        return False, f"couldn't find a contact matching '{name}' in WhatsApp"

    before = image
    if not screen.click_box(result_box, image.size):
        return False, "the mouse fail-safe triggered"
    # Opening a chat repaints the whole right-hand pane — a large, obvious change.
    opened, _img, _jpeg = screen.wait_for_change(before, timeout=settle * 3)
    if not opened:
        return False, f"clicked '{name}' but the chat didn't open"
    return True, "opened"


def _type_and_send(message: str, settle: float):
    """Type ``message`` into the open chat and send it.

    Returns ``(sent: bool, detail: str)``.
    """
    image, jpeg = screen.capture_screen()
    box = screen.locate(_MESSAGE_BOX_DESC, jpeg)
    if box is not None:
        if not screen.click_box(box, image.size):
            return False, "the mouse fail-safe triggered"
        time.sleep(0.2)
    # If the box wasn't located, WhatsApp usually focuses it on chat open, so we
    # still try to type — but we only claim success if the screen confirms it.

    pyautogui.write(message, interval=0.01)
    time.sleep(0.15)
    before, _bjpeg = screen.capture_screen()
    pyautogui.press("enter")
    # Sending clears the input and adds a bubble: verify a real change happened.
    sent, _img, _jpeg = screen.wait_for_change(before, timeout=settle * 3)
    if not sent:
        return False, "typed the message but couldn't confirm it was sent"
    return True, "sent"


def send_via_ui(contact_name: str, message: str) -> str:
    """Open WhatsApp, find the contact by name, and send the message.

    The path used when no phone number is known. Returns a human-readable status
    string and never raises.
    """
    name = str(contact_name or "").strip()
    if not name:
        return "No contact name was given for the WhatsApp message."

    timeout = config.WHATSAPP_SEND_TIMEOUT
    settle = config.WHATSAPP_SETTLE_SECONDS

    if not _open_whatsapp(timeout):
        return (
            f"Opened WhatsApp but its window never came to the front, so I "
            f"couldn't message {name}."
        )
    time.sleep(settle)

    if cancellation.cancelled():
        return "Cancelled before messaging on WhatsApp."

    opened, detail = _find_and_open_chat(name, settle)
    if not opened:
        if detail == "cancelled":
            return "Cancelled while searching WhatsApp for the contact."
        return f"Couldn't message {name}: {detail}."

    if cancellation.cancelled():
        return f"Opened {name}'s chat but cancelled before sending."

    sent, detail = _type_and_send(message, settle)
    if not sent:
        return f"Opened {name}'s chat but {detail}."

    remember(name)
    return f"Sent WhatsApp message to {name}."


def _read_visible_names(jpeg) -> list:
    """Read the display names currently visible in the left-hand list."""
    import providers
    from google.genai import types

    contents = [
        types.Part.from_bytes(data=jpeg, mime_type=screen.MIME_TYPE),
        _LIST_PROMPT,
    ]
    try:
        response = providers.vision_generate(contents, None, needs_pointing=False)
    except Exception as exc:
        print(f"[whatsapp] name-read request failed: {exc}")
        return []
    data = screen.parse_json(response.text or "")
    if isinstance(data, dict) and isinstance(data.get("names"), list):
        return [str(n).strip() for n in data["names"] if str(n).strip()]
    return []


def capture_whatsapp_contacts(scope: str = "") -> str:
    """Save the user's WhatsApp contacts into Azleem's own local list.

    Opens WhatsApp, scrolls the left-hand chat/contact list from top to bottom,
    reads the visible names with vision at each step, and stores them in a local
    cache so future messages can be sent by name without any saved number. Stops
    as soon as a scroll no longer changes the screen (the end of the list) or a
    safety cap is reached.

    Args:
        scope: Optional note about what to capture; currently informational.

    Returns:
        A status string with how many contacts were saved.
    """
    timeout = config.WHATSAPP_SEND_TIMEOUT
    settle = config.WHATSAPP_SETTLE_SECONDS

    if not _open_whatsapp(timeout):
        return (
            "Opened WhatsApp but its window never came to the front, so I "
            "couldn't read the contact list."
        )
    time.sleep(settle)

    # Scroll over the left list pane, not screen centre, so the wheel hits the
    # chat list rather than the conversation pane.
    w, h = pyautogui.size()
    list_x = max(1, int(w * 0.15))
    list_y = int(h * 0.5)
    try:
        pyautogui.moveTo(list_x, list_y, duration=0.1)
    except pyautogui.FailSafeException:
        return "Couldn't position the cursor over the contact list."

    seen: dict = {}
    image, jpeg = screen.capture_screen()
    for _ in range(config.WHATSAPP_MAX_SCROLLS):
        if cancellation.cancelled():
            break
        for nm in _read_visible_names(jpeg):
            seen.setdefault(nm.strip().lower(), nm.strip())
        for _n in range(3):
            pyautogui.scroll(-120, x=list_x, y=list_y)
            time.sleep(0.04)
        changed, image, jpeg = screen.wait_for_change(image, timeout=settle * 2)
        if not changed:
            break  # reached the bottom of the list

    if not seen:
        return "I opened WhatsApp but couldn't read any contact names from the list."

    cache = _load_cache()
    added = 0
    for key, nm in seen.items():
        entry = cache.get(key)
        if not isinstance(entry, dict):
            cache[key] = {"name": nm}
            added += 1
        elif "name" not in entry:
            entry["name"] = nm
    _save_cache(cache)

    return f"Saved {len(seen)} WhatsApp contacts ({added} new) to my local list."
