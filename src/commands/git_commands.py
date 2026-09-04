"""Git commands mixin for CommandHandler.

Provides git-related commands that go through the CommandHandler dispatch
surface.  Currently contains:

- ``pr_merge`` — merge a PR via ``gh pr merge``.  Only callable by profiles
  that whitelist ``pr_merge`` in ``allowed_tools`` (final-reviewer only in
  the shipped dv2-phase2 configuration).  A merge is also where the daemon
  learns *which branch* the work actually landed on — see
  :meth:`GitCommandsMixin._record_pr_base`.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class GitCommandsMixin:
    """Mixin that adds git PR commands to CommandHandler."""

    async def _cmd_pr_merge(self, args: dict) -> dict:
        """Merge a PR.  Backs ``aq pr merge`` and the final-reviewer's tool.

        Only allowed for profiles that whitelist ``pr_merge`` in
        ``allowed_tools`` — the profile system enforces the toolset per
        agent, so worker profiles cannot invoke this even if they discover
        the command name.

        Before merging, the PR's status-check rollup is consulted according
        to ``integration.merge_ci_policy`` — see :meth:`_check_ci_before_merge`.
        That gate exists because GitHub's does not: ``main`` carries no
        required status check, so ``gh pr merge`` merged #341 with its own
        ``Tests (default)`` run red and landed the regression that run had
        caught.

        Args:
            project_id: ID of the project whose workspace will be used.
            pr_url: Full GitHub PR URL, e.g. ``https://github.com/o/r/pull/42``.
            method: Merge strategy — ``"squash"`` (default), ``"merge"``,
                or ``"rebase"``.
            force: Merge even when ``merge_ci_policy: required`` would
                refuse. This waives only the CI policy: immutable PR identity
                and reserved-path delivery checks always remain mandatory.

        Returns:
            ``{"success": bool, "pr_url": str, "sha": str | None, "error": str | None}``,
            plus a ``ci`` block whenever the policy is not ``off``.
        """
        project_id = args.get("project_id")
        pr_url = args.get("pr_url")
        method = str(args.get("method") or "squash")
        force = bool(args.get("force") or False)

        if not project_id:
            return {"success": False, "pr_url": "", "sha": None, "error": "project_id is required"}
        if not pr_url:
            return {"success": False, "pr_url": "", "sha": None, "error": "pr_url is required"}

        project = await self.db.get_project(project_id)
        if project is None:
            return {
                "success": False,
                "pr_url": pr_url,
                "sha": None,
                "error": f"unknown project: {project_id}",
            }

        # ``gh pr merge`` used to run in ``get_project_workspace_path()`` —
        # the project's first workspace row, clones before links, which under
        # worktree mode is the *base* checkout and is routinely the operator's
        # own working tree.  Merging does not need it: given a full PR URL gh
        # resolves owner/repo/number from the URL and talks to the API, so any
        # directory works (verified against gh 2.45).  Run in the daemon's
        # data dir instead, which is not a checkout at all.  The one behaviour
        # this drops is ``--delete-branch``'s *local* half; the remote branch
        # is still deleted, and the local branch it used to remove lived in a
        # tree no agent should have been touching.  See
        # :mod:`src.orchestrator.base_workspace`.
        cwd = self.config.data_dir or os.getcwd()
        os.makedirs(cwd, exist_ok=True)

        # Validate the concrete base/head pair and the merge-base PR diff
        # before CI is consulted. ``force`` is deliberately unavailable here:
        # it can waive a policy opinion about CI, never delivery identity or
        # daemon-owned paths.
        try:
            identity = await self.orchestrator.git.avalidate_pr_for_merge(cwd, pr_url)
        except Exception as exc:
            return {
                "success": False,
                "pr_url": pr_url,
                "sha": None,
                "error": f"Could not validate immutable PR delivery: {exc}",
            }

        ci = await self._check_ci_before_merge(cwd, pr_url, force=force)
        if ci is not None and ci.get("blocked"):
            return {
                "success": False,
                "pr_url": pr_url,
                "sha": None,
                "error": ci["message"],
                "ci": ci,
            }

        result = await self.orchestrator.git.amerge_pr(
            cwd,
            pr_url,
            method=method,
            expected_head_oid=identity.head_oid,
            expected_base_ref=identity.base_ref,
        )
        response = {
            "success": result["success"],
            "pr_url": pr_url,
            "sha": result.get("sha"),
        }
        if ci is not None:
            response["ci"] = ci
        if error := result.get("error"):
            response["error"] = error
        if result["success"]:
            # Best-effort: the merge has already happened, so a failure to
            # annotate it must never be reported as a failed merge.
            try:
                response.update(await self._record_pr_base(project, pr_url, cwd))
            except Exception:
                logger.warning("Could not record the base branch for %s", pr_url, exc_info=True)
        return response

    async def _check_ci_before_merge(self, cwd: str, pr_url: str, *, force: bool) -> dict | None:
        """Apply ``integration.merge_ci_policy`` to a PR about to be merged.

        Returns ``None`` when the policy is ``off`` (no ``gh`` call is made
        and no ``ci`` block appears in the result), otherwise a dict:

        ``{"policy", "state", "summary", "failing", "pending", "missing",
        "blocked": bool, "forced": bool, "message": str}``

        ``blocked`` is the only field ``_cmd_pr_merge`` acts on.  Under
        ``warn`` it is always ``False`` — the point of ``warn`` is that the
        verdict becomes *visible* (in the command result the final-reviewer
        reads, and in the daemon log) without changing what merges, which
        is what makes it safe to ship on by default while ``main`` itself
        is red.  Under ``required`` anything but green blocks, including
        :data:`~src.git.ci_gate.UNKNOWN`: "the rollup could not be read" is
        precisely the state in which merging blind is how #341 happened.
        """
        from src.git import ci_gate

        policy = getattr(self.config.integration, "merge_ci_policy", ci_gate.MERGE_CI_POLICY_WARN)
        if policy == ci_gate.MERGE_CI_POLICY_OFF:
            return None

        required = list(getattr(self.config.integration, "merge_required_checks", []) or [])
        try:
            entries = await self.orchestrator.git.apr_check_rollup(cwd, pr_url)
        except Exception:
            # Probing CI must never be the thing that breaks merging. Any
            # failure here is UNKNOWN, which ``warn`` logs and ``required``
            # refuses on — never a crash, and never a silent green.
            logger.warning("Could not read the CI rollup for %s", pr_url, exc_info=True)
            entries = None
        verdict = ci_gate.classify_rollup(entries, required)

        ci: dict = {
            "policy": policy,
            "state": verdict.state,
            "summary": verdict.summary(),
            "failing": list(verdict.failing),
            "pending": list(verdict.pending),
            "missing": list(verdict.missing),
            "blocked": False,
            "forced": False,
            "message": "",
        }

        # The second question: did those checks run against the base as it
        # is *now*?  A green head that is behind its base is the #390/#391
        # shape — each PR green alone, the combination untested.  This is
        # GitHub's "require branches to be up to date" applied here, where
        # it can be turned on before the ruleset flag (which would refuse
        # every fleet merge until pr_merge could recover from it).
        freshness = None
        if getattr(self.config.integration, "merge_require_up_to_date", True):
            try:
                comparison = await self.orchestrator.git.apr_behind_base(cwd, pr_url)
            except Exception:
                logger.warning("Could not compare %s against its base", pr_url, exc_info=True)
                comparison = None
            freshness = ci_gate.classify_base(comparison)
            ci["base"] = freshness.as_dict()

        problems: list[str] = []
        if not verdict.is_green:
            problems.append(f"CI is not green ({verdict.state}): {verdict.summary()}")
        if freshness is not None and not freshness.is_current:
            problems.append(f"head is {freshness.summary()} ({freshness.state})")
        if not problems:
            return ci

        detail = f"{pr_url}: " + "; ".join(problems)
        if policy != ci_gate.MERGE_CI_POLICY_REQUIRED:
            ci["message"] = f"{detail} — merged anyway (integration.merge_ci_policy: {policy})"
            logger.warning("%s", ci["message"])
            return ci
        if force:
            ci["forced"] = True
            ci["message"] = f"{detail} — merged anyway (force=true)"
            logger.warning("%s", ci["message"])
            return ci
        ci["blocked"] = True
        remedies = ["fix the failing checks", "wait for the run to finish"]
        if freshness is not None and not freshness.is_current:
            parsed = ci_gate.parse_pr_url(pr_url)
            if parsed is not None:
                owner, repo, number = parsed
                remedies.append(
                    "update the branch so its checks re-run against the current base "
                    f"(gh api -X PUT repos/{owner}/{repo}/pulls/{number}/update-branch)"
                )
            else:
                remedies.append("update the branch (PUT .../pulls/<n>/update-branch)")
        remedies.append("pass force=true")
        ci["message"] = (
            f"{detail}. Refusing to merge under integration.merge_ci_policy: required. "
            + ", ".join(remedies[:-1])
            + f", or {remedies[-1]}."
        )
        logger.warning("%s", ci["message"])
        return ci

    async def _record_pr_base(self, project, pr_url: str, cwd: str) -> dict:
        """Record which branch a merged PR actually landed on.

        "Merged" is not the same as "on the default branch".  Pkg 4's workers
        stacked PRs #284/#288/#289 onto ``feature/playbook-v2-pkg4-core``; the
        sweep merged all three, every task closed COMPLETED, and nothing ever
        merged that feature branch into ``main`` — so ``main`` never grew
        ``src/playbooks/executors/agent_task.py`` while every dependent task
        believed its prerequisite had shipped.

        The base branch therefore goes on the task as ``pr_base``, and a merge
        to anything other than the project default branch is labelled as such
        (``pr_merged_to_default``).  ``_sweep_resolve_pr_ci_gates`` reads the
        same question from the PR itself before it lets a ``pr-merged`` gate
        release dependents.  ``gh`` being unavailable is not an error — the
        merge already happened; the annotation is best-effort.
        """
        base = await self.orchestrator.git.apr_base_ref(cwd, pr_url)
        if not base:
            return {}
        default_branch = await self.orchestrator._get_default_branch(project)
        on_default = base == default_branch
        extra: dict = {"base": base, "merged_to_default": on_default}
        if not on_default:
            extra["note"] = f"merged to {base} (not {default_branch})"
        try:
            tasks = await self.db.list_tasks(project_id=project.id)
        except Exception:
            return extra
        for task in tasks:
            if task.pr_url and task.pr_url == pr_url:
                await self.db.set_task_meta(task.id, "pr_base", base)
                await self.db.set_task_meta(task.id, "pr_merged_to_default", on_default)
                extra["task_id"] = task.id
                break
        return extra
