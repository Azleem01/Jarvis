"""Local speech-to-text: microphone capture + faster-whisper transcription.

Nothing here touches the network — audio is recorded with ``sounddevice`` and
transcribed on-device by ``faster-whisper``, so speech recognition is free and
private.

Usage::

    recorder = Recorder()
    transcriber = Transcriber()          # loads the model once (slow first time)

    recorder.start()                     # on hotkey press
    ...                                  # user speaks
    audio = recorder.stop()              # on hotkey release
    text = transcriber.transcribe(audio) # -> "open notepad"
"""

from __future__ import annotations

import json
import re
import threading

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

import config

# High-frequency, unambiguous mishears of the assistant's OWN name. Deliberately
# tiny and conservative — only fixes that are never a real command word, so
# ordinary speech is untouched. The user can add their own via STT_CORRECTIONS
# (e.g. "travis": "Jarvis"), which is riskier and therefore opt-in.
_DEFAULT_CORRECTIONS = {
    "diabetes": "Jarvis",
    "javascript": "Jarvis",
    "java script": "Jarvis",
    "jervis": "Jarvis",
}

# Static command vocabulary the decoder is biased toward. Contact names are
# added dynamically at load time (see _load_hotwords) so they are never hard
# coded and never trip the brand-contamination guard on model-facing prompts.
_BASE_HOTWORDS = [
    "Jarvis", "Azleem", "WhatsApp", "Notepad", "Chrome", "File Explorer",
    "screenshot", "paste the link", "submission field", "quiz", "next question",
    "mum", "dad",
]


def _load_corrections() -> "dict[str, str]":
    """The built-in name fixes, merged with any the user configured."""
    merged = dict(_DEFAULT_CORRECTIONS)
    raw = config.STT_CORRECTIONS
    if raw:
        try:
            user = json.loads(raw)
            if isinstance(user, dict):
                merged.update({str(k).lower(): str(v) for k, v in user.items()})
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[stt] bad STT_CORRECTIONS ({exc}); using built-in defaults.")
    return merged


def _correct(text: str, corrections: "dict[str, str]") -> str:
    """Apply whole-word, case-insensitive corrections to a transcript."""
    if not text:
        return text
    out = text
    for wrong, right in corrections.items():
        out = re.sub(rf"\b{re.escape(wrong)}\b", right, out, flags=re.IGNORECASE)
    return out


def _load_hotwords() -> str:
    """Command vocabulary plus the user's real contact names, to bias decoding.

    Contacts come from .env WHATSAPP_CONTACTS and the learnt cache, so names like
    'Aisha' are recognised instead of mangled. Defensive: any failure just falls
    back to the static vocabulary.
    """
    words = list(_BASE_HOTWORDS)
    try:
        words += [str(k) for k in config.WHATSAPP_CONTACTS]
        from tools import whatsapp

        for entry in whatsapp._load_cache().values():
            if isinstance(entry, dict):
                if entry.get("name"):
                    words.append(str(entry["name"]))
                words += [str(a) for a in entry.get("aliases", []) or []]
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[stt] could not load contact hotwords: {exc}")
    seen: set = set()
    unique = []
    for w in words:
        w = str(w).strip()
        if w and w.lower() not in seen:
            seen.add(w.lower())
            unique.append(w)
    return " ".join(unique)

# Safety cap on buffered audio blocks (~5 minutes at default block sizes). Only
# reachable if a key release is never delivered, which does happen when another
# app grabs the keyboard hook.
_MAX_FRAMES = 20000


