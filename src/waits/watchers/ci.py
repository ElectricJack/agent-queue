"""Credentialled, exact-head observations for verified CI and PR waits.

The composition root supplies the existing ExactChecks resolver and GitManager.
Registration reads are fresh and use the caller's connection without writing or
opening a transaction. Ticks recover subscriptions from PostgreSQL and refresh
each head once through ``ExactChecks.refresh``, whose cache upsert is the one
writer of ``ci_check:<repo>:<sha>:<name>`` result events. Run, PR and watcher
unavailability outcomes go through the injected ``commit``, the composition root's
service-principal ``wait_source_commit`` command. Nothing starts at import.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from src.database.tables import integration_check_evidence, repos, wait_records
from src.git.manager import GitError
from src.integration.checks import Conclusion, evaluate
from src.integration.runtime_contracts import HeadIdentity
from src.waits.adapters.ci import CIAdapter
from src.waits.adapters.pull_request import PullRequestAdapter
from src.waits.conditions import Condition, Observation, Snapshot, WaitError
from src.waits.sources import payload_hash

logger = logging.getLogger(__name__)

KINDS = ("ci_check", "ci_run", "pull_request")
# Final conclusions plus cancellation: each is a result the waiter must read.
TERMINAL = frozenset({Conclusion.SUCCESS, Conclusion.FAILURE, Conclusion.MISSING,
                      Conclusion.CANCELLED})


def wait_source_handlers():
    """The watcher's callbacks for ``orchestrator.wait_v2_handlers``; never activates v2."""
    return {"wait_source_commit": commit_source_event}


async def commit_source_event(args, *, principal, scope, handler):
    """Concrete service callback for ``orchestrator.wait_v2_handlers``.

    The closed command contract validated ``args`` before dispatch. Repeat the
    service fence here so direct callback invocation cannot grant an agent write
    authority. The source writer owns the transaction and idempotent revision insert.
    """
    # PrincipalKind is a StrEnum; the waits package never imports src.commands.
    if getattr(principal, "kind", None) != "service":
        return {"success": False, "error_code": "wait.out_of_scope",
                "error": "source commits require a service principal"}
    seq = await handler.db.wait_source_commit(**args)
    return {"success": True, "seq": seq, "replayed": seq is None}


