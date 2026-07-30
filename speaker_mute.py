"""Mute the speakers while Azleem is listening.

Why this exists: the microphone hears whatever the speakers are playing. Asking
Azleem about a YouTube video meant the take contained the video's narration —
loud, clear and continuous — so Whisper transcribed the video instead of the
user, and Gemini answered the video's words.

Design notes (learned the hard way)
-----------------------------------
``GetMute()`` can return a stale value immediately after a ``SetMute()``, so
this module must never *decide whether* to unmute by reading the device. An
earlier version did: on a fast take it misread its own mute as the user's,
refused to unmute on principle, and left the speakers silent.

Instead: remember the mute state observed when the take started, and on restore
always drive the device explicitly back to that state, verifying the write.
Re-asserting a mute the user set is harmless; failing to undo ours is not.

Every failure path degrades to a no-op — audio control must never be able to
break the assistant.
"""

from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_cached = None       # resolved endpoint; resolving costs ~50ms (250ms first time)
_prior_state = None  # mute state before we touched it; None = we hold nothing


def _endpoint():  # noqa: ANN202
    """The default output device's volume interface, or None if unavailable.

    Cached: resolving through COM takes ~50ms, which would otherwise sit in
    front of every take. The cache is dropped whenever a call through it fails,
    so switching output device self-heals.
    """
    global _cached
    if _cached is not None:
        return _cached
    try:
        from pycaw.utils import AudioUtilities

        _cached = AudioUtilities.GetSpeakers().EndpointVolume
        return _cached
    except Exception:
        return None  # pycaw missing, no audio device, COM failure — all fine


def _invalidate() -> None:
    global _cached
    _cached = None


def endpoint():  # noqa: ANN201
    """The cached output-device volume interface, or None if unavailable.

    Public because ``tools/system.py`` drives the same device for the user's own
    volume commands. Resolving costs ~50ms through COM, so sharing the cache
    matters — but note the contract: callers may read and write the *device*,
    and must not touch this module's ``_prior_state``, which belongs to the
    take-scoped mute alone.
    """
    return _endpoint()


def drop_cache() -> None:
    """Forget the cached endpoint after a failed call through it."""
    _invalidate()


def prewarm() -> None:
    """Resolve the endpoint up front so the first take isn't delayed by COM."""
    try:
        endpoint = _endpoint()
        if endpoint is not None:
            endpoint.GetMute()
    except Exception:
        _invalidate()


def mute() -> None:
    """Mute the speakers, remembering the state to return them to."""
    global _prior_state
    with _lock:
        if _prior_state is not None:
            return  # already inside a take
        endpoint = _endpoint()
        if endpoint is None:
            return
        try:
            _prior_state = int(bool(endpoint.GetMute()))
            endpoint.SetMute(1, None)
        except Exception:
            _prior_state = None
            _invalidate()


def restore() -> None:
    """Return the speakers to exactly the state they were in before ``mute()``.

    Verified with a read-back and one retry: a silently-dropped unmute is the
    worst outcome this module can produce.
    """
    global _prior_state
    with _lock:
        if _prior_state is None:
            return  # we never muted — nothing of ours to undo
        wanted = _prior_state
        _prior_state = None  # cleared first, so a failure can't wedge us muted
        endpoint = _endpoint()
        if endpoint is None:
            return
        for attempt in (0, 1):
            try:
                endpoint.SetMute(wanted, None)
                if int(bool(endpoint.GetMute())) == wanted:
                    return
            except Exception:
                _invalidate()
                endpoint = _endpoint()
                if endpoint is None:
                    return
            if attempt == 0:
                time.sleep(0.05)  # let the audio service settle, then re-assert
