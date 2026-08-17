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
# what Jarvis needs (routing + on-screen element location).
#
# Ordering is "fastest-that-works first, with a capability tier reachable" —
# *not* strongest-first. On paid credits the flash primary answers almost
# everything in a couple of seconds; the chain only walks on an outage. So the
# fast flash models lead, one pro model sits mid-chain as a capability/reliability
# escalation for when the flash tier is genuinely unavailable, and the "-lite"
# and "-latest" aliases sit at the end as last resorts (they draw on separate
# quota pools from the numbered models). All IDs verified against the live
# models.list() API — a non-existent primary 404s into a 24 h retirement.
_DEFAULT_FALLBACK_MODELS = [
    "gemini-3.6-flash",         # fast + capable — carries almost all traffic
    "gemini-3.5-flash",         # fast fallback, separate quota
    "gemini-flash-latest",      # fast alias, separate quota pool
    "gemini-pro-latest",        # capability/reliability escalation (slower)
    "gemini-3.5-flash-lite",    # cheap, quick last resorts
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
#
# Default is "gemini": with paid credits the reliable, fast path is Gemini's own
# vision, and the free shared OpenRouter endpoints (which stall/429 before
# answering) become the fallback rather than the first thing tried. Set
# "openrouter" to prefer the free models first and spend Gemini credits only on
# fallback.
VISION_PROVIDER: str = _get("VISION_PROVIDER", "gemini").strip().lower()

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
HISTORY_TURNS: int = max(0, int(_get("HISTORY_TURNS", "8")))

# Where the rolling history is persisted so it survives a restart (Azleem
# restarts itself for self-extension). Reloaded on startup only if still inside
# HISTORY_IDLE_SECONDS, so a restart hours later still starts fresh. Git-ignored
# under logs/.
HISTORY_FILE: Path = Path(
    _get("HISTORY_FILE", str(Path(__file__).resolve().parent / "logs" / "history.json"))
)

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
# Default is 'small.en': a large accuracy jump over 'base' on proper nouns like
# 'Jarvis' and contact names, still fine on CPU/int8. 'language' is fixed to
# English below, so the '.en' variant is the right one. For maximum accuracy set
# 'distil-large-v3' (bigger download + more RAM); for maximum speed, 'base.en'.
WHISPER_MODEL: str = _get("WHISPER_MODEL", "small.en")
WHISPER_DEVICE: str = _get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE: str = _get("WHISPER_COMPUTE_TYPE", "int8")

# Optional JSON map of transcription mishears -> corrections, applied after
# decoding (merged with a small built-in set for the assistant's own name, e.g.
# 'diabetes' -> 'Jarvis'). Example: {"travis":"Jarvis","javis":"Jarvis"}
STT_CORRECTIONS: str = _get("STT_CORRECTIONS", "")

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

# Hard cap on actions in one computer-use task. Raised to 25 so genuinely
# multi-step flows (multi-page forms, job applications) can finish; a confused
# agent is still bounded by the oscillation guard long before this.
AGENT_MAX_STEPS: int = max(1, int(_get("AGENT_MAX_STEPS", "25")))

# Thinking tokens the model may spend on a single click/type decision. 0 keeps
# per-step latency down and is right for "click the blue button" — but it is
# wrong for anything requiring knowledge, which is why quizzes have their own
# tool rather than riding the computer-use loop. Raise if the agent starts
# making poor decisions on complex screens.
AGENT_THINKING_BUDGET: int = max(0, int(_get("AGENT_THINKING_BUDGET", "0")))

# Thinking budget the loop escalates to once a step has visibly stalled (the
# oscillation guard trips). The common path stays at AGENT_THINKING_BUDGET for
# speed; only when the agent is stuck does it "stop and think" its way out
# instead of aborting. 0 disables the escalation.
AGENT_STALL_THINKING_BUDGET: int = max(0, int(_get("AGENT_STALL_THINKING_BUDGET", "1024")))

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

# Local cache of contacts Azleem discovers itself while messaging by name (the
# canonical spelling it saw on screen, plus any number it already knows). Lives
# under logs/ (git-ignored) so the user's contacts stay private and out of
# version control. Purely additive — it never overwrites WHATSAPP_CONTACTS.
WHATSAPP_CONTACT_CACHE: Path = Path(
    _get(
        "WHATSAPP_CONTACT_CACHE",
        str(Path(__file__).resolve().parent / "logs" / "whatsapp_contacts.json"),
    )
)

# Timings for the WhatsApp send path (previously hardcoded inside the tool).
# SEND_TIMEOUT: how long to wait for the app window to come to the front.
# SETTLE_SECONDS: the pause that lets the chat / message box populate before
# typing or pressing Enter. MAX_SCROLLS caps capture_whatsapp_contacts so a
# runaway list can never scroll forever.
WHATSAPP_SEND_TIMEOUT: float = float(_get("WHATSAPP_SEND_TIMEOUT", "8.0"))
WHATSAPP_SETTLE_SECONDS: float = float(_get("WHATSAPP_SETTLE_SECONDS", "0.6"))
WHATSAPP_MAX_SCROLLS: int = int(_get("WHATSAPP_MAX_SCROLLS", "40"))

# Fuzzy contact matching (tools/contacts.py). THRESHOLD is difflib's cutoff for
# calling a name "close"; MARGIN is how far ahead the top candidate must be to
# auto-pick rather than ask which person was meant. Higher = stricter / asks
# more often. The point is never to message the wrong person on a shaky guess.
CONTACT_MATCH_THRESHOLD: float = float(_get("CONTACT_MATCH_THRESHOLD", "0.72"))
CONTACT_MATCH_MARGIN: float = float(_get("CONTACT_MATCH_MARGIN", "0.12"))

# The user's own WhatsApp name / a self-chat contact, so "send this file to my
# phone" knows where to go without naming a contact (a file sent to yourself
# lands on your phone's WhatsApp). Empty = ask who to send to.
WHATSAPP_SELF_NAME: str = _get("WHATSAPP_SELF_NAME", "")

# ---- Self-extension (Azleem writing its own tools) -------------------------
# Master switch for add_capability, which lets Azleem write a NEW tool into its
# own tools/generated/ package and restart to load it. Off makes the tool
# refuse honestly rather than touch its own source.
SELF_EXTEND_ENABLED: bool = _get("SELF_EXTEND_ENABLED", "true").strip().lower() not in (
    "0", "false", "no", "off",
)

# How long the pre-install safety checks (import + Gemini-schema build +
# tests/test_prompts) may run before a self-extension is refused as unverified.
# The whole point of the gate is that a broken new tool never becomes live, so
# an unverifiable one is treated as a failed one.
SELF_EXTEND_TEST_TIMEOUT: float = float(_get("SELF_EXTEND_TEST_TIMEOUT", "180"))


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
