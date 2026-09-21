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

The rest of the mixin is the operator surface of provider *availability*
(``docs/specs/provider-failover.md`` D6, D19, D20): ``provider_status``,
``provider_history``, ``provider_recheck`` and ``provider_set_state`` read and
steer ``Orchestrator.provider_availability``; ``provider_availability_notify``
is the idempotent state-change notice a playbook may also drive.
``provider_reroute`` / ``provider_reroute_undo`` drive the re-route engine
(``Orchestrator.provider_reroute``, D11-D16).  None of them holds state of
its own -- the services own every write.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from src.providers import probe as probe_module
from src.providers.probe import NOT_APPLICABLE

logger = logging.getLogger(__name__)

#: The providers this command knows how to interrogate.  Codex is absent on
#: purpose: it needs no probe, and inventing a reading for an idle Codex
#: session is exactly the "frozen number that looks live" the design forbids.
PROBEABLE = ("claude",)

#: ``provider_set_state`` states (D6): two overrides and ``auto`` to clear.
SET_STATES = ("disabled", "available", "auto")
#: How many transitions ``provider_status --verbose`` prints per provider.
VERBOSE_TRANSITIONS = 10

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw]?)", re.IGNORECASE)
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: Any) -> float:
    """Seconds in *value*: a number, or ``4h`` / ``30m`` / ``90s`` / ``1h30m``.

    Raises ``ValueError`` on anything else -- an override whose length was
    misread is worse than a refused one.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        text = str(value or "").strip().replace(" ", "")
        if not text:
            raise ValueError("empty duration")
        pos = 0
        seconds = 0.0
        for match in _DURATION_RE.finditer(text):
            if match.start() != pos:
                raise ValueError(f"not a duration: {value!r}")
            seconds += float(match.group(1)) * _DURATION_UNITS[match.group(2).lower()]
            pos = match.end()
        if pos != len(text) or pos == 0:
            raise ValueError(f"not a duration: {value!r} (use e.g. 90s, 30m, 4h, 2d)")
    if seconds <= 0:
        raise ValueError("duration must be positive")
    return seconds


def parse_timestamp(value: Any) -> float:
    """An epoch from a number or an ISO-8601 string (naive means local time)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise ValueError(f"not a timestamp: {value!r} (epoch seconds or ISO-8601)") from exc


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

    # -- provider availability (docs/specs/provider-failover.md) ------------

    def _availability(self):
        return getattr(getattr(self, "orchestrator", None), "provider_availability", None)

    async def _resolve_provider_arg(self, service, raw: Any) -> tuple[str | None, dict | None]:
        """A known provider key for *raw* (vendor aliases accepted), or an error."""
        name = str(raw or "").strip()
        if not name:
            return None, {"success": False, "error": "provider is required"}
        known = await service.tracked_providers()
        provider = service.resolve_provider(name)
        if provider not in known:
            listing = ", ".join(sorted(known)) or "none"
            return None, {
                "success": False,
                "error": f"unknown provider {name!r}; known providers: {listing}",
            }
        return provider, None

    def _provider_operator_refusal(self, command: str) -> dict | None:
        """Refuse a task-scoped worker token (D6): operators and supervisors only.

        The HTTP scope layer already refuses these commands for an ordinary
        session (they are not in ``AGENT_COMMAND_SET``); this repeats the rule
        for any caller that reaches the handler another way.
        """
        scope = self._current_scope or {}
        if scope.get("kind") == "session" and not scope.get("elevated"):
            return {
                "success": False,
                "error": f"out of scope: {command} requires an operator or supervisor",
            }
        return None

    def _provider_actor(self) -> str:
        """The principal an override is attributed to (``provider_availability``'s actor)."""
        from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal

        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            return "human:local-operator"
        if principal.kind is PrincipalKind.SESSION:
            return f"session:{principal.session_id or '-'}"
        if principal.kind is PrincipalKind.SERVICE:
            name = principal.service_name or "-"
            # ``discord:<id>`` is a human reaching us through a transport.
            return f"human:{name}" if ":" in name else f"service:{name}"
        return principal.describe()

    async def _provider_view(
        self, service, provider: str, *, verbose: bool, now: float
    ) -> dict[str, Any]:
        """One provider's status row: what ``aq provider status`` prints (D20)."""
        from src.providers.availability import UNAVAILABLE, usage_view
        from src.providers.availability_service import stale_after_seconds

        view = service.describe(provider, now)
        held = 0
        if view["state"] in UNAVAILABLE and service.enforcing:
            try:
                held = len((await service.affected(provider))["held"])
            except Exception:  # a count is decoration, never a failure
                logger.debug("provider status: held count failed", exc_info=True)
        view["held"] = held
        usage = None
        if provider != "llm":
            try:
                rows = await self.db.latest_provider_usage(provider)
                account, _scoped = usage_view(
                    rows, now=now, stale_after=stale_after_seconds(provider, self.config)
                )
            except Exception:  # no reading is "nothing known"
                logger.debug("provider status: usage read failed", exc_info=True)
                account = None
            if account is not None:
                usage = {
                    "window": account.window,
                    "scope": account.scope,
                    "used_percent": account.used_percent,
                    "resets_at": account.resets_at,
                    "observed_at": account.observed_at,
                }
        view["usage"] = usage
        if verbose:
            view["transitions"] = await self.db.list_provider_transitions(
                provider, limit=VERBOSE_TRANSITIONS
            )
        else:
            view.pop("evidence", None)
            view["transitions"] = []
        return view

    async def _cmd_provider_status(self, args: dict) -> dict:
        """Every tracked provider's availability, or one (``aq provider status``).

        Args:
            provider: Limit to one provider key (``codex``) or unambiguous
                vendor alias (``openai``).
            verbose: Add the evidence ring and the last ten transitions.

        Returns:
            ``providers`` rows carrying the effective state, reason, since,
            expected recovery (``until``), any operator override and its
            expiry, the held-task count, the last launch success and the
            newest account-wide usage reading, plus ``mode``.
        """
        service = self._availability()
        if service is None:
            return {"success": False, "error": "provider availability is not running"}
        verbose = bool(args.get("verbose"))
        if args.get("provider"):
            provider, error = await self._resolve_provider_arg(service, args["provider"])
            if error:
                return error
            providers = [provider]
        else:
            providers = sorted(await service.tracked_providers())
        now = service.now()
        rows = [
            await self._provider_view(service, provider, verbose=verbose, now=now)
            for provider in providers
        ]
        return {
            "success": True,
            "mode": getattr(service.config, "mode", "enforce"),
            "now": now,
            "providers": rows,
        }

    async def _cmd_provider_history(self, args: dict) -> dict:
        """One provider's effective-state transitions, newest first (``aq provider history``)."""
        service = self._availability()
        if service is None:
            return {"success": False, "error": "provider availability is not running"}
        provider, error = await self._resolve_provider_arg(service, args.get("provider"))
        if error:
            return error
        try:
            limit = max(1, min(int(args.get("limit") or 50), 1000))
        except (TypeError, ValueError):
            return {"success": False, "error": "limit must be an integer"}
        transitions = await self.db.list_provider_transitions(provider, limit=limit)
        return {"success": True, "provider": provider, "transitions": transitions}

    async def _cmd_provider_recheck(self, args: dict) -> dict:
        """Run the provider's login probe now and fold the answer in (``aq provider recheck``).

        The "I just ran ``codex login``" button (D4/D5): an ``authenticated``
        answer moves an ``unauthenticated`` provider to probation.
        """
        refusal = self._provider_operator_refusal("provider_recheck")
        if refusal:
            return refusal
        service = self._availability()
        if service is None:
            return {"success": False, "error": "provider availability is not running"}
        provider, error = await self._resolve_provider_arg(service, args.get("provider"))
        if error:
            return error
        result = await service.recheck(provider)
        now = service.now()
        return {
            "success": True,
            "provider": provider,
            "probe": result.get("probe"),
            "probe_detail": dict(result.get("probe_detail") or {}),
            "state": service.effective_state(provider, now),
            "transition": result.get("transition"),
            "status": await self._provider_view(service, provider, verbose=False, now=now),
        }

    async def _cmd_provider_set_state(self, args: dict) -> dict:
        """Set or clear an operator override on a provider (``aq provider set-state``, D6).

        ``disabled`` forces the unavailable half; ``available`` forces the
        launchable half against the evidence and always expires; ``auto``
        clears the override, resets the failure counters and re-derives.
        Expiry: ``for`` (a duration) or ``until`` (a timestamp), else
        ``override.default_ttl_seconds``; never past ``override.max_ttl_seconds``.
        ``no_expiry`` is accepted for ``disabled`` only.
        """
        refusal = self._provider_operator_refusal("provider_set_state")
        if refusal:
            return refusal
        service = self._availability()
        if service is None:
            return {"success": False, "error": "provider availability is not running"}
        provider, error = await self._resolve_provider_arg(service, args.get("provider"))
        if error:
            return error
        state = str(args.get("state") or "").strip().lower()
        if state not in SET_STATES:
            return {
                "success": False,
                "error": f"state must be one of {', '.join(SET_STATES)}",
            }
        reason = str(args.get("reason") or "").strip()
        duration = args.get("for")
        until_raw = args.get("until")
        no_expiry = bool(args.get("no_expiry"))
        now = service.now()
        until: float | None = None
        if state != "auto":
            if not reason:
                return {"success": False, "error": "reason is required for an override"}
            if sum(x is not None and x != "" for x in (duration, until_raw)) + no_expiry > 1:
                return {
                    "success": False,
                    "error": "give at most one of for, until and no_expiry",
                }
            override_cfg = service.config.override
            if no_expiry:
                if state != "disabled":
                    return {
                        "success": False,
                        "error": "an 'available' override always expires; no_expiry is "
                        "accepted for 'disabled' only",
                    }
            else:
                try:
                    if duration not in (None, ""):
                        until = now + parse_duration(duration)
                    elif until_raw not in (None, ""):
                        until = parse_timestamp(until_raw)
                    else:
                        until = now + float(override_cfg.default_ttl_seconds)
                except ValueError as exc:
                    return {"success": False, "error": str(exc)}
                if until <= now:
                    return {"success": False, "error": "the override would already have expired"}
                if until - now > float(override_cfg.max_ttl_seconds):
                    return {
                        "success": False,
                        "error": (
                            f"an override may last at most "
                            f"{float(override_cfg.max_ttl_seconds):g}s "
                            "(provider_failover.override.max_ttl_seconds)"
                        ),
                    }
        result = await service.set_state(
            provider, state, by=self._provider_actor(), reason=reason, until=until
        )
        now = service.now()
        return {
            "success": True,
            "provider": provider,
            "state": service.effective_state(provider, now),
            "transition": result.transition.payload() if result.transition else None,
            "status": await self._provider_view(service, provider, verbose=False, now=now),
        }

    async def _cmd_provider_availability_notify(self, args: dict) -> dict:
        """Tell the supervisor and the human that a provider changed half (D19).

        Idempotent per ``(provider, generation)``: the transition that caused
        it, an event replay and a periodic timer never message twice.  The
        daemon calls it on every half change; it is contracted so the
        ``provider-failover`` playbook can drive it too, and excluded from
        MCP, the CLI and the typed API.
        """
        service = self._availability()
        if service is None:
            return {"success": False, "error": "provider availability is not running"}
        provider, error = await self._resolve_provider_arg(service, args.get("provider"))
        if error:
            return error
        generation = args.get("generation")
        try:
            generation = None if generation is None else int(generation)
        except (TypeError, ValueError):
            return {"success": False, "error": "generation must be an integer"}
        return await service.notify_state_change(provider, generation)

    # -- re-routing (docs/specs/provider-failover.md D11-D16) -----------------

    def _reroute_service(self):
        return getattr(getattr(self, "orchestrator", None), "provider_reroute", None)

    @staticmethod
    def _task_id_list(raw: Any) -> list[str] | None:
        if raw is None or raw == "" or raw == []:
            return None
        if isinstance(raw, str):
            raw = [part for part in raw.replace(",", " ").split() if part]
        return [str(item).strip() for item in raw if str(item).strip()]

    async def _cmd_provider_reroute(self, args: dict) -> dict:
        """Plan and apply one re-route sweep (``aq provider reroute``, D11-D16).

        Moves eligible queued and provider-paused tasks off an unavailable
        provider onto the equivalent rung (same class) of an ``available``
        one, within the trickle and per-task limits, and records each move
        on the task.  ``dry_run`` plans only -- the same code path the
        dashboard preview uses.  Naming tasks with ``task_id`` and giving
        ``to_profile`` or ``force`` is an operator's explicit move: ``force``
        may move a pinned task, target a degraded provider or (with
        ``to_profile``) change the class, and is recorded ``operator_forced``.

        Args:
            provider: Limit the sweep to one provider key (vendor alias accepted).
            task_id: One task id, or a list, to move explicitly.
            to_profile: The target profile for the named tasks.
            include_paused: Also resume and move tasks paused before failover
                recorded a cause (the tasks of 2026-09-20).
            dry_run: Plan only; write nothing.
            force: Operator override (see above).

        Returns:
            ``outcome`` is ``rerouted``, ``held``, ``idle`` or ``disabled``,
            with ``moved`` / ``held`` / ``skipped`` decisions, ``held_by_kind``,
            ``resumed`` task ids and the ``batch_ids`` written.
        """
        refusal = self._provider_operator_refusal("provider_reroute")
        if refusal:
            return refusal
        service = self._reroute_service()
        availability = self._availability()
        if service is None or availability is None:
            return {"success": False, "error": "provider failover is not running"}
        task_ids = self._task_id_list(args.get("task_id") or args.get("task_ids"))
        force = bool(args.get("force"))
        to_profile = str(args.get("to_profile") or "").strip() or None
        dry_run = bool(args.get("dry_run"))
        if (force or to_profile) and not task_ids:
            return {
                "success": False,
                "error": "force and to_profile move named tasks only; pass task_id",
            }
        if to_profile is not None:
            profile = await self.db.get_profile(to_profile)
            if profile is None:
                return {"success": False, "error": f"profile '{to_profile}' not found"}
            if error := self._task_execution_profile_error(profile):
                return {"success": False, "error": error}
        provider = None
        if args.get("provider"):
            provider, error = await self._resolve_provider_arg(availability, args["provider"])
            if error:
                return error
        if task_ids:
            missing = [tid for tid in task_ids if await self.db.get_task(tid) is None]
            if missing:
                return {"success": False, "error": f"task(s) not found: {', '.join(missing)}"}
        return await service.sweep(
            provider=provider,
            task_ids=task_ids,
            to_profile=to_profile,
            include_paused=bool(args.get("include_paused")),
            dry_run=dry_run,
            force=force,
            actor=self._provider_actor(),
        )

    async def _cmd_provider_reroute_undo(self, args: dict) -> dict:
        """Undo re-routes (``aq provider reroute-undo``, D16).

        Restores ``profile_id`` to ``rerouted_from`` for tasks that are not
        running or claimed, writes an ``operator_undo`` row and clears the
        marker.  Refused per task while the original provider is still
        unavailable, unless ``force``.

        Args:
            batch_id: Undo every un-undone move of one batch.
            task_id: One task id, or a list.
            force: Undo even while the original provider is unavailable.
        """
        refusal = self._provider_operator_refusal("provider_reroute_undo")
        if refusal:
            return refusal
        service = self._reroute_service()
        if service is None:
            return {"success": False, "error": "provider failover is not running"}
        batch_id = str(args.get("batch_id") or args.get("batch") or "").strip() or None
        task_ids = self._task_id_list(args.get("task_id") or args.get("task_ids"))
        if not batch_id and not task_ids:
            return {"success": False, "error": "pass batch_id or task_id"}
        return await service.undo(
            batch_id=batch_id,
            task_ids=task_ids,
            force=bool(args.get("force")),
            actor=self._provider_actor(),
        )

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


__all__ = [
    "PROBEABLE",
    "SET_STATES",
    "ProviderCommandsMixin",
    "parse_duration",
    "parse_timestamp",
]
