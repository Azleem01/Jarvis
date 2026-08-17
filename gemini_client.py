"""Resilient Gemini access with automatic model fallback.

A single shared client plus a router that walks ``config.GEMINI_MODEL_CHAIN``
until a model answers. This exists because free-tier keys hit per-minute and
per-day quotas constantly, and individual model IDs get retired without notice
— pinning one model makes the assistant fail for reasons that have nothing to
do with the user's command.

What triggers a switch to the next model:

  * ``429 RESOURCE_EXHAUSTED`` — rate limited or out of quota.
  * ``404 NOT_FOUND``         — model retired or not enabled for this key.
  * ``500 / 502 / 503 / 504`` — transient server-side trouble.

What does *not*: auth failures (``401``/``403``) and malformed requests
(``400``), which would fail identically on every model — those raise straight
away so the real problem stays visible.

Rate-limited models go into a short cooldown so the next command skips them
instead of paying the same 429 again, and the last model that worked is tried
first. Cooldowns honour the API's own ``retryDelay`` when it sends one.

Usage::

    import gemini_client
    response = gemini_client.generate_content(contents="hello")
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from google import genai
from google.genai import types as genai_types

import cancellation
import config

# Per-request HTTP timeout (ms). Without one the SDK waits forever, and a
# single wedged connection freezes the whole assistant — observed live: a
# fallback attempt hung >30s while the same model answered a fresh client in
# 4.5s. On paid credits a real answer (even a tool-calling or vision turn)
# comes back in a few seconds, so this caps the worst-case wedged request at
# 20s instead of 60s — the ceiling the user feels when they press Esc and the
# thread is stuck mid-call. Raise it if legitimate slow turns start timing out.
_REQUEST_TIMEOUT_MS = 20_000

# Status codes worth retrying on a different model.
_QUOTA_CODES = {429}
_MISSING_CODES = {404}
_SERVER_CODES = {500, 502, 503, 504}
_RETRYABLE_CODES = _QUOTA_CODES | _MISSING_CODES | _SERVER_CODES

# A model that 404s is gone for the whole session, not just cooling off.
_RETIRED_COOLDOWN = 24 * 3600.0
# Transient server errors clear quickly.
_SERVER_COOLDOWN = 5.0


class AllModelsUnavailable(RuntimeError):
    """Every model in the chain refused the request."""


class NetworkUnavailable(RuntimeError):
    """The API host can't be reached at all (DNS/connection failure).

    Distinct from AllModelsUnavailable: every model shares the same host, so
    walking the chain is pointless — the caller should tell the user their
    internet connection is the problem.
    """


# Same-model retries for transient network failures (DNS blips, dropped
# connections). Short: a hard outage should fail fast with a clear message.
_NETWORK_RETRIES = 2
_NETWORK_RETRY_PAUSE_S = 2.0


def _is_network_error(exc: Exception) -> bool:
    """Whether this exception is a transport-level failure, not an API reply."""
    try:
        import httpx

        if isinstance(exc, httpx.TransportError):
            return True
    except ImportError:
        pass
    if isinstance(exc, (ConnectionError, OSError)):
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
    """Whether this exception is a request timeout (httpx or SDK flavoured)."""
    if "timeout" in type(exc).__name__.lower():
        return True
    return "timed out" in str(exc).lower()


def _status_code(exc: Exception) -> int | None:
    """Best-effort HTTP status for a google-genai error."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    match = re.search(r"\b([45]\d\d)\b", str(exc))
    return int(match.group(1)) if match else None


def _retry_delay(exc: Exception) -> float | None:
    """Pull Google's suggested retry delay (e.g. 'retryDelay': '31s') if present."""
    match = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(exc))
    return float(match.group(1)) if match else None


def _cooldown_for(exc: Exception, code: int | None) -> float:
    if code in _MISSING_CODES:
        return _RETIRED_COOLDOWN
    if code in _SERVER_CODES:
        return _SERVER_COOLDOWN
    # Quota: prefer the server's own advice, else the configured default.
    return _retry_delay(exc) or config.MODEL_COOLDOWN_SECONDS


