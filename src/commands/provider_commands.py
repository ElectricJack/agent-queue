"""Provider quota commands mixin for CommandHandler.

``provider_usage_probe`` is the active half of the provider-usage feature
(``docs/superpowers/specs/2026-09-07-provider-usage-design.md``): Codex
publishes its rate limits passively on the transcript lines the watcher
already reads, but Claude has no local equivalent, so the shipped
``provider-usage-probe`` playbook calls this command on a ten-minute timer.

The command is deliberately dull.  It runs one subprocess, hands the text to
a pure parser, writes whatever snapshots came back, and records its own
verdict so ``aq doctor --check providers.claude_usage`` can tell "the CLI's
wording moved" from "nobody has probed lately".  Every failure path returns
``success: False`` *without* writing anything, so the last good reading
survives a timeout, a crash, or a CLI that started printing something else.
"""

from __future__ import annotations

import logging
import time

from src.providers import probe as probe_module
from src.providers.probe import NOT_APPLICABLE

logger = logging.getLogger(__name__)

#: The providers this command knows how to interrogate.  Codex is absent on
#: purpose: it needs no probe, and inventing a reading for an idle Codex
#: session is exactly the "frozen number that looks live" the design forbids.
PROBEABLE = ("claude",)


class ProviderCommandsMixin:
    """Mixin that adds provider quota probes to CommandHandler."""

    async def _cmd_provider_usage_probe(self, args: dict) -> dict:
        """Ask a provider's CLI what is left of the account's limit windows.

        Args:
            provider: Which provider to probe. Only ``claude`` today; the
                default.

        Returns:
            ``outcome`` is ``probed`` (snapshots written), ``unparsed`` (the
            CLI answered with something no limit-line regex matched),
            ``not_applicable`` (an API-key account has no window to report),
            or ``unavailable`` (the CLI is absent or timed out) — all
            four with ``success: True``, because none of them is a broken
            step an operator can act on. A non-zero exit or an
            unreadable body returns ``success: False`` and writes no
            snapshot.
        """
        provider = str(args.get("provider") or "claude").strip().lower()
        if provider not in PROBEABLE:
            return {
                "success": False,
                "error": f"unknown provider {provider!r}; probeable: {', '.join(PROBEABLE)}",
            }

        settings = getattr(self.config, "providers", None)
        claude = getattr(settings, "claude", None)
        if claude is not None and not claude.usage_probe_enabled:
            # Disabled is a decision, not a fault.  Report it as a healthy
            # no-op so a still-active timer does not look like a failure.
            return await self._finish_probe(
                provider,
                {
                    "success": True,
                    "outcome": "disabled",
                    "provider": provider,
                    "snapshots": [],
                    "recorded": 0,
                    "unparsed": False,
                    "detail": "providers.claude.usage_probe_enabled is false",
                },
            )

        result = await probe_module.probe_claude_usage(
            binary=getattr(claude, "binary", "claude"),
            cwd=getattr(self.config, "data_dir", None) or None,
        )

        if not result.ok:
            return await self._finish_probe(
                provider,
                {
                    "success": False,
                    "error": result.error or result.outcome,
                    "reason": result.outcome,
                    "provider": provider,
                    "snapshots": [],
                    "recorded": 0,
                    "unparsed": False,
                },
            )

        recorded = 0
        if result.snapshots:
            recorded = await self.db.record_provider_usage(result.snapshots)

        payload: dict = {
            "success": True,
            "outcome": result.outcome,
            "provider": provider,
            "snapshots": [_public(snapshot) for snapshot in result.snapshots],
            "recorded": int(recorded),
            "unparsed": result.unparsed,
        }
        if result.detail:
            payload["detail"] = result.detail
        return await self._finish_probe(provider, payload)

    async def _finish_probe(self, provider: str, payload: dict) -> dict:
        """Persist the probe's own verdict, then return *payload* unchanged.

        Written on **every** probe, success or failure: the doctor check has
        no other way to distinguish a probe whose output stopped parsing
        from one that has not run.  A failure to record the verdict must not
        turn a good probe into a bad one, so it is logged and swallowed.
        """
        health = {
            "ok": bool(payload.get("success")),
            "outcome": str(payload.get("outcome") or payload.get("reason") or "unknown"),
            "unparsed": bool(payload.get("unparsed")),
            "not_applicable": payload.get("outcome") == NOT_APPLICABLE,
            "error": payload.get("error"),
            "detail": payload.get("detail"),
            "recorded": int(payload.get("recorded") or 0),
            "ts": time.time(),
        }
        try:
            await self.db.record_probe_health(provider, health)
        except Exception:  # pragma: no cover - defensive
            logger.debug("could not record %s probe health", provider, exc_info=True)
        return payload


def _public(snapshot) -> dict:
    """One snapshot as the wire sees it."""
    return {
        "provider": snapshot.provider,
        "account_label": snapshot.account_label,
        "window": snapshot.window,
        "scope": snapshot.scope,
        "used_percent": snapshot.used_percent,
        "resets_at": snapshot.resets_at,
        "observed_at": snapshot.observed_at,
        "source": snapshot.source,
    }


__all__ = ["PROBEABLE", "ProviderCommandsMixin"]
