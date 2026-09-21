"""What a direct-path LLM failure says about the credential behind it.

``docs/specs/provider-failover.md`` D13a: the adapters simply raise, and the
playbook executor records only the exception's class name, so an auth failure
and a rate limit used to be indistinguishable.  :func:`classify_llm_error`
turns an adapter exception into the ``llm_call`` evidence signal provider
availability tracks under the reserved key ``llm``:

* ``auth`` -- 401/403: the credential is rejected (``unauthenticated``);
* ``usage`` -- a 429 whose body says the quota or credit is spent
  (``exhausted``, ``until`` from ``Retry-After`` when present);
* ``rate_limit`` -- an ordinary 429: never more than ``degraded``;
* ``error`` -- a transport failure, a timeout or a 5xx (counts toward
  ``failing``);
* ``None`` -- a 4xx that is the caller's fault (a bad request) and says
  nothing about the provider.

Duck-typed on purpose: the Anthropic, OpenAI and Google SDKs each spell the
status and the body differently, and importing all three to ``isinstance``
against them would make a missing optional SDK an import error here.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ProviderUnavailableError", "classify_llm_error"]


class ProviderUnavailableError(RuntimeError):
    """The direct path's credential is unavailable; the call was never made.

    Raised by ``LLMClient`` while provider availability holds the reserved
    ``llm`` key in the unavailable half (provider-failover D13a): calls fail
    fast instead of burning a step timeout against a dead credential.  The
    playbook executor reports it as ``provider_error`` with the diagnostic
    ``provider_unavailable``.  Never itself evidence of anything.
    """

_QUOTA_WORDS = (
    "insufficient_quota",
    "quota",
    "credit balance",
    "billing",
    "exceeded your current",
    "resource_exhausted",
)
_TRANSPORT_NAMES = (
    "Timeout",
    "TimeoutError",
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectionError",
    "ReadTimeout",
    "ServiceUnavailable",
    "InternalServerError",
    "OverloadedError",
)


def _status(exc: BaseException) -> int | None:
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value < 600:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except (AttributeError, TypeError):  # an exotic headers object is "no hint"
        return None
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def classify_llm_error(exc: BaseException) -> tuple[str | None, dict[str, Any]]:
    """``(signal, detail)`` for an exception raised by a direct-path adapter."""
    name = type(exc).__name__
    text = str(exc).lower()
    status = _status(exc)
    detail: dict[str, Any] = {"error": name}
    if status is not None:
        detail["status"] = status
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return "auth", detail
    if status == 429 or name == "RateLimitError":
        retry_after = _retry_after(exc)
        if retry_after is not None:
            detail["retry_after"] = retry_after
        if any(word in text for word in _QUOTA_WORDS):
            return "usage", detail
        return "rate_limit", detail
    if (status is not None and status >= 500) or name in _TRANSPORT_NAMES or isinstance(
        exc, (TimeoutError, ConnectionError)
    ):
        return "error", detail
    return None, detail