class ModelRouter:
    """Tries each model in the chain, remembering which ones are unavailable."""

    def __init__(self, models: list[str] | None = None) -> None:
        self.models = list(models or config.GEMINI_MODEL_CHAIN)
        if not self.models:
            raise ValueError("No Gemini models configured (GEMINI_MODEL_CHAIN is empty).")
        self._client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options=genai_types.HttpOptions(timeout=_REQUEST_TIMEOUT_MS),
        )
        self._lock = threading.Lock()
        # model -> monotonic timestamp when it becomes usable again
        self._cooldown_until: dict[str, float] = {}
        # Model that last succeeded; tried first on the next request.
        self._preferred: str | None = None

    @property
    def client(self) -> genai.Client:
        return self._client

    @property
    def last_model(self) -> str | None:
        """The model that most recently answered successfully."""
        with self._lock:
            return self._preferred

    # -- model ordering ------------------------------------------------------
    def _order(self) -> list[str]:
        """Available models first (preferred leading), then cooling-down ones.

        Cooling-down models stay on the list as a last resort: a stale cooldown
        is a far better outcome than refusing to answer at all.
        """
        now = time.monotonic()
        with self._lock:
            cooldowns = dict(self._cooldown_until)
            preferred = self._preferred

        ready = [m for m in self.models if cooldowns.get(m, 0.0) <= now]
        waiting = [m for m in self.models if cooldowns.get(m, 0.0) > now]
        # Soonest-to-recover first among the cooling-down models.
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

    # -- request -------------------------------------------------------------
    def _generate_with_network_retry(self, model: str, contents, gen_config):  # noqa: ANN001
        """One model request, retrying transient network failures in place.

        Model fallback can't help with a dead connection (same host for every
        model), so DNS blips and dropped sockets get a couple of quick retries
        here; a persistent outage raises NetworkUnavailable for a clear,
        human-readable failure instead of a chain walk.
        """
        for attempt in range(_NETWORK_RETRIES + 1):
            try:
                return self._client.models.generate_content(
                    model=model, contents=contents, config=gen_config
                )
            except Exception as exc:
                if _status_code(exc) is not None or not _is_network_error(exc):
                    raise  # a real API reply (or non-network error) — handle upstream
                if attempt == _NETWORK_RETRIES:
                    raise NetworkUnavailable(
                        "Can't reach the Gemini API — the internet connection "
                        f"appears to be down ({exc})."
                    ) from exc
                print(f"[gemini] network hiccup ({exc}); retrying...")
                time.sleep(_NETWORK_RETRY_PAUSE_S)

    def generate_content(
        self,
        *,
        contents: Any,
        gen_config: Any = None,
        on_attempt_failed: Any = None,
    ) -> Any:
        """Generate content, falling back through the model chain as needed.

        Args:
            contents: Prompt payload, exactly as ``client.models.generate_content``
                takes it (string, Part list, etc.).
            gen_config: Optional ``types.GenerateContentConfig``.
            on_attempt_failed: Optional callback ``(model, exc) -> bool``. Return
                False to stop falling back after that failure — used by callers
                whose tools have already caused side effects and must not be
                re-run on another model.

        Returns:
            The SDK response. ``router.last_model`` names the model that
            produced it (the response object itself is a strict Pydantic model
            and rejects extra attributes).

        Raises:
            AllModelsUnavailable: every model failed with a retryable error.
            Exception: the original error for non-retryable failures (bad key,
                malformed request), which no other model would survive either.
        """
        errors: list[str] = []
        order = self._order()
        for position, model in enumerate(order):
            # Esc/corner cancel: stop walking the chain the moment the user
            # aborts, so a cancelled command doesn't pay out the remaining
            # models' timeouts before the worker can release its slot. (A single
            # in-flight request can't be interrupted, but the per-request timeout
            # caps how long that lasts; this stops everything after it.)
            if cancellation.cancelled():
                raise cancellation.Cancelled("cancelled before contacting Gemini")
            try:
                response = self._generate_with_network_retry(
                    model, contents, gen_config
                )
            except Exception as exc:
                code = _status_code(exc)
                timed_out = _is_timeout(exc)
                if code not in _RETRYABLE_CODES and not timed_out:
                    raise  # auth/validation problem — every model would reject it
                if timed_out and code is None:
                    # A hung request says nothing about the model's health;
                    # brief cooldown, same as a transient server error.
                    self._mark_unavailable(model, _SERVER_COOLDOWN)
                    errors.append(f"{model}: timeout")
                    reason = "request timed out"
                else:
                    self._mark_unavailable(model, _cooldown_for(exc, code))
                    errors.append(f"{model}: {code}")
                    reason = "rate limited" if code in _QUOTA_CODES else f"HTTP {code}"
                more = position + 1 < len(order)
                tail = "trying next model" if more else "no models left"
                print(f"[gemini] {model} unavailable ({reason}); {tail}.")
                if on_attempt_failed is not None and not on_attempt_failed(model, exc):
                    raise
                continue

            if self._preferred != model:
                print(f"[gemini] using {model}.")
            self._mark_working(model)
            return response

        raise AllModelsUnavailable(
            "All Gemini models are unavailable right now "
            f"({'; '.join(errors)}). Check your quota at "
            "https://aistudio.google.com/apikey"
        )


# ---- Shared singleton ------------------------------------------------------
_router: ModelRouter | None = None
_router_lock = threading.Lock()


def get_router() -> ModelRouter:
    """The process-wide router (one client, one shared cooldown map)."""
    global _router
    with _router_lock:
        if _router is None:
            _router = ModelRouter()
        return _router


def generate_content(
    *, contents: Any, gen_config: Any = None, on_attempt_failed: Any = None
) -> Any:
    """Convenience wrapper around the shared router."""
    return get_router().generate_content(
        contents=contents, gen_config=gen_config, on_attempt_failed=on_attempt_failed
    )
