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


def operation_marker(owner_id: str, *, prefix: str) -> str:
    fold = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{fold}"


@dataclass(frozen=True)
class FrozenMessage:
    channel_id: str
    text: str
    marker: str
    attempt_count: int
    thread_id: str | None = None
    last_error: str | None = None
    reclaimed: bool = False


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
            content = f"{message.text}\n{message.marker}"
            if message.thread_id:
                outcome = await self.transport.post_thread_message(
                    thread_id=message.thread_id, content=content
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
