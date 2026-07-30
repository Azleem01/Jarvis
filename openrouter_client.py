"""Resilient OpenRouter access, shaped exactly like ``gemini_client``.

OpenRouter is an OpenAI-compatible gateway in front of many vendors. It exists
here for one reason: Gemini's free tier allows 20 requests per day *per model*,
and a single quiz burns more than that, so every command in ``logs/azleem.log``
was ending in a 429. Moving the repetitive vision work onto a second provider
takes that load off entirely.

Callers use the same ``generate_content(contents=..., gen_config=...)`` shape as
``gemini_client`` and get back an object with ``.text``, so no tool needs to know
which provider answered. Translation from the google-genai payload happens here.

Two chains, not one — this is a measured distinction, not a stylistic one:

  * ``POINTING`` models return bounding boxes accurate enough to click. Only the
    Gemma models qualify: benchmarked against generated quiz pages they land
    within ~7/1000 of an option's centre.
  * ``VISION`` models are for reading only. The Nvidia Nemotron free models
    answer questions correctly but mislocate by up to 231/1000 — they would
    click the wrong option, so they must never be used for pointing.

Free models are shared capacity and genuinely flaky: during testing
``gemma-4-31b:free`` returned an upstream 429 after a single request. The
cooldown machinery below is load-bearing, not decoration.

NOTE: uses only the standard library for HTTP. The google-genai SDK is imported
for *type inspection* of the payload, never to make the call.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import config

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_REQUEST_TIMEOUT_S = 90.0

_QUOTA_CODES = {429}
_MISSING_CODES = {404}
_SERVER_CODES = {500, 502, 503, 504}
_RETRYABLE_CODES = _QUOTA_CODES | _MISSING_CODES | _SERVER_CODES

_RETIRED_COOLDOWN = 24 * 3600.0
_SERVER_COOLDOWN = 5.0
# Upstream capacity 429s clear much faster than a real daily-quota exhaustion,
# and on the free tier they are by far the more common of the two.
_UPSTREAM_COOLDOWN = 30.0

_NETWORK_RETRIES = 2
_NETWORK_RETRY_PAUSE_S = 2.0


class AllModelsUnavailable(RuntimeError):
    """Every model in the chain refused the request."""


class NetworkUnavailable(RuntimeError):
    """The OpenRouter host can't be reached at all."""


class NotConfigured(RuntimeError):
    """No OPENROUTER_API_KEY is set — the caller should use Gemini instead."""


def _is_upstream_rate_limit(body: str) -> bool:
    """OpenRouter's 'the provider is busy' 429, distinct from our own quota.

    Seen live as: "google/gemma-4-31b-it:free is temporarily rate-limited
    upstream. Please retry shortly." That clears in seconds, so it must not
    park a good model in a 60s cooldown.
    """
    text = (body or "").lower()
    return "rate-limited upstream" in text or "temporarily rate-limited" in text


def _is_network_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False  # a real reply, just not a happy one
    if isinstance(exc, (urllib.error.URLError, ConnectionError, OSError)):
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "getaddrinfo", "11001", "name or service not known",
            "connection refused", "connection reset", "connection aborted",
            "network is unreachable", "temporary failure in name resolution",
        )
    )


def _is_timeout(exc: Exception) -> bool:
    if "timeout" in type(exc).__name__.lower():
        return True
    return "timed out" in str(exc).lower()


def _retry_delay(body: str) -> float | None:
    match = re.search(r"retry[- ]?after['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)", body or "")
    return float(match.group(1)) if match else None


def _cooldown_for(code: int | None, body: str) -> float:
    if code in _MISSING_CODES:
        return _RETIRED_COOLDOWN
    if code in _SERVER_CODES:
        return _SERVER_COOLDOWN
    if _is_upstream_rate_limit(body):
        return _UPSTREAM_COOLDOWN
    return _retry_delay(body) or config.MODEL_COOLDOWN_SECONDS


