"""Deliver one frozen message without assuming a network receipt."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from src.escalations.plan import MAX_ATTEMPTS, backoff_for
from src.escalations.transport import (
    EscalationTransport,
    SendOutcome,
    TransportAmbiguous,
    TransportError,
    TransportRetryable,
    TransportUnavailable,
)

logger = logging.getLogger(__name__)


#: Base-4 zero-width alphabet: space, non-joiner, joiner, word joiner.
_ZW_DIGITS = ("\u200b", "\u200c", "\u200d", "\u2060")
_ZW_START = "\u2063"  # invisible separator
#: One-digit kind tags; any other prefix shares the last tag.
_KINDS = {"aq-out": 0, "aq-dig": 1, "aq-conv": 2}
_HEX = "0123456789abcdef"


def _fold(value: str) -> str:
    if len(value) == 16 and all(ch in _HEX for ch in value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def invisible(marker: str) -> str:
    """Encode a reconciliation marker as a compact run of zero-width characters.

    ``prefix:payload`` becomes a start character, one kind digit and the
    16-hex fold of the payload at two base-4 digits per hex digit (34 chars).
    Discord keeps these characters, so a marker search still matches, but a
    reader never sees the marker.
    """
    prefix, _, payload = marker.partition(":")
    kind = _KINDS.get(prefix, 3)
    body = "".join(
        _ZW_DIGITS[n >> 2] + _ZW_DIGITS[n & 3] for n in (_HEX.index(c) for c in _fold(payload))
    )
    return _ZW_START + _ZW_DIGITS[kind] + body


def operation_marker(owner_id: str, *, prefix: str) -> str:
    fold = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]
    return invisible(f"{prefix}:{fold}")


@dataclass(frozen=True)
class FrozenMessage:
    channel_id: str
    text: str
    marker: str
    attempt_count: int
    thread_id: str | None = None
    last_error: str | None = None
    reclaimed: bool = False
    #: The message this post replies to, for a chat channel that has no thread
    #: to hold the context.  Ignored when ``thread_id`` is set.
    reference_message_id: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    status: Literal["sent", "retry", "unknown"]
    receipt_id: str | None = None
    next_attempt_at: float | None = None
    last_error: str | None = None


class MessageDelivery:
    def __init__(
        self,
        transport: EscalationTransport,
        *,
        clock: Callable[[], float],
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self.transport = transport
        self.clock = clock
        self.max_attempts = max_attempts

    async def reconcile(self, message: FrozenMessage) -> SendOutcome | None:
        try:
            return await self.transport.find_marker(
                channel_id=message.channel_id, thread_id=message.thread_id, marker=message.marker
            )
        except Exception:
            logger.debug("message marker reconciliation unavailable", exc_info=True)
            return None

    def failure(self, message: FrozenMessage, error: str, *, retryable: bool) -> DeliveryResult:
        if retryable and message.attempt_count < self.max_attempts:
            return DeliveryResult(
                "retry",
                next_attempt_at=self.clock() + backoff_for(message.attempt_count),
                last_error=error,
            )
        return DeliveryResult("unknown", last_error=error)

    async def deliver(self, message: FrozenMessage) -> DeliveryResult:
        if message.attempt_count > 1:
            found = await self.reconcile(message)
            if found is not None:
                return DeliveryResult("sent", receipt_id=found.receipt_id)
            if message.reclaimed or (message.last_error or "").startswith("ambiguous:"):
                return DeliveryResult(
                    "unknown",
                    last_error=(
                        "an earlier send was ambiguous and no matching message was found; "
                        "delivery ownership is unknown and nothing was reposted"
                    ),
                )
        try:
            content = f"{message.text}{message.marker}"
            if message.thread_id:
                outcome = await self.transport.post_thread_message(
                    thread_id=message.thread_id, content=content
                )
            elif message.reference_message_id:
                outcome = await self.transport.post_root(
                    channel_id=message.channel_id,
                    content=content,
                    reference_message_id=message.reference_message_id,
                )
            else:
                outcome = await self.transport.post_root(
                    channel_id=message.channel_id, content=content
                )
        except TransportAmbiguous as exc:
            found = await self.reconcile(message)
            if found is not None:
                return DeliveryResult("sent", receipt_id=found.receipt_id)
            return DeliveryResult(
                "unknown",
                last_error=(
                    f"ambiguous: {exc}; delivery ownership is unknown and nothing was reposted"
                ),
            )
        except (TransportRetryable, TransportUnavailable) as exc:
            return self.failure(message, str(exc), retryable=True)
        except TransportError as exc:
            return self.failure(message, str(exc), retryable=False)
        except Exception:
            # An unclassified exception can occur after the write left the process.
            found = await self.reconcile(message)
            if found is not None:
                return DeliveryResult("sent", receipt_id=found.receipt_id)
            return DeliveryResult("unknown", last_error="ambiguous: unexpected transport failure")
        return DeliveryResult("sent", receipt_id=outcome.receipt_id)