class CheckWatcher:
    def __init__(self, db, commit, git, *, checks, repository, clock=time.time,
                 required_check_sets=None, page_size=200, pr_poll_seconds=60):
        """Resolvers are async ``checks(repo, condition)`` and ``repository(repo)``.

        ``commit(event)`` runs the ``wait_source_commit`` command as a service
        principal and returns its result dictionary.

        checks returns the policy-bound ExactChecks instance for that source;
        repository returns its authenticated GitHub repository binding. Named
        required-check sets map (repository_id, set_name) to allowed check names.
        No client may nominate a producer or an unregistered check set.
        """
        if page_size < 1 or pr_poll_seconds <= 0:
            raise ValueError("watcher page size and cadence must be positive")
        self.db, self.commit, self.git = db, commit, git
        self.checks, self.repository, self.clock = checks, repository, clock
        self.required_check_sets = dict(required_check_sets or {})
        self.page_size, self.pr_poll_seconds = page_size, pr_poll_seconds
        self._due = {}
        self._tick_lock = asyncio.Lock()

    def registry_readers(self):
        """``build_registry`` keywords: one fresh read serves the CI and PR adapters."""
        return {"ci_reader": self.read, "pr_reader": self.read}

    async def lock_source(self, conn, cond):
        """Keep repository binding/cache stable across the fresh remote verdict.

        Remote state cannot be transaction locked: read() re-verifies the
        exact head and check with its authenticated provider on every dispatch.
        Events and cached results never substitute for that observation.
        """
        await conn.execute(select(repos.c.id).where(
            repos.c.id == cond.parameters["repository_id"],
            repos.c.project_id == cond.context["project_id"],
        ).with_for_update(read=True))
        table = integration_check_evidence
        await conn.execute(select(table.c.id).where(
            table.c.repository_id == cond.parameters["repository_id"],
            table.c.sha == cond.parameters["head_sha"],
        ).order_by(table.c.id).with_for_update(read=True))

    @staticmethod
    def head(cond):
        sha = cond.parameters["head_sha"]
        return HeadIdentity(repository_id=cond.parameters["repository_id"], sha=sha, ref=sha,
                            generation=0)

    @staticmethod
    def adapter(cond):
        return PullRequestAdapter() if cond.kind == "pull_request" else CIAdapter(cond.kind)

    async def _prepare(self, conn, cond):
        repo = (await conn.execute(select(repos).where(
            repos.c.id == cond.parameters["repository_id"],
            repos.c.project_id == cond.context["project_id"],
        ))).mappings().first()
        if repo is None:
            return None
        repo = dict(repo)
        binding = await self.repository(repo)
        head = cond.parameters.get("head_sha", cond.parameters.get("expected_head_sha"))
        origin = await self.git.acommit_head_sha(binding.full_name, head, repository=binding)
        if origin != head:
            return repo, binding, None, "head_unavailable"
        if cond.kind == "pull_request":
            return repo, binding, None, None
        exact = await self.checks(repo, cond)
        if exact is None:
            return repo, binding, None, "checks_unavailable"
        set_name = cond.parameters.get("required_check_set")
        if set_name is not None:
            names = self.required_check_sets.get((repo["id"], set_name))
            if not names or not set(names) <= set(exact.required.names):
                raise WaitError("wait.source_unresolved", "required-check set is not registered")
            if cond.kind == "ci_check" and cond.parameters["check_name"] not in names:
                raise WaitError("wait.source_unresolved", "check is outside the registered set")
        return repo, binding, exact, None

    def _snapshot(self, cond, observation_state, revision, *, result_ref=None, **facts):
        params = cond.parameters
        identity = {k: params[k] for k in ("check_name", "run_id", "pr_number",
                                         "required_check_set") if k in params}
        return Snapshot(cond.context["project_id"], 0,
            Observation(revision, observation_state, result_ref, {
                "repository_id": params["repository_id"],
                "head_sha": params.get("head_sha", params.get("expected_head_sha")),
                "exists_on_origin": True, "identity_verified": True,
                **identity, **facts,
            }), f"{'pr' if cond.kind == 'pull_request' else 'ci'}:{params['repository_id']}")

    async def _ci(self, cond, prepared, *, result=None):
        """Read one exact check or run from a verdict on this exact head.

        Registration passes no verdict and observes the provider without writing;
        a tick passes the verdict its ExactChecks refresh recorded.
        """
        repo, _binding, exact, reason = prepared
        if reason:
            return self._snapshot(cond, "unavailable", reason, reason=reason,
                                  exists_on_origin=reason != "head_unavailable")
        head = self.head(cond)
        if result is None:
            observed = await exact.provider.observe(head, now=self.clock())
            result = evaluate(exact.required, head, observed, now=self.clock())
        # evaluate dropped foreign SHAs, repositories, producers and check versions.
        identity = "check_name" if cond.kind == "ci_check" else "run_id"
        wanted = cond.parameters[identity]
        rows = [c for c in result.checks
                if (c.name if identity == "check_name" else c.run_id) == wanted]
        set_name = cond.parameters.get("required_check_set")
        if set_name is not None:
            names = self.required_check_sets[(repo["id"], set_name)]
            rows = [c for c in rows if c.name in names]
        # A required name the producer never reported is not proof of a pending
        # check. A reported pending check carries no run until its run starts.
        if not rows or any(c.reason == "not_observed" for c in rows):
            return self._snapshot(cond, "unavailable", "check_unverified",
                                  identity_verified=False, reason="check_unverified")
        detail = [{"check_name": c.name, "run_id": c.run_id, "attempt": c.attempt,
                   "producer_id": c.producer_id, "version": c.required_check_version,
                   "conclusion": c.conclusion.value, "url": c.run_url}
                  for c in sorted(rows, key=lambda c: c.name)]
        revision = payload_hash(detail)
        if any(c.conclusion is Conclusion.UNAVAILABLE for c in rows):
            state = "unavailable"
        elif all(c.conclusion in TERMINAL for c in rows):
            state = "satisfied"
        else:
            state = "unsatisfied"
        return self._snapshot(cond, state, revision,
            result_ref=next((c.run_url for c in rows if c.run_url),
                            f"check:{repo['id']}:{head.sha}:{wanted}"), checks=detail)

    async def _pr(self, cond, prepared):
        repo, binding, _, reason = prepared
        if reason:
            return self._snapshot(cond, "unavailable", reason, reason=reason,
                                  exists_on_origin=False)
        p = cond.parameters
        url = f"https://github.com/{binding.full_name}/pull/{p['pr_number']}"
        first = await self.git.aget_pr_identity(repo["checkout_base_path"], url,
                                              repository=binding)
        merged = await self.git.acheck_pr_merged(repo["checkout_base_path"], url,
                                               repository=binding)
        last = await self.git.aget_pr_identity(repo["checkout_base_path"], url,
                                             repository=binding)
        if (first.head_oid != p["expected_head_sha"] or last.head_oid != first.head_oid
                or first.number != p["pr_number"] or last.number != first.number
                or first.repository != binding.full_name or last.repository != first.repository):
            return self._snapshot(cond, "unavailable", "source_changed", reason="source_changed")
        state = "unavailable" if merged is None else "satisfied" if merged else "unsatisfied"
        return self._snapshot(cond, state, f"{first.head_oid}:{merged}", result_ref=url,
                              merged=merged is True, state="closed" if merged is None else "open")

    async def read(self, conn, cond):
        """Fresh registration/post-subscribe read; never trusts a previous conclusion."""
        try:
            prepared = await self._prepare(conn, cond)
            if prepared is None:
                return None
            if cond.kind == "pull_request":
                return await self._pr(cond, prepared)
            return await self._ci(cond, prepared)
        except (GitError, TimeoutError, ConnectionError):
            return self._snapshot(cond, "unavailable", "provider_unavailable",
                                  reason="provider_unavailable")

    async def _publish(self, cond, snapshot):
        observation = snapshot.observation
        identity = self.adapter(cond).source_key(cond).identity
        event = {"source_key": identity, "generation": 0, "revision": observation.revision,
                 "outcome_ref": observation.result_ref or identity,
                 "payload": {"state": observation.state, **observation.facts},
                 "occurred_at": self.clock()}
        result = await self.commit(event)
        if not result.get("success"):
            raise WaitError("wait.unavailable", "check source commit was refused", result=result)

    async def tick(self):
        """Recover pending subscriptions, refresh due heads once, and commit outcomes.

        Publication failures leave the head due. This also handles restarts without
        a local head registry and conclusions arriving before registration.
        """
        async with self._tick_lock:
            cursor, seen, active, published = "", {}, set(), 0
            while True:
                async with self.db._engine.connect() as conn:
                    rows = (await conn.execute(select(wait_records).where(
                        wait_records.c.id > cursor, wait_records.c.outcome == "pending",
                        wait_records.c.condition_kind.in_(KINDS),
                        wait_records.c.verification_state == "verified",
                    ).order_by(wait_records.c.id).limit(self.page_size))).mappings().all()
                    for row in rows:
                        cond = Condition.from_record(row["condition"])
                        source = self.adapter(cond).source_key(cond)
                        if source.identity != row["source_key"] or row["source_generation"] != 0:
                            continue
                        key = (cond.kind, source.identity, cond.parameters.get("required_check_set"))
                        active.add(key)
                        if self._due.get(key, 0) > self.clock():
                            continue
                        try:
                            prepared = await self._prepare(conn, cond)
                            if prepared is None:
                                snapshot = self._snapshot(cond, "unavailable", "missing", reason="missing")
                                delay = 300
                            elif cond.kind == "pull_request":
                                snapshot, delay = await self._pr(cond, prepared), self.pr_poll_seconds
                            else:
                                exact = prepared[2]
                                result = None
                                if exact is not None:
                                    head_key = (cond.parameters["repository_id"],
                                                cond.parameters["head_sha"], id(exact))
                                    if head_key not in seen:
                                        # Records changed exact-SHA rows as ci_check events.
                                        seen[head_key] = await exact.refresh(self.head(cond))
                                    result = seen[head_key]
                                snapshot = await self._ci(cond, prepared, result=result)
                                delay = (exact.retry_seconds if snapshot.observation.state == "unavailable"
                                         else exact.poll_seconds) if exact is not None else 300
                        except (GitError, TimeoutError, ConnectionError, WaitError):
                            snapshot = self._snapshot(cond, "unavailable", "provider_unavailable",
                                                      reason="provider_unavailable")
                            delay = 300
                        state, facts = snapshot.observation.state, snapshot.observation.facts
                        # The refresh already recorded every reported check row.
                        recorded = cond.kind == "ci_check" and "reason" not in facts
                        if state != "unsatisfied" and not recorded:
                            try:
                                await self._publish(cond, snapshot)
                            except WaitError:
                                logger.warning("wait source commit refused for %s",
                                               source.identity)
                                continue  # stays due
                            published += 1
                        self._due[key] = self.clock() + delay
                if len(rows) < self.page_size:
                    break
                cursor = rows[-1]["id"]
            self._due = {key: due for key, due in self._due.items() if key in active}
            return published
