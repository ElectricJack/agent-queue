"""Lightweight patch boundary for external provider login probes.

Shared test fixtures replace this boundary without importing the availability
service and its application dependencies. The service owns the real probe;
normal calls delegate to it with the original provider and timeout unchanged.
"""

from __future__ import annotations

from typing import Any


class LoginProbeMixin:
    """Separate the replaceable external probe from availability orchestration."""

    async def _login_probe(self, provider: str, timeout: float) -> tuple[str | None, dict[str, Any]]:
        return await self._perform_login_probe(provider, timeout)

    async def _perform_login_probe(
        self, provider: str, timeout: float
    ) -> tuple[str | None, dict[str, Any]]:
        raise NotImplementedError
