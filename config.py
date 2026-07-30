"""Central configuration for Jarvis.

All tunable settings live here and are loaded from a local ``.env`` file
(see ``.env.example``). Importing this module fails fast with a clear message
if the Gemini API key is missing so the rest of the app can assume it is set.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load variables from a .env file sitting next to this module (if present).
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)


def _get(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value if value is not None and value != "" else default


# ---- Gemini ----------------------------------------------------------------
GEMINI_API_KEY: str = _get("GEMINI_API_KEY")
GEMINI_MODEL: str = _get("GEMINI_MODEL", "gemini-3.6-flash")

# Fallback chain used when the primary model is rate limited, out of quota, or
# retired. Every model here supports both function calling and vision, which is
# what Jarvis needs (routing + on-screen element location). Ordered strongest
# first; the "-lite" and "-latest" aliases sit at the end as last resorts
# because they draw on separate quota pools from the numbered models.
_DEFAULT_FALLBACK_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
]


def _model_chain() -> list[str]:
    """Primary model first, then fallbacks, de-duplicated and order-preserving."""
    configured = _get("GEMINI_FALLBACK_MODELS")
    fallbacks = (
        [m.strip() for m in configured.split(",") if m.strip()]
        if configured
        else _DEFAULT_FALLBACK_MODELS
    )
    chain: list[str] = []
    for model in [GEMINI_MODEL, *fallbacks]:
        if model and model not in chain:
            chain.append(model)
    return chain


# The full ordered list of models Jarvis will try for any single request.
GEMINI_MODEL_CHAIN: list[str] = _model_chain()

# How long to skip a model after it reports quota exhaustion, when the API
# doesn't tell us its own retry delay. Free-tier limits are mostly per-minute.
MODEL_COOLDOWN_SECONDS: float = float(_get("MODEL_COOLDOWN_SECONDS", "60"))

# ---- OpenRouter (optional second provider) ---------------------------------
# An OpenAI-compatible gateway in front of many vendors. Exists here because
# Gemini's free tier allows 20 requests per day *per model*, which a single
# quiz exceeds. Absent key = Azleem runs on Gemini alone; it never fails hard.
OPENROUTER_API_KEY: str = _get("OPENROUTER_API_KEY")

# Which provider serves vision requests first: "openrouter" or "gemini".
# Routing always stays on Gemini — free models route measurably worse.
VISION_PROVIDER: str = _get("VISION_PROVIDER", "openrouter").strip().lower()

# Two chains, and they are NOT interchangeable. Benchmarked against generated
# quiz pages with known option positions:
#   google/gemma-4-*            answers correct, boxes within ~7/1000  -> can click
#   nvidia/nemotron-*           answers correct, boxes off by 231/1000 -> cannot click
# Putting a Nemotron model in the pointing chain makes Azleem click the wrong
# answer while reporting the right one. Guarded by tests/test_openrouter.py.
_DEFAULT_POINTING_MODELS = [
    "google/gemma-4-26b-a4b-it:free",   # most available in testing
    "google/gemma-4-31b-it:free",       # equally accurate, rate-limits sooner
]
_DEFAULT_VISION_MODELS = _DEFAULT_POINTING_MODELS + [
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
]


def _model_list(name: str, default: list) -> list:
    configured = _get(name)
    if not configured:
        return list(default)
    return [m.strip() for m in configured.split(",") if m.strip()]


OPENROUTER_POINTING_MODELS: list[str] = _model_list(
    "OPENROUTER_POINTING_MODELS", _DEFAULT_POINTING_MODELS
)
OPENROUTER_VISION_MODELS: list[str] = _model_list(
    "OPENROUTER_VISION_MODELS", _DEFAULT_VISION_MODELS
)

# ---- Quiz answering --------------------------------------------------------
# Hard cap on questions in one answer_quiz run, so a misread page can't loop.
QUIZ_MAX_QUESTIONS: int = max(1, int(_get("QUIZ_MAX_QUESTIONS", "30")))

# ---- Conversation memory ---------------------------------------------------
# How long a command stays available as context for the next one. Follow-ups
# ("now search it for something", "do that again") only make sense while the
# thing being referred to is still the thing you just did; after this many
# idle seconds the history is dropped, so a stale antecedent can never be
# resurrected by a command that meant something else entirely.
HISTORY_IDLE_SECONDS: float = max(0.0, float(_get("HISTORY_IDLE_SECONDS", "180")))

# Conversation turns (a command plus its reply) kept for context. Deliberately
# small: a long transcript costs tokens on every request and gives the router
# more chances to act on something the user has already moved on from.
HISTORY_TURNS: int = max(0, int(_get("HISTORY_TURNS", "5")))

# ---- GitHub (optional) -----------------------------------------------------
# Personal access token with the "gist" scope. When set, solve_with_python
# uploads solutions as secret gists and replies with the shareable link.
GITHUB_TOKEN: str = _get("GITHUB_TOKEN")

# ---- Hotkey ----------------------------------------------------------------
# "caps_lock" -> hold Caps Lock to talk.
# "ctrl_space" -> hold Ctrl+Space to talk (leaves Caps Lock untouched).
HOTKEY: str = _get("HOTKEY", "caps_lock").strip().lower()

# How long the hotkey must be held before the mic opens. Keep this small: the
# mic must be live by the time the user starts speaking, or their command is
# lost and the take captures only background audio. A fraction of a second is
# enough to ignore an accidental brush of the keys. (Caps Lock users may want a
# longer gate so a normal tap still works as Caps Lock.)
HOLD_SECONDS: float = float(_get("HOLD_SECONDS", "0.25"))

# Show the blue recording HUD while capturing. Set to 0/false for headless.
SHOW_OVERLAY: bool = _get("SHOW_OVERLAY", "true").strip().lower() not in (
    "0", "false", "no", "off",
)

# ---- Speech-to-text (faster-whisper) ---------------------------------------
WHISPER_MODEL: str = _get("WHISPER_MODEL", "base")
WHISPER_DEVICE: str = _get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE: str = _get("WHISPER_COMPUTE_TYPE", "int8")

# Beam width for Whisper decoding. Measured on real speech (tests/bench.py),
# 1 (greedy) is about 10% faster than 5 with identical transcription on short
# push-to-talk commands, where the decoder context prompt is already biasing
# proper nouns. Raise it if you dictate long or unusual phrases.
WHISPER_BEAM_SIZE: int = max(1, int(_get("WHISPER_BEAM_SIZE", "1")))

# Sample rate Whisper expects. Do not change unless you know why.
SAMPLE_RATE: int = 16000

# ---- Computer-use agent ----------------------------------------------------
# Pause after each on-screen action before the next screenshot, letting menus
# open and pages paint. Too low and the agent decides against a stale frame;
# too high and every step pays for it.
AGENT_SETTLE_SECONDS: float = float(_get("AGENT_SETTLE_SECONDS", "0.45"))

# Hard cap on actions in one computer-use task, so a confused agent surfaces a
# partial result quickly instead of grinding.
AGENT_MAX_STEPS: int = max(1, int(_get("AGENT_MAX_STEPS", "12")))

# Thinking tokens the model may spend on a single click/type decision. 0 keeps
# per-step latency down and is right for "click the blue button" — but it is
# wrong for anything requiring knowledge, which is why quizzes have their own
# tool rather than riding the computer-use loop. Raise if the agent starts
# making poor decisions on complex screens.
AGENT_THINKING_BUDGET: int = max(0, int(_get("AGENT_THINKING_BUDGET", "0")))

# ---- File search -----------------------------------------------------------
def _downloads_dir() -> Path:
    configured = _get("DOWNLOADS_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Downloads"


DOWNLOADS_DIR: Path = _downloads_dir()

# ---- WhatsApp contacts -----------------------------------------------------
def _load_contacts() -> dict[str, str]:
    raw = _get("WHATSAPP_CONTACTS", "{}")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("WHATSAPP_CONTACTS must be a JSON object")
        # Normalise keys to lower-case for tolerant name matching.
        return {str(k).strip().lower(): str(v).strip() for k, v in data.items()}
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"[config] WARNING: could not parse WHATSAPP_CONTACTS ({exc}); using empty map.")
        return {}


WHATSAPP_CONTACTS: dict[str, str] = _load_contacts()


def validate() -> None:
    """Raise a friendly error if required settings are missing."""
    if not GEMINI_API_KEY or GEMINI_API_KEY == "your_key_here":
        raise SystemExit(
            "\n[Azleem] GEMINI_API_KEY is not set.\n"
            "  1. Copy .env.example to .env\n"
            "  2. Get a free key at https://aistudio.google.com/apikey\n"
            "  3. Put it in .env as GEMINI_API_KEY=...\n"
        )
    # A missing OpenRouter key is a downgrade, not a failure — Gemini alone
    # still works, just against a much tighter daily quota.
    if VISION_PROVIDER == "openrouter" and not OPENROUTER_API_KEY:
        print(
            "[config] WARNING: VISION_PROVIDER=openrouter but OPENROUTER_API_KEY "
            "is not set; falling back to Gemini for vision requests."
        )
    stray = [m for m in OPENROUTER_POINTING_MODELS if "gemma" not in m.lower()]
    if stray:
        print(
            f"[config] WARNING: OPENROUTER_POINTING_MODELS contains {stray}, which "
            "were not measured as accurate enough to click. A model that "
            "mislocates will select the wrong answer while reporting the right one."
        )