class Recorder:
    """Captures mono float32 audio from the default input device.

    Recording happens in a background audio thread (sounddevice callback), so
    ``start()`` / ``stop()`` return immediately and never block the hotkey loop.
    """

    def __init__(
        self,
        sample_rate: int = config.SAMPLE_RATE,
        level_callback=None,  # noqa: ANN001
    ) -> None:
        """
        Args:
            sample_rate: Capture rate; must match what Whisper expects.
            level_callback: Optional ``fn(level: float)`` called from the audio
                thread with the RMS of each block (0..1). Used to drive the
                recording HUD, so the meter reflects real input rather than an
                animation. Keep it cheap and non-blocking.
        """
        self.sample_rate = sample_rate
        self._level_callback = level_callback
        self._frames: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._lock = threading.Lock()
        self._recording = False
        self.last_peak = 0.0  # loudest sample of the most recent take

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            # Overflows etc. are non-fatal; just note them.
            print(f"[stt] audio status: {status}")
        with self._lock:
            # A hotkey that never registers its release would otherwise grow
            # this list without limit; past the cap, keep listening but stop
            # accumulating rather than eating memory.
            if len(self._frames) < _MAX_FRAMES:
                self._frames.append(indata.copy())
        if self._level_callback is not None:
            try:
                self._level_callback(float(np.sqrt(np.mean(np.square(indata)))))
            except Exception:
                pass  # never let a UI hiccup kill the audio thread

    def start(self) -> None:
        """Begin recording. Safe to call again only after ``stop()``.

        start() runs on the hold-timer thread and stop() on the listener
        thread, so the whole transition — guard, buffer reset, stream creation
        and flag — happens under the lock. Setting ``_recording`` after the
        lock was released let two callers both pass the guard and open two
        streams, and let a stop() land between the flag and the stream being
        assigned.
        """
        with self._lock:
            if self._recording:
                return
            self._frames = []
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                callback=self._callback,
            )
            stream.start()
            self._stream = stream
            self._recording = True

    def stop(self) -> np.ndarray:
        """Stop recording and return the captured audio as a 1-D float32 array."""
        with self._lock:
            if not self._recording:
                return np.zeros(0, dtype=np.float32)
            self._recording = False
            stream = self._stream
            self._stream = None
            frames = self._frames
            self._frames = []

        # Closing outside the lock: the audio callback takes the same lock, so
        # holding it across a blocking device close invites a stall.
        if stream is not None:
            stream.stop()
            stream.close()

        if not frames:
            self.last_peak = 0.0
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(frames, axis=0).flatten()
        self.last_peak = float(np.abs(audio).max()) if audio.size else 0.0
        return audio


class Transcriber:
    """Wraps a single, reused faster-whisper model instance."""

    # Ignore clips shorter than this (seconds) — usually accidental taps.
    _MIN_SECONDS = 0.3

    def __init__(self) -> None:
        print(
            f"[stt] loading faster-whisper model '{config.WHISPER_MODEL}' "
            f"({config.WHISPER_DEVICE}/{config.WHISPER_COMPUTE_TYPE})..."
        )
        self.model = WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
        # Built once: the decoder bias vocabulary (incl. the user's contacts) and
        # the post-decode correction map. A restart picks up new contacts.
        self._hotwords = _load_hotwords()
        self._corrections = _load_corrections()
        print("[stt] model ready.")

    # Below this peak amplitude the take really is silence, and running Whisper
    # unfiltered on it invites hallucinated phrases like "Thank you."
    _SILENCE_PEAK = 0.002

    # Biases Whisper's decoder toward the vocabulary Jarvis commands actually
    # use. Without it the small models routinely mangle app names ("Open
    # Nutspad", "Open Note 5" were both real transcriptions of "open Notepad").
    _CONTEXT_PROMPT = (
        "Voice commands for a Windows desktop assistant named Jarvis: open "
        "Notepad, open Chrome, open File Explorer, launch Calculator, search for "
        "a file in Downloads, send a WhatsApp message to mum or dad, paste the "
        "link into the submission field, answer the quiz on screen, take a "
        "screenshot."
    )

    def _run(self, audio: np.ndarray, vad: bool) -> str:
        segments, _info = self.model.transcribe(
            audio,
            language="en",
            beam_size=config.WHISPER_BEAM_SIZE,
            vad_filter=vad,
            initial_prompt=self._CONTEXT_PROMPT,
            hotwords=getattr(self, "_hotwords", None) or None,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a mono float32 array (16 kHz) to text; '' if too short.

        Runs with Whisper's VAD first, which trims silence and guards against
        hallucination. But VAD is tuned for continuous streams and regularly
        discards short or quietly-recorded push-to-talk takes outright — the
        user held a key and spoke, so throwing their audio away is the worst
        possible outcome. If VAD yields nothing and the take clearly *wasn't*
        silence, retry unfiltered rather than reporting "heard nothing".
        """
        if audio is None or audio.size == 0:
            return ""
        if audio.size < self._MIN_SECONDS * config.SAMPLE_RATE:
            return ""

        corrections = getattr(self, "_corrections", None) or _DEFAULT_CORRECTIONS
        text = self._run(audio, vad=True)
        if text:
            return _correct(text, corrections)

        peak = float(np.abs(audio).max())
        if peak >= self._SILENCE_PEAK:
            print(f"[stt] VAD found no speech (peak {peak:.3f}); retrying unfiltered.")
            return _correct(self._run(audio, vad=False), corrections)
        return ""
