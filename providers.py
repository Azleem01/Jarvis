"""Which provider serves which kind of request.

Azleem makes two very different sorts of model call, and measurement says they
want different providers:

  * **Routing** — "which tool does this command mean?". Gemini is measurably
    better: with the real system prompt and tool schemas it routes
    "solve the quiz question on my screen and move to the next one" correctly,
    while every free OpenRouter model tested got it wrong (Gemma reached for
    read_screen; gpt-oss-20b called nothing). Routing therefore stays on
    ``gemini_client`` and does not come through this module at all.

  * **Vision** — reading the screen, answering an on-screen question, locating
    something to click. The free Gemma models on OpenRouter answered 16/16
    benchmark questions correctly across five different quiz layouts, and point
    within ~7/1000 of an option's centre. This is also the call Azleem makes
    dozens of times per task, so it is the one worth moving off Gemini's
    20-per-day-per-model free cap.

So: routing stays on Gemini. Vision tries one provider first and falls back to
the other, in whichever order ``VISION_PROVIDER`` sets:

  * ``gemini`` (default on paid credits) — Gemini's own fast, reliable vision
    first, free OpenRouter as the safety net if the whole Gemini chain is down.
  * ``openrouter`` — the free Gemma models first to save credits, Gemini as the
    fallback.

Either way the other provider is a genuine fallback, not disabled: the only
thing that turns OpenRouter off is having no ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

from typing import Any

import config
import gemini_client
import openrouter_client


def _openrouter_available() -> bool:
    return bool(config.OPENROUTER_API_KEY)


def _openrouter_first() -> bool:
    return config.VISION_PROVIDER == "openrouter" and _openrouter_available()


def _gemini_vision(contents: Any, gen_config: Any) -> Any:
    return gemini_client.generate_content(contents=contents, gen_config=gen_config)


def _openrouter_vision(contents: Any, gen_config: Any, needs_pointing: bool) -> Any:
    return openrouter_client.generate_content(
        contents=contents, gen_config=gen_config,
        kind="pointing" if needs_pointing else "vision",
    )


def vision_generate(
    contents: Any, gen_config: Any = None, *, needs_pointing: bool = False
) -> Any:
    """Run a vision request, preferring one provider and falling back to the other.

    Order is set by ``config.VISION_PROVIDER`` (see the module docstring). Only a
    provider being *unavailable* (out of quota / unreachable / not configured)
    triggers the fallback; a hard error the other provider would also reject
    (bad key, malformed request) propagates instead of being retried blindly.

    Args:
        contents: Payload in google-genai form — the OpenRouter path translates
            it, so callers build one shape regardless of who answers.
        gen_config: Optional ``types.GenerateContentConfig``.
        needs_pointing: True when the reply's bounding box will be *clicked*.
            This restricts OpenRouter to the models measured as accurate enough
            to point; the others answer questions well but mislocate by up to
            231/1000, which would click the wrong thing. (Gemini points fine.)

    Returns:
        An object with ``.text``.

    Raises:
        The last provider's error if both are unavailable.
    """
    if _openrouter_first():
        # Free models first, Gemini as the fallback.
        try:
            return _openrouter_vision(contents, gen_config, needs_pointing)
        except openrouter_client.NotConfigured:
            pass
        except (openrouter_client.AllModelsUnavailable,
                openrouter_client.NetworkUnavailable) as exc:
            print(f"[providers] OpenRouter unavailable ({exc}); falling back to Gemini.")
        return _gemini_vision(contents, gen_config)

    # Gemini first. Fall back to the free OpenRouter models only if the whole
    # paid Gemini chain is down and a key is configured.
    if not _openrouter_available():
        return _gemini_vision(contents, gen_config)
    try:
        return _gemini_vision(contents, gen_config)
    except (gemini_client.AllModelsUnavailable,
            gemini_client.NetworkUnavailable) as exc:
        print(f"[providers] Gemini unavailable ({exc}); falling back to OpenRouter.")
        return _openrouter_vision(contents, gen_config, needs_pointing)


def describe() -> str:
    """One line for the startup banner, so the active setup is never a guess."""
    n_vision = len(config.OPENROUTER_VISION_MODELS)
    n_point = len(config.OPENROUTER_POINTING_MODELS)
    if _openrouter_first():
        return (
            f"vision: openrouter ({n_vision} models, {n_point} can point) "
            "-> gemini fallback"
        )
    if _openrouter_available():
        return f"vision: gemini -> openrouter fallback ({n_vision} free models)"
    return "vision: gemini only"