# ---- payload translation ---------------------------------------------------
class Response:
    """Minimal stand-in for the google-genai response object.

    Only ``.text`` is ever read by Azleem's tools, so that is all this carries.
    """

    def __init__(self, text: str, model: str) -> None:
        self.text = text
        self.model = model


def _part_to_message_content(part: Any) -> dict | None:
    """One google-genai content part -> one OpenAI message-content entry."""
    if isinstance(part, str):
        return {"type": "text", "text": part}

    # types.Part.from_bytes(...) carries inline_data with mime_type + data.
    inline = getattr(part, "inline_data", None)
    if inline is not None:
        data = getattr(inline, "data", None)
        mime = getattr(inline, "mime_type", None) or "image/jpeg"
        if data:
            import base64

            if isinstance(data, str):  # already base64 in some SDK versions
                b64 = data
            else:
                b64 = base64.b64encode(data).decode()
            return {"type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}}

    text = getattr(part, "text", None)
    if text:
        return {"type": "text", "text": text}
    return None


def build_payload(model: str, contents: Any, gen_config: Any = None) -> dict:
    """Translate a google-genai call into an OpenAI chat-completions body.

    Exposed (rather than private) because the offline tests assert on it — the
    translation is where a silent mistake would cost a live debugging round.
    """
    if not isinstance(contents, (list, tuple)):
        contents = [contents]

    parts = [p for p in (_part_to_message_content(c) for c in contents) if p]
    if not parts:
        parts = [{"type": "text", "text": ""}]

    messages = []
    system = getattr(gen_config, "system_instruction", None) if gen_config else None
    if system:
        messages.append({"role": "system", "content": str(system)})
    messages.append({"role": "user", "content": parts})

    payload: dict = {"model": model, "messages": messages}
    if gen_config is not None:
        temperature = getattr(gen_config, "temperature", None)
        if temperature is not None:
            payload["temperature"] = temperature
        max_tokens = getattr(gen_config, "max_output_tokens", None)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
    payload.setdefault("max_tokens", 1500)
    return payload


class OpenRouterRouter:
    """Tries each model in a chain, remembering which ones are unavailable."""

    def __init__(self, models: list[str] | None = None) -> None:
        self.models = list(models or config.OPENROUTER_VISION_MODELS)
        self._lock = threading.Lock()
        self._cooldown_until: dict[str, float] = {}
        self._preferred: str | None = None

    @property
    def last_model(self) -> str | None:
        with self._lock:
            return self._preferred

    def _order(self) -> list[str]:
        """Available models first (preferred leading), then cooling-down ones."""
        now = time.monotonic()
        with self._lock:
            cooldowns = dict(self._cooldown_until)
            preferred = self._preferred
        ready = [m for m in self.models if cooldowns.get(m, 0.0) <= now]
        waiting = [m for m in self.models if cooldowns.get(m, 0.0) > now]
        waiting.sort(key=lambda m: cooldowns.get(m, 0.0))
        if preferred in ready:
            ready.remove(preferred)
            ready.insert(0, preferred)
        return ready + waiting

    def _mark_unavailable(self, model: str, seconds: float) -> None:
        with self._lock:
            self._cooldown_until[model] = time.monotonic() + seconds
            if self._preferred == model:
                self._preferred = None

    def _mark_working(self, model: str) -> None:
        with self._lock:
            self._preferred = model
            self._cooldown_until.pop(model, None)

    def _post(self, payload: dict) -> str:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            _ENDPOINT, data=body,
            headers={
                "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                # OpenRouter uses these for attribution; harmless and polite.
                "HTTP-Referer": "https://github.com/local/azleem",
                "X-Title": "Azleem desktop assistant",
            },
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_S) as reply:
            return reply.read().decode("utf-8", errors="replace")

    def _request_with_network_retry(self, payload: dict) -> str:
        """One model request, retrying transport failures in place."""
        for attempt in range(_NETWORK_RETRIES + 1):
            try:
                return self._post(payload)
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError) or not _is_network_error(exc):
                    raise
                if attempt == _NETWORK_RETRIES:
                    raise NetworkUnavailable(
                        "Can't reach OpenRouter — the internet connection "
                        f"appears to be down ({exc})."
                    ) from exc
                print(f"[openrouter] network hiccup ({exc}); retrying...")
                time.sleep(_NETWORK_RETRY_PAUSE_S)

    def generate_content(
        self, *, contents: Any, gen_config: Any = None, on_attempt_failed: Any = None
    ) -> Response:
        """Generate content, falling back through the chain as needed."""
        if not config.OPENROUTER_API_KEY:
            raise NotConfigured("OPENROUTER_API_KEY is not set.")

        errors: list[str] = []
        order = self._order()
        for position, model in enumerate(order):
            payload = build_payload(model, contents, gen_config)
            try:
                raw = self._request_with_network_retry(payload)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                code = exc.code
                if code not in _RETRYABLE_CODES:
                    raise RuntimeError(f"OpenRouter {code}: {detail[:200]}") from exc
                self._mark_unavailable(model, _cooldown_for(code, detail))
                errors.append(f"{model}: {code}")
                reason = ("rate limited upstream" if _is_upstream_rate_limit(detail)
                          else "rate limited" if code in _QUOTA_CODES
                          else f"HTTP {code}")
                tail = "trying next model" if position + 1 < len(order) else "no models left"
                print(f"[openrouter] {model} unavailable ({reason}); {tail}.")
                if on_attempt_failed is not None and not on_attempt_failed(model, exc):
                    raise
                continue
            except NetworkUnavailable:
                raise
            except Exception as exc:
                if not _is_timeout(exc):
                    raise
                self._mark_unavailable(model, _SERVER_COOLDOWN)
                errors.append(f"{model}: timeout")
                print(f"[openrouter] {model} unavailable (request timed out).")
                continue

            text = self._extract_text(raw, model)
            if text is None:
                # A 200 with no usable choice: treat like a transient failure
                # rather than handing an empty string back to a caller that is
                # about to parse it as JSON.
                self._mark_unavailable(model, _SERVER_COOLDOWN)
                errors.append(f"{model}: empty response")
                print(f"[openrouter] {model} returned no content; trying next model.")
                continue

            if self._preferred != model:
                print(f"[openrouter] using {model}.")
            self._mark_working(model)
            return Response(text, model)

        raise AllModelsUnavailable(
            f"All OpenRouter models are unavailable right now ({'; '.join(errors)})."
        )

    @staticmethod
    def _extract_text(raw: str, model: str) -> str | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and data.get("error"):
            return None
        choices = (data or {}).get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):  # some providers return parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content if content else None


# ---- Shared singletons -----------------------------------------------------
_routers: dict[str, OpenRouterRouter] = {}
_routers_lock = threading.Lock()


def get_router(kind: str = "vision") -> OpenRouterRouter:
    """The process-wide router for a chain.

    Args:
        kind: ``"vision"`` for read-only work, ``"pointing"`` for anything whose
            bounding box will be clicked. These are deliberately different model
            lists — see the module docstring.
    """
    with _routers_lock:
        router = _routers.get(kind)
        if router is None:
            models = (config.OPENROUTER_POINTING_MODELS if kind == "pointing"
                      else config.OPENROUTER_VISION_MODELS)
            router = OpenRouterRouter(models)
            _routers[kind] = router
        return router


def generate_content(
    *, contents: Any, gen_config: Any = None, kind: str = "vision",
    on_attempt_failed: Any = None,
) -> Response:
    """Convenience wrapper around the shared router for ``kind``."""
    return get_router(kind).generate_content(
        contents=contents, gen_config=gen_config, on_attempt_failed=on_attempt_failed
    )
