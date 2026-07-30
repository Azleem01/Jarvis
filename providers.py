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

So: vision goes to OpenRouter first and falls back to the full Gemini chain;
routing stays on Gemini. Set ``VISION_PROVIDER=gemini`` in .env to disable
OpenRouter entirely (for instance if you would rather not send screenshots to
free shared endpoints).
"""

from __future__ import annotations

from typing import Any

import config
import gemini_client
import openrouter_client


def _openrouter_first() -> bool:
    return (
        config.VISION_PROVIDER == "openrouter"
        and bool(config.OPENROUTER_API_KEY)
    )


def vision_generate(
    contents: Any, gen_config: Any = None, *, needs_pointing: bool = False
) -> Any:
    """Run a vision request on whichever provider is available.

    Args:
        contents: Payload in google-genai form — the OpenRouter path translates
            it, so callers build one shape regardless of who answers.
        gen_config: Optional ``types.GenerateContentConfig``.
        needs_pointing: True when the reply's bounding box will be *clicked*.
            This restricts OpenRouter to the models measured as accurate enough
            to point; the others answer questions well but mislocate by up to
            231/1000, which would click the wrong thing.

    Returns:
        An object with ``.text``.

    Raises:
        The last provider's error if both are unavailable.
    """
    if _openrouter_first():
        try:
            return openrouter_client.generate_content(
                contents=contents, gen_config=gen_config,
                kind="pointing" if needs_pointing else "vision",
            )
        except openrouter_client.NotConfigured:
            pass
        except (openrouter_client.AllModelsUnavailable,
                openrouter_client.NetworkUnavailable) as exc:
            print(f"[providers] OpenRouter unavailable ({exc}); falling back to Gemini.")

    return gemini_client.generate_content(contents=contents, gen_config=gen_config)


def describe() -> str:
    """One line for the startup banner, so the active setup is never a guess."""
    if not _openrouter_first():
        return "vision: gemini only"
    return (
        f"vision: openrouter ({len(config.OPENROUTER_VISION_MODELS)} models, "
        f"{len(config.OPENROUTER_POINTING_MODELS)} can point) -> gemini fallback"
    )
