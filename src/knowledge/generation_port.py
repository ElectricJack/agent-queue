"""Stateless optional aq-memory generation adapter; legacy writers stay fenced."""

from src.records.models import RecordError


class MemoryExtractionPort:
    """Wrap only a plugin's proposal callback and local cost estimator.

    The separate aq-memory package supplies these callbacks from its model
    backend. Never pass ``_process_batch``/``extract`` from its legacy writer:
    those own destructive buffers and guidance writes. This port is deliberately
    unable to access a memory service, event bus, store, KV or promotion method.
    """

    def __init__(self, propose, estimate, *, provider_id, provider_version, available=True):
        self._propose, self._estimate = propose, estimate
        self.provider_id, self.provider_version = provider_id, provider_version
        self.available = available
        self.deprecated = False

    def estimate(self, request):
        return self._estimate(request.wire())

    async def generate(self, request):
        result = await self._propose(request.wire())
        if not isinstance(result, dict):
            raise RecordError("extraction.invalid_output")
        return result

    def handshake(self):
        return dict(
            adapter="knowledge-extraction",
            handshake_version=1,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            authoritative_writes=False,
            legacy_watchers_fenced=True,
            output="unverified-proposals",
            paid_call_exactly_once=False,
        )
