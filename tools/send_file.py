"""Send a file from this PC to the user's phone over WhatsApp.

A concrete demonstration of multi-step agency that needs no credentials: find
the file, open the WhatsApp chat, then drive the attach -> pick file -> send
flow, verifying each step on screen. "To my phone" means the user's own WhatsApp
chat (or a named contact) — a file sent to yourself shows up on your phone.

Honest limits: this drives WhatsApp's real UI by vision, so it depends on the
attach button, the Document menu item, and the OS file dialog looking roughly as
expected on the user's setup. Every step is verified and any failure is reported
plainly rather than claimed as success (gotcha 22). The live proof is a spoken
test only the user can run.

NOTE: no ``from __future__ import annotations`` — string annotations break
google-genai's automatic function calling (see tools/os_tools.py).
"""

import time
from pathlib import Path

import pyautogui

import cancellation
import config
from tools import screen, whatsapp

# How many files to scan before giving up, so a huge Documents tree can't hang.
_MAX_SCAN = 20000

_ATTACH_DESC = (
    "the attach button — a paperclip or a plus (+) icon — next to the message "
    "input box at the bottom of the open chat"
)
_DOCUMENT_DESC = (
    'the "Document" item in the attachment menu that just opened (not Photos, '
    "not Camera)"
)
_SEND_DESC = (
    "the green send button (a paper-plane / arrow icon) that sends the attached "
    "file"
)


def _find_file(name):
    """Newest file whose name contains ``name`` in Downloads/Documents/Desktop."""
    needle = str(name or "").strip().lower()
    if not needle:
        return None
    folders = [
        config.DOWNLOADS_DIR,
        Path.home() / "Documents",
        Path.home() / "Desktop",
    ]
    matches = []
    scanned = 0
    for folder in folders:
        try:
            for p in Path(folder).rglob("*"):
                scanned += 1
                if scanned > _MAX_SCAN:
                    break
                if p.is_file() and needle in p.name.lower():
                    matches.append(p)
        except (OSError, ValueError):
            continue
    if not matches:
        return None
    try:
        return max(matches, key=lambda p: p.stat().st_mtime)
    except OSError:
        return matches[0]


def send_file_to_phone(file_name: str, contact: str = "") -> str:
    """Send a file from this computer to the user's phone via WhatsApp.

    Finds the file by (partial) name in Downloads, Documents or Desktop, opens
    WhatsApp, opens the chat (the named contact, or the user's own chat when none
    is given so it lands on their phone), attaches the file and sends it. Each UI
    step is verified; nothing is reported as sent unless it visibly happened.

    Args:
        file_name: Full or partial name of the file to send, e.g. "resume".
        contact: WhatsApp contact to send to. Empty = send to yourself / your
            phone (uses WHATSAPP_SELF_NAME).

    Returns:
        A status string describing what happened.
    """
    path = _find_file(file_name)
    if path is None:
        return (
            f"I couldn't find a file matching '{file_name}' in Downloads, "
            "Documents or Desktop."
        )

    to_self = not str(contact or "").strip()
    target = str(contact or "").strip() or config.WHATSAPP_SELF_NAME
    if not target:
        return (
            "Who should I send it to? Say a contact name, or set WHATSAPP_SELF_NAME "
            "in .env to your own WhatsApp name so 'send it to my phone' knows where."
        )

    settle = config.WHATSAPP_SETTLE_SECONDS
    if not whatsapp._open_whatsapp(config.WHATSAPP_SEND_TIMEOUT):
        return (
            "Opened WhatsApp but its window never came to the front, so I "
            f"couldn't send {path.name}."
        )
    time.sleep(settle)
    if cancellation.cancelled():
        return "Cancelled before sending the file."

    opened, detail = whatsapp._find_and_open_chat(target, settle)
    if not opened:
        return f"Couldn't open a chat with {target}: {detail}."

    # 1. Open the attachment menu.
    image, jpeg = screen.capture_screen()
    box = screen.locate(_ATTACH_DESC, jpeg)
    if box is None:
        return f"Opened {target}'s chat but couldn't find the attach button."
    before = image
    if not screen.click_box(box, image.size):
        return "The mouse fail-safe triggered before I could attach the file."
    _c, image, jpeg = screen.wait_for_change(before, timeout=settle * 3)

    # 2. Choose "Document".
    box = screen.locate(_DOCUMENT_DESC, jpeg)
    if box is None:
        return "Opened the attach menu but couldn't find the Document option."
    before = image
    screen.click_box(box, image.size)
    opened_dialog, image, jpeg = screen.wait_for_change(before, timeout=settle * 4)
    if not opened_dialog:
        return "Clicked Document but the file picker never opened."

    if cancellation.cancelled():
        return "Cancelled before choosing the file."

    # 3. Type the path into the OS file dialog and confirm.
    time.sleep(0.4)  # let the dialog take focus
    pyautogui.write(str(path), interval=0.01)
    pyautogui.press("enter")
    picked, image, jpeg = screen.wait_for_change(image, timeout=settle * 4)
    if not picked:
        return f"Couldn't select {path.name} in the file picker."

    if cancellation.cancelled():
        return "Cancelled before sending the file."

    # 4. Send: the preview screen usually sends on Enter; fall back to the button.
    before = image
    pyautogui.press("enter")
    sent, image, jpeg = screen.wait_for_change(before, timeout=settle * 3)
    if not sent:
        box = screen.locate(_SEND_DESC, jpeg)
        if box is not None and screen.click_box(box, image.size):
            sent, image, jpeg = screen.wait_for_change(before, timeout=settle * 3)
    if not sent:
        return f"Attached {path.name} to {target} but couldn't confirm it sent."

    whatsapp.remember(target)
    where = "your phone" if to_self else target
    return f"Sent {path.name} to {where} on WhatsApp."
