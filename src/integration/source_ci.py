"""Exact source CI facts; task creation is delegated to CommandHandler."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.integration.models import HierarchicalIntegrationPolicy

logger = logging.getLogger(__name__)


#: GitHub's annotation on a job that no hosted runner ever picked up.  It
#: names the outage; a cancellation without it is still infrastructure, so it
#: is evidence, never the classification.
RUNNER_NOT_ACQUIRED = "The job was not acquired by Runner of type"


@dataclass(frozen=True)
class SourceCIObservation:
    task_id: str
    source: dict[str, Any]
    policy_generation: int
    state: str
    evidence: dict[str, Any]


def _cancelled_check_suite_ids(cancelled) -> list[int]:
    """The check suites GitHub can re-run, named by the cancelled checks.

    A check run names the suite it belongs to, and the suite is what the
    re-request endpoint addresses, so this is the exact target for re-running
    this head.  Ids are deduplicated because the cancelled checks of one
    workflow run usually share a single suite.
    """
    suites = set()
    for item in cancelled:
        suite_id = (item.get("check_suite") or {}).get("id")
        if isinstance(suite_id, bool) or not isinstance(suite_id, int) or suite_id <= 0:
            continue
        suites.add(suite_id)
    return sorted(suites)


def classify_source_checks(entries, *, head, required, conflicting=False):
    """Judge the latest trusted run of each required check, including cancellation.

    ``cancelled`` means every non-success required check was cancelled and
    nothing is pending: infrastructure, not a verdict on the code.  It is only
    reached when no required check failed, so one genuine failure is still red
    whatever else was cancelled, and a cancelled check is never success.

    Every verdict here is read from the per-check conclusions of the commit's
    own check runs, and never from a run-level conclusion: GitHub concludes a
    *workflow run* ``failure`` whenever jobs are cancelled because no hosted
    runner was acquired, even with zero failed jobs (the 2026-10-05 Actions
    outage).  A run-level conclusion carried in an entry is therefore ignored —
    jobs that only succeeded or were cancelled are infrastructure whatever the
    run says, and a single genuinely failed job is red whatever else the run
    says.

    A cancelled old run cannot supersede a newer pending/successful rerun.
    Missing, foreign-producer and mismatched-head checks never establish green.
    *conflicting* says GitHub reports the PR unmergeable; with no required
    check run on *head* the state is ``conflict``, because GitHub never runs
    ``pull_request`` CI for such a PR and it would otherwise stay pending.
    """
    latest = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        app = entry.get("app") or {}
        name = entry.get("name")
        if (
            name not in required.names
            or entry.get("head_sha") != head
            or str(app.get("id")) != required.producer_id
        ):
            continue
        identity = (
            str(entry.get("started_at") or entry.get("created_at") or ""),
            int(entry.get("id") or 0),
        )
        if name not in latest or identity > latest[name][0]:
            latest[name] = (identity, entry)
    checks = [item[1] for item in latest.values()]
    failed = [
        item
        for item in checks
        if item.get("status") == "completed"
        and item.get("conclusion") in {"failure", "timed_out", "action_required"}
    ]
    cancelled = [
        item
        for item in checks
        if item.get("status") == "completed" and item.get("conclusion") == "cancelled"
    ]
    pending = any(item.get("status") != "completed" for item in checks)
    if failed:
        state = "red"
    elif cancelled and not pending:
        state = "cancelled"
    elif conflicting and not latest:
        state = "conflict"
    elif len(latest) == len(required.names) and all(
        item.get("status") == "completed" and item.get("conclusion") == "success" for item in checks
    ):
        state = "green"
    else:
        state = "pending"
    evidence = {
        "head_sha": head,
        "producer_id": required.producer_id,
        "required_checks_version": required.version,
        "required_checks": list(required.names),
        # A cancellation is infrastructure, not a verdict on the code, and
        # this names why: an outage GitHub annotated, or an unknown cause.
        "runner_not_acquired": any(
            RUNNER_NOT_ACQUIRED in str((item.get("output") or {}).get("summary") or "")
            for item in cancelled
        ),
        "infra_only": state == "cancelled",
        # Exactly what a re-run of this head would address: the suites the
        # cancelled checks belong to.  Empty means there is nothing to re-run.
        "cancelled_check_suite_ids": _cancelled_check_suite_ids(cancelled),
        "checks": [
            {
                "id": item.get("id"),
                "name": item["name"],
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "url": item.get("html_url") or item.get("details_url"),
                "summary": str((item.get("output") or {}).get("summary") or "")[:4000],
            }
            for item in sorted(checks, key=lambda item: item["name"])
        ],
        "failing_checks": [item["name"] for item in failed + cancelled],
    }
    return state, evidence


def pull_request_conflicts(pull) -> bool:
    """Whether GitHub reports *pull* as unmergeable because of a conflict."""
    return (
        isinstance(pull, dict)
        and pull.get("mergeable") is False
        and pull.get("mergeable_state") == "dirty"
    )


async def observe_source_ci(*, row, source, client, handler, pull=None):
    policy_data = row.get("hierarchical_integration_policy")
    if not policy_data:
        return
    policy = HierarchicalIntegrationPolicy.model_validate(policy_data)
    if not policy.root.repair.source_ci:
        return
    state, evidence = classify_source_checks(
        await client.commit_check_runs(source["head"]),
        head=source["head"],
        required=policy.root.required_checks,
        conflicting=pull_request_conflicts(pull),
    )
    recorded = await handler(
        SourceCIObservation(
            task_id=row["id"],
            source=source,
            policy_generation=row["hierarchical_integration_generation"],
            state=state,
            evidence=evidence,
        )
    )
    if isinstance(recorded, dict):
        if recorded.get("blocker"):
            # Bounded by policy: an outage that outlasts the attempts is a
            # named blocker for a human, not another observation loop.
            logger.warning(
                "source CI infrastructure blocker %s for task %s at %s after %s "
                "consecutive observations",
                recorded["blocker"], row["id"], source["head"][:12],
                recorded.get("infra_observations"),
            )
        if recorded.get("rerun_requested"):
            await _request_check_reruns(client, row, source, evidence)


async def _request_check_reruns(client, row, source, evidence) -> None:
    """Re-run the cancelled check suites of this exact head; never raise.

    An outage is the expected failure here, not a defect: the suites cannot be
    re-run yet, the next observation asks again when its backoff is up, and a
    sustained outage ends at the named blocker instead of at a worker session.
    Nothing to target means the observation only waits.
    """
    suites = evidence.get("cancelled_check_suite_ids") or []
    if not suites:
        logger.warning(
            "source CI re-run due for task %s at %s with no cancelled check "
            "suite to address", row["id"], source["head"][:12],
        )
        return
    for suite_id in suites:
        try:
            await client.rerequest_check_suite(suite_id)
        except Exception:  # an outage is the expected failure, not a defect
            logger.warning(
                "source CI re-run request failed for check suite %s of task %s at %s",
                suite_id, row["id"], source["head"][:12], exc_info=True,
            )


def repair_description(observation):
    source, evidence = observation.source, observation.evidence
    checks = "\n".join(
        f"- {item['name']}: {item['conclusion']} {item.get('url') or ''}\n"
        f"  {item.get('summary') or ''}"
        for item in evidence["checks"]
        if item["name"] in evidence["failing_checks"]
    )
    return (
        f"Authorized source CI repair for task {observation.task_id}, PR {source['pr_url']}.\n"
        f"Repository {source['repository_id']}; source branch {source['branch']}; "
        f"exact source base {source['base']}, head {source['head']}, "
        f"checkpoint generation {source['generation']}.\n"
        f"Observed {observation.state} under policy generation {observation.policy_generation}.\n"
        f"Failing/cancelled checks:\n{checks}\n\n"
        "Work in your assigned root branch. Fetch and merge the exact source head "
        "above, preserving it as an ancestor; resolve all necessary code, test, "
        "migration and generated-artifact issues. Use the failed check links and "
        "record concrete failures and fixes. A red run is repairable only because at "
        "least one required check genuinely failed; any cancelled check listed beside "
        "it is infrastructure and is never fixed in code or counted as success. "
        "Re-chain colliding migration "
        "revisions and regenerate generated files. Run focused and relevant area checks "
        "with aq test, publish your assigned branch and close with exact test evidence. "
        "The delivery service observes this branch's own exact CI and queues further "
        "bounded repair if needed; only its fully checked candidate can reach main. "
        "Do not rewrite the source branch or publish main."
    )


class RootAdmissionReader:
    """Fresh exact-source Git/GitHub facts, without review-poller prerequisites."""

    def __init__(self, promotion):
        self.promotion = promotion

    async def observe_many(self, members, policy):
        """One open-PR listing per repository prunes stale branches before Git I/O."""
        repositories = {}
        observations = []
        # One fetch of all heads per repository per admission pass, not per member.
        fetched = set()
        for member in members:
            repository_id = member["repository_id"]
            if repository_id not in repositories:
                try:
                    resolved = await self.promotion._resolve_repository(repository_id)
                    binding = await self.promotion.git.bind_github_repository(resolved.origin_url)
                    client = self.promotion.git._github_client(binding)
                    pulls = await client.paged_list(
                        f"/repositories/{binding.repository_id}/pulls?state=open&per_page=100"
                    )
                    if any(pull.get("state") != "open" or not pull.get("html_url") for pull in pulls):
                        raise ValueError("open PR listing is malformed")
                    repositories[repository_id] = {
                        pull["html_url"]: pull for pull in pulls if pull.get("state") == "open"
                    }
                except Exception as exc:
                    repositories[repository_id] = exc
            pulls = repositories[repository_id]
            if isinstance(pulls, Exception):
                result = {"reason": "source_observation_unavailable", "error": str(pulls)}
            elif member["pr_url"] not in pulls:
                result = {"reason": "pr_closed"}
            else:
                result = await self(member, policy, pull=pulls[member["pr_url"]],
                                    fetched=fetched)
                fetched.add(repository_id)
            observations.append(result)
        return observations

    async def __call__(self, member, policy, *, pull=None, fetched=None):
        import time

        from src.git.manager import RemoteRefState

        try:
            resolved = await self.promotion._resolve_repository(member["repository_id"])
            git = self.promotion.git
            binding = await git.bind_github_repository(resolved.origin_url)
            client = git._github_client(binding)
            if pull is None:
                pull = await client.pull_request(member["pr_url"])
            if pull.get("state") != "open":
                return {"reason": "pr_closed"}
            head, base = pull.get("head") or {}, pull.get("base") or {}
            if (head.get("sha") != member["source_head"]
                    or head.get("ref") != member["source_branch"].removeprefix("refs/heads/")
                    or (head.get("repo") or {}).get("id") != binding.repository_id
                    or (base.get("repo") or {}).get("id") != binding.repository_id
                    or base.get("ref") != member["default_branch"].removeprefix("refs/heads/")):
                return {"reason": "pr_identity_changed"}
            # No CI before admission: batch candidate CI is the only gate.
            evidence = {}
            await self.promotion._ensure_retained_repository(resolved)
            store = str(resolved.retained_git_dir)
            async with git.arepository_transaction(store):
                if fetched is None or member["repository_id"] not in fetched:
                    await self.promotion._fetch_all_heads(resolved.retained_git_dir, resolved.origin_url)
                remote = await git.als_remote_ref(store, member["source_branch"])
                target = await git.als_remote_ref(store, member["default_branch"])
                if remote.state is not RemoteRefState.PRESENT or remote.oid != member["source_head"]:
                    return {"reason": "source_ref_changed"}
                if target.state is not RemoteRefState.PRESENT:
                    return {"reason": "target_unavailable"}
                contained = await git.ais_ancestor(
                    store, member["source_head"], target.oid, strict=True,
                )
                if contained is True:
                    return {"reason": "already_delivered"}
                if contained is not False:
                    return {"reason": "ancestry_unknown"}
                tree = await self.promotion._tree_oid(resolved.retained_git_dir, member["source_head"])
            return {"reason": None, "tree": tree, "checks": evidence,
                    "target_sha": target.oid, "observed_at": time.time()}
        except Exception as exc:
            return {"reason": "source_observation_unavailable", "error": str(exc)}
