"""Promotion configuration reads and PR-backed intent commands."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
from datetime import UTC, datetime

from sqlalchemy import func, insert, select, update

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.commands.supervisor_authority import integration_operator
from src.database.tables import (
    integration_batch_members,
    integration_batches,
    integration_check_evidence,
    integration_review_evidence,
    projects,
    task_context,
    task_metadata,
    tasks,
)
from src.git.github_contracts import GitHubAccessError, rate_limit_cause
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.promotion_steps import (
    PROMOTION_CONTEXT,
    PROMOTION_RESULT,
    FlowSchema,
    promotion_ref,
)


class PromoteCommandsMixin:
    async def _cmd_promote_schema(self, args: dict) -> dict:
        """Print the promotion-flow JSON schema."""
        return {"success": True, "outcome": "schema", "schema": FlowSchema.schema()}

    async def _promotion_manifest(self, repository) -> dict:
        """Read trusted configuration at the designated default branch's exact SHA.

        A caller-supplied flow never supplies its own trust anchors. Missing
        trust fails the layer-3 membership checks rather than trusting defaults.
        """
        from src.integration.preflight import read_committed_trust_manifest

        resolver = getattr(self.orchestrator, "github_repository_binding_resolver", None)
        factory = getattr(self.orchestrator, "github_client_factory", None)
        if resolver is None or factory is None:
            return {}
        binding = resolver(repository)
        if inspect.isawaitable(binding):
            binding = await binding
        if binding is None:
            return {}
        client = factory(binding)
        if inspect.isawaitable(client):
            client = await client
        _sha, raw = await read_committed_trust_manifest(client, binding, repository.default_branch)
        document = json.loads(raw) if raw is not None else {}
        return document if isinstance(document, dict) else {}

    async def _cmd_promote_validate(self, args: dict) -> dict:
        """Validate a supplied or stored flow without changing project configuration."""
        from src.commands.contracts.integration import PromoteValidateArgs

        request = PromoteValidateArgs.model_validate(args)
        project_id = request.project_id
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.SESSION and principal.project_id is None:
            _label, refusal = await integration_operator(self.db, project_id)
            authorized = refusal is None
        elif principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            authorized = principal.project_id == project_id and not principal.unresolved
        else:
            authorized = principal.kind in {PrincipalKind.LOCAL, PrincipalKind.SERVICE}
        if not authorized:
            return {
                "success": False,
                "outcome": "unauthorized",
                "error": "Project is outside caller scope.",
            }
        project = await self.db.get_project(project_id)
        if project is None:
            return {
                "success": False,
                "outcome": "not_found",
                "error": f"Project {project_id!r} does not exist.",
            }
        document = request.flow
        if request.use_stored:
            async with self.db._engine.connect() as conn:
                document = (
                    await conn.execute(
                        select(projects.c.promotion_flow).where(projects.c.id == project_id)
                    )
                ).scalar_one()
        repository_id = project.integration_repository_id
        repository = await self.db.get_repo(repository_id) if repository_id else None
        if repository is not None and repository.project_id != project_id:
            repository = None
        default_branch = repository.default_branch if repository else project.repo_default_branch
        if not default_branch:
            return {
                "success": False,
                "outcome": "not_found",
                "error": "Repository default branch is missing.",
            }
        result = FlowSchema.validate(document, default_branch=default_branch)
        # Structural/chain errors win over trust read failures. Empty flows
        # have no trust requirements and can validate before configuration.
        warnings = []
        if result.layer == 3 and result.flow:
            try:
                manifest = await self._promotion_manifest(repository) if repository else {}
            except Exception as exc:  # noqa: BLE001 - provider boundary; validation fails closed
                manifest = {}
                warnings.append(
                    {
                        "code": "trust_manifest_unavailable",
                        "pointer": "",
                        "message": str(exc),
                        "layer": 3,
                    }
                )
            result = FlowSchema.validate(document, default_branch=default_branch, manifest=manifest)
        remote = {}
        if request.remote and result.valid:
            from src.integration.promotion_steps import validate_promotion_remote

            try:
                binding, client, app_id = await self._promotion_client(repository)
                remote = await validate_promotion_remote(
                    client, binding, result.flow, default_branch=default_branch, app_id=app_id,
                )
                warnings.extend(remote.pop("warnings"))
            except Exception as exc:  # noqa: BLE001 - layer four is diagnostic, never a refusal
                warnings.append({
                    "code": "remote_unverifiable", "pointer": "", "layer": 4,
                    "message": f"Remote validation is unavailable: {type(exc).__name__}.",
                })
        return {
            "success": result.valid,
            "outcome": "valid" if result.valid else "invalid",
            "project_id": project_id,
            **result.as_dict(),
            "warnings": warnings,
            **remote,
        }

    # E1 read-only configuration commands. Keep separate from intent/PR
    # commands so those can register independently in the shared group.
    async def _promotion_client(self, repository):
        from src.git.github_contracts import credential_identity_from_client

        resolver = getattr(self.orchestrator, "github_repository_binding_resolver", None)
        factory = getattr(self.orchestrator, "github_client_factory", None)
        if repository is None or resolver is None or factory is None:
            raise ValueError("Repository client is unavailable.")
        binding = resolver(repository)
        if inspect.isawaitable(binding):
            binding = await binding
        if binding is None:
            raise ValueError("Repository binding is unavailable.")
        client = factory(binding)
        if inspect.isawaitable(client):
            client = await client
        if client is None or client.repository != binding:
            raise ValueError("Repository client does not match its binding.")
        identity = credential_identity_from_client(client)
        if identity.app_id is None:
            raise ValueError("Rulesets require the daemon's App identity.")
        return binding, client, identity.app_id

    async def _cmd_promote_rulesets(self, args: dict) -> dict:
        """Print admin-owned rulesets and copyable workflow triggers; never write GitHub."""
        from src.integration.promotion_steps import promotion_rulesets, promotion_workflow_triggers

        result = await self._cmd_promote_validate({**args, "remote": False})
        if not result.get("valid"):
            return result
        project = await self.db.get_project(args["project_id"])
        repository = await self.db.get_repo(project.integration_repository_id) \
            if project.integration_repository_id else None
        if repository is not None and repository.project_id != args["project_id"]:
            repository = None
        try:
            _binding, _client, app_id = await self._promotion_client(repository)
        except Exception as exc:  # noqa: BLE001 - configuration boundary
            return {"success": False, "outcome": "not_found", "error": str(exc)}
        default_branch = repository.default_branch
        return {
            **result, "outcome": "rulesets", "app_id": app_id,
            "rulesets": promotion_rulesets(result["flow"], default_branch=default_branch,
                                          app_id=app_id),
            "workflow_triggers": promotion_workflow_triggers(result["flow"],
                                                            default_branch=default_branch),
        }

    async def _promotion_inputs(self, project_id, *, read=False, command=None):
        """Authorize before resolving a project's promotion configuration."""
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind in {PrincipalKind.SESSION, PrincipalKind.PLAYBOOK}:
            authorized = (
                not principal.unresolved
                and principal.project_id == project_id
                and (read or principal.policy.allows_aq_command(command))
            )
            if principal.project_id is None:
                _, refusal = await integration_operator(self.db, project_id)
                authorized = refusal is None
        elif principal.kind is PrincipalKind.SERVICE:
            authorized = True
        else:
            _, refusal = await integration_operator(self.db, project_id)
            authorized = refusal is None
        if not authorized:
            raise PromotionRefusal(
                "unauthorized", "Caller cannot access this project's promotions."
            )
        project = await self.db.get_project(project_id)
        if project is None:
            raise PromotionRefusal("not_found", "Project does not exist.")
        async with self.db._engine.connect() as conn:
            raw = await conn.scalar(
                select(projects.c.promotion_flow).where(
                    projects.c.id == project_id,
                )
            )
        if not raw:
            raise PromotionRefusal("promotion_flow_empty", "Project has no promotion flow.")
        repository = await self.db.get_repo(project.integration_repository_id or "")
        if repository is None or repository.project_id != project_id:
            raise PromotionRefusal("unavailable", "Project has no bound integration repository.")
        result = FlowSchema.validate(raw, default_branch=repository.default_branch)
        if result.layer < 3 or not result.flow:
            raise PromotionRefusal("promotion_flow_invalid", "Stored promotion flow is invalid.")
        return project, repository, raw, result.flow

    async def _promotion_runtime(self, project, repository, step):
        from src.integration.train import TrainTarget

        train = getattr(self.orchestrator, "integration_train", None)
        if train is None:
            raise PromotionRefusal("unavailable", "The integration train is not active.")
        lane = await train.lane_for(
            TrainTarget(
                project.id,
                repository.id,
                "refs/heads/" + step["target"],
                "promotion",
                step=step,
            )
        )
        ops = lane.service.gitops
        repo = await ops.repository(
            Batch("promotion-request", project.id, repository.id, "refs/heads/" + step["target"])
        )
        await ops.validate_repository(repo)
        client = self.orchestrator.github_client_factory(repo.binding)
        if inspect.isawaitable(client):
            client = await client
        return lane, ops, repo, client

    def _promotion_user_client(self, binding):
        """Use the existing human gh login, isolated from all App token variables."""
        from src.git.github import GitHubClient
        from src.git.github_cli import ExistingLoginCredentials, GhRunner

        factory = getattr(self.orchestrator, "promotion_user_client_factory", None)
        return (
            factory(binding)
            if factory
            else GitHubClient(
                binding,
                runner=GhRunner(ExistingLoginCredentials()),
            )
        )

    async def _promotion_requester(self, binding, approval):
        principal = current_principal() or TRUSTED_LOCAL
        identity = _requester_identity(principal)
        login = None
        if principal.kind is PrincipalKind.LOCAL:
            user_client = self._promotion_user_client(binding)
            login = await _human_login(user_client)
        elif approval == "requester":
            resolver = getattr(self.orchestrator, "promotion_requester_login", None)
            if resolver:
                login = await resolver(principal, binding)
            if not login:
                raise PromotionRefusal(
                    "promotion_requester_identity_missing",
                    "Requester approval requires a verified GitHub user binding.",
                )
        return {"identity": identity, "github_login": login}

    async def _cmd_promote_request(self, args):
        from src.commands.contracts.promote import PromoteRequestArgs
        from src.integration.ci import IntegrationTrustManifest
        from src.integration.promotion_steps import StepAdmission, step_required_checks

        request = PromoteRequestArgs.model_validate(args)
        try:
            project, repository, raw, flow = await self._promotion_inputs(
                request.project_id, command="promote_request"
            )
            step = _find_step(flow, request.step_id)
            manifest = await self._promotion_manifest(repository)
            validation = FlowSchema.validate(
                raw, default_branch=repository.default_branch, manifest=manifest
            )
            if not validation.valid:
                raise PromotionRefusal(
                    "promotion_flow_invalid", "Flow does not match committed trust."
                )
            trust = IntegrationTrustManifest.model_validate(manifest)
            if trust.canonical_repository_id != repository.id:
                raise PromotionRefusal("promotion_flow_invalid", "Trust names another repository.")
            required = step_required_checks(trust, step)
            _lane, ops, repo, client = await self._promotion_runtime(project, repository, step)
            if (trust.repository_id, trust.full_name) != (
                repo.binding.repository_id,
                repo.binding.full_name,
            ):
                raise PromotionRefusal(
                    "promotion_flow_invalid", "Trust names another GitHub repository."
                )
            requester = await self._promotion_requester(repo.binding, step["gate"]["approval"])
            target_ref = "refs/heads/" + step["target"]
            async with self.db.immediate() as conn:
                # Serialize idempotency and competing versions for this target.
                # Only promotion requests take this lock; the project row stays
                # unlocked until the provider calls are done.
                await conn.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtext(
                                f"aq-promote-admit:{repository.id}:{step['target']}",
                            )
                        )
                    )
                )
                await _check_flow(conn, project.id, raw, repository.id)
                source_tip = await ops.remote(repo, "refs/heads/" + step["source"])
                source = request.source_sha or source_tip
                if not source or not is_valid_git_oid(source):
                    raise PromotionRefusal(
                        "promotion_source_not_on_chain", "Source must be an exact commit."
                    )
                if not source_tip:
                    raise PromotionRefusal(
                        "promotion_source_not_on_chain", "Source branch is missing."
                    )
                await _fetch_commit(ops, repo, source_tip)
                await ops.exact(repo, source)
                version, notes_sha = await _source_inputs(ops, repo, step, source, request)
                suffix = version or source[:12]
                request_id = f"promotion:{repository.id}:{step['id']}:{suffix}"
                existing = (
                    (
                        await conn.execute(
                            select(integration_batches).where(
                                integration_batches.c.project_id == project.id,
                                integration_batches.c.request_id == request_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["intent"] == "aborted" or existing["lifecycle"] in (
                        "aborted",
                        "failed",
                    ):
                        raise PromotionRefusal(
                            "promotion_not_open",
                            f"Request {request_id} was {existing['lifecycle']} and its identity "
                            "cannot be reused; pin another source commit or version.",
                        )
                    return _intent_response("already_requested", existing)
                active = (
                    (
                        await conn.execute(
                            select(integration_batches).where(
                                integration_batches.c.project_id == project.id,
                                integration_batches.c.repository_id == repository.id,
                                integration_batches.c.target_ref == target_ref,
                                integration_batches.c.trigger == "promotion",
                                integration_batches.c.intent != "aborted",
                                integration_batches.c.lifecycle.not_in(
                                    ("promoted", "aborted", "failed")
                                ),
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if active:
                    raise PromotionRefusal(
                        "promotion_in_progress", f"Existing request: {active['request_id']}"
                    )
                if not source_tip or not await ops.is_ancestor(repo, source, source_tip):
                    raise PromotionRefusal(
                        "promotion_source_not_on_chain", "Source is outside the step source branch."
                    )
                base = await ops.remote(repo, target_ref)
                if base:
                    await _fetch_commit(ops, repo, base)
                if not base or not await ops.is_ancestor(repo, base, source):
                    raise PromotionRefusal(
                        "promotion_not_fast_forward", "Target is not an ancestor of the source."
                    )
                now = time.time()
                meta = {
                    "request_id": request_id,
                    "repository_id": repository.id,
                    "target_ref": target_ref,
                    "source_sha": source,
                    "base_sha": base,
                    "step": step,
                    "version": version,
                    "checks_version": required.version,
                    # Frozen from committed trust so the lane never reads S's own set.
                    "check_names": list(required.names),
                    "requested_at": now,
                    "requester": requester,
                    "notes_sha256": notes_sha,
                }
                tag = _tag_name(meta)
                if tag:
                    await ops.run(repo, "check-ref-format", "refs/tags/" + tag)
                    remote = await ops.git.als_remote_qualified_refs(
                        str(repo.store),
                        ["refs/tags/" + tag],
                        repository_url=f"https://github.com/{repo.binding.full_name}.git",
                    )
                    observed = remote["refs/tags/" + tag]
                    if observed.state is RemoteRefState.ERROR:
                        raise PromotionRefusal("unavailable", "Tag inventory is unavailable.")
                    if observed.state is RemoteRefState.PRESENT:
                        raise PromotionRefusal("tag_exists", f"Tag {tag} already exists.")
                await _guard_backmerges(conn, ops, repo, repository.id, target_ref, source)
                digest = hashlib.sha256(f"{project.id}:{request_id}".encode()).hexdigest()
                batch = Batch(
                    "promotion-" + digest, project.id, repository.id, target_ref, created_at=now
                )
                member = BatchMember("promote-" + digest[:24], source, base)
                ref = promotion_ref(step, meta)
                # Only this private head is written. The target/tag remain daemon-only.
                remote = await ops.remote(repo, ref)
                if remote is None:
                    try:
                        await ops.push(repo, ref, source, "")
                    except GitError:
                        if await ops.remote(repo, ref) != source:
                            raise
                elif remote != source:
                    raise PromotionRefusal(
                        "promotion_ref_conflict", "Request ref names another commit."
                    )
                pr_url = await client.create_pull_request(
                    title=f"Promote {step['id']} {suffix}",
                    body=f"Promote `{step['source']}` to `{step['target']}` at `{source}`.\n\n"
                    f"AQ-Promotion-Request: {request_id}\n",
                    head=ref.removeprefix("refs/heads/"),
                    base=step["target"],
                )
                from src.git.github import GitHubAccess

                meta.update(
                    pr_url=pr_url, pr_number=GitHubAccess.validate_pr_url(repo.binding, pr_url)
                )
                await _check_pull(client, repo.binding, meta)
                # Retention and the PR are recoverable by deterministic identity if
                # this transaction is rolled back after an ambiguous provider response.
                await StepAdmission(self.db, ops, step).retain(batch, member, request_id)
                tree = await ops.git.atree_sha(str(repo.store), source)
                # Held only for the writes: a flow edit waits for this intent or
                # this intent sees the edit and rolls back.
                await _check_flow(conn, project.id, raw, repository.id, lock=True)
                await conn.execute(
                    insert(tasks).values(
                        id=member.task_id,
                        project_id=project.id,
                        repo_id=repository.id,
                        title=f"Promote {step['id']} {suffix}",
                        description="Daemon-authored step intent.",
                        task_type="promotion",
                        status="IN_PROGRESS",
                        pr_url=pr_url,
                        created_at=now,
                        updated_at=now,
                        created_by_kind="promotion",
                        created_by_id=requester["identity"],
                    )
                )
                await conn.execute(
                    insert(task_context).values(
                        id="intent-" + digest,
                        task_id=member.task_id,
                        type=PROMOTION_CONTEXT,
                        content=json.dumps(meta, sort_keys=True),
                        created_at=now,
                    )
                )
                await BatchStore(self.db).freeze(
                    batch, (member,), trees={member.task_id: tree}, promotion=meta, conn=conn
                )
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == batch.id,
                    )
                    .values(pr_url=pr_url)
                )
                return self._woken({
                    "success": True,
                    "outcome": "requested",
                    "project_id": project.id,
                    "request_id": request_id,
                    "batch_id": batch.id,
                    "task_id": member.task_id,
                    "intent": "open",
                    "promotion": meta,
                    "pr_url": pr_url,
                }, project.id, repository.id, target_ref)
        except PromotionRefusal as exc:
            return exc.response()
        except (GitError, GitHubAccessError, OSError, ValueError) as exc:
            return _unavailable(exc)

    def _woken(self, result, project_id, repository_id, target_ref):
        """Let the train revisit a promotion target this command just changed."""
        train = getattr(self.orchestrator, "integration_train", None)
        if train is not None and result.get("success"):
            train.wake(project_id, repository_id, target_ref)
        return result

    async def _promotion_intent(self, project_id, request_id):
        async with self.db._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        select(integration_batches).where(
                            integration_batches.c.project_id == project_id,
                            integration_batches.c.request_id == request_id,
                            integration_batches.c.trigger == "promotion",
                        )
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise PromotionRefusal("not_found", "Promotion request does not exist.")
        meta = row["policy_snapshot"].get(PROMOTION_CONTEXT)
        if not meta:
            raise PromotionRefusal("promotion_intent_invalid", "Promotion identity is missing.")
        return row, meta

    async def _cmd_promote_approve(self, args):
        from src.commands.contracts.promote import PromoteIntentArgs
        from src.git.github_contracts import GitHubCredentialMode
        from src.integration.promotion_steps import StepPullRequestGate, cache_promotion_review

        request = PromoteIntentArgs.model_validate(args)
        try:
            project, repository, _, _ = await self._promotion_inputs(request.project_id, read=True)
            row, meta = await self._promotion_intent(project.id, request.request_id)
            principal = current_principal() or TRUSTED_LOCAL
            # The review is posted with this host's gh login, so only the human
            # at this host may approve; every other principal is refused.
            if principal.kind is not PrincipalKind.LOCAL:
                raise PromotionRefusal(
                    "unauthorized", "Approval requires the local human operator."
                )
            approval = meta["step"]["gate"]["approval"]
            if approval == "none":
                raise PromotionRefusal("approval_not_required", "Step requires checks alone.")
            if (
                approval == "requester"
                and _requester_identity(principal) != meta["requester"]["identity"]
            ):
                raise PromotionRefusal(
                    "unauthorized", "Only the authenticated requester may approve this step."
                )
            if row["intent"] == "aborted" or row["lifecycle"] == "promoted":
                raise PromotionRefusal("promotion_not_open", "Request is no longer open.")
            _, ops, repo, _ = await self._promotion_runtime(project, repository, meta["step"])
            client = self._promotion_user_client(repo.binding)
            if client.credential_identity.mode is not GitHubCredentialMode.EXISTING_LOGIN:
                raise PromotionRefusal("unauthorized", "Approval cannot use App credentials.")
            login = await _human_login(client)
            if approval == "operator":
                allowed = meta["step"]["gate"].get("operator_logins")
                if (
                    allowed is not None
                    and login.casefold() not in {x.casefold() for x in allowed}
                    or not await StepPullRequestGate(client, repo.binding)._repository_operator(
                        login
                    )
                ):
                    raise PromotionRefusal(
                        "unauthorized",
                        "Approver must be a permitted human repository administrator.",
                    )
            elif login.casefold() != (meta["requester"].get("github_login") or "").casefold():
                raise PromotionRefusal(
                    "unauthorized", "gh login does not match the requester's verified identity."
                )
            # Serialize with cancel and publication; a review never opens a local gate.
            async with self.db.immediate() as conn:
                locked = (
                    (
                        await conn.execute(
                            select(integration_batches)
                            .where(
                                integration_batches.c.id == row["id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if locked["intent"] != "open" or locked["lifecycle"] == "promoted":
                    raise PromotionRefusal("promotion_not_open", "Request is no longer open.")
                await _check_pull(client, repo.binding, meta)
                path = (
                    f"/repositories/{repo.binding.repository_id}/pulls/{meta['pr_number']}/reviews"
                )
                reviews = await client.paged_list(path + "?per_page=100")
                latest = [
                    r
                    for r in reviews
                    if (r.get("user") or {}).get("login", "").casefold() == login.casefold()
                    and r.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
                ]
                last = max(latest, key=lambda r: r.get("id", 0), default={})
                already = (
                    last.get("state") == "APPROVED" and last.get("commit_id") == meta["source_sha"]
                )
                review = (
                    last
                    if already
                    else await client.request_json(
                        "POST",
                        path,
                        json_body={
                            "event": "APPROVE",
                            "commit_id": meta["source_sha"],
                            "body": f"AQ-Promotion-Request: {meta['request_id']}",
                        },
                        expected_statuses={200, 201},
                    )
                )
                if (
                    review.get("state") != "APPROVED"
                    or review.get("commit_id") != meta["source_sha"]
                    or (review.get("user") or {}).get("type") != "User"
                    or (review.get("user") or {}).get("login", "").casefold() != login.casefold()
                ):
                    raise PromotionRefusal(
                        "promotion_review_invalid",
                        "GitHub did not confirm the user's pinned approval.",
                    )
                members = await BatchStore(self.db).members(row["id"])
                gate = StepPullRequestGate(client, repo.binding)
                reason = await gate.observe(Batch.from_row(locked), meta)
                await cache_promotion_review(
                    conn,
                    repository.id,
                    members[0],
                    await ops.git.atree_sha(str(repo.store), meta["source_sha"]),
                    reason,
                    {**gate.evidence, "posted_by": login},
                )
            return self._woken({
                "success": True,
                "outcome": "already_approved" if already else "approved",
                "project_id": project.id,
                "request_id": request.request_id,
                "batch_id": row["id"],
                "pr_url": meta["pr_url"],
                "review": review,
            }, project.id, row["repository_id"], row["target_ref"])
        except PromotionRefusal as exc:
            return exc.response()
        except (GitError, GitHubAccessError, OSError, ValueError) as exc:
            return _unavailable(exc)

    async def _cmd_promote_cancel(self, args):
        from src.commands.contracts.promote import PromoteIntentArgs
        from src.integration.lock import BranchLock, managed
        from src.integration.models import BranchKey

        request = PromoteIntentArgs.model_validate(args)
        try:
            project, repository, _, _ = await self._promotion_inputs(
                request.project_id, command="promote_cancel"
            )
            row, meta = await self._promotion_intent(project.id, request.request_id)
            principal = current_principal() or TRUSTED_LOCAL
            if (
                principal.kind is not PrincipalKind.LOCAL
                and meta["requester"]["identity"] != _requester_identity(principal)
            ):
                raise PromotionRefusal(
                    "unauthorized", "Only the local operator or the requester may cancel."
                )
            if row["intent"] == "aborted":
                return _intent_response("already_cancelled", row)
            lane, ops, repo, client = await self._promotion_runtime(
                project, repository, meta["step"]
            )
            async with self.db.immediate() as conn:
                locked = (
                    (
                        await conn.execute(
                            select(integration_batches)
                            .where(
                                integration_batches.c.id == row["id"],
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one()
                )
                if locked["intent"] == "aborted":
                    return _intent_response("already_cancelled", locked)
                if locked["lifecycle"] == "promoted":
                    raise PromotionRefusal(
                        "promotion_publish_started", "Promotion is already delivered."
                    )
                lease = await BranchLock(self.db).lock_on(
                    conn,
                    BranchKey(
                        repository_id=repository.id,
                        branch=row["target_ref"],
                    ),
                )
                if managed(lease) and lease["holder"] and lease["expires_at"] > time.time():
                    raise PromotionRefusal(
                        "promotion_publish_started", "Target has an active publication lease."
                    )
                target = await ops.remote(repo, row["target_ref"])
                if target is None:
                    raise PromotionRefusal("unavailable", "Target cannot be observed.")
                await _fetch_commit(ops, repo, target)
                if await ops.is_ancestor(repo, meta["source_sha"], target):
                    raise PromotionRefusal(
                        "promotion_publish_started", "Target already contains the source."
                    )
                await _check_pull(client, repo.binding, meta, allow_closed=True)
                await client.close_pull_request(number=meta["pr_number"])
                members = await lane.service.store.members(row["id"])
                now = time.time()
                await conn.execute(
                    update(integration_batches)
                    .where(
                        integration_batches.c.id == row["id"],
                    )
                    .values(
                        intent="aborted",
                        lifecycle="aborted",
                        cleanup_state="pending",
                        human_abort_reason=f"Cancelled by {_requester_identity(principal)}",
                        updated_at=now,
                    )
                )
                await conn.execute(
                    update(tasks)
                    .where(tasks.c.id == members[0].task_id, tasks.c.status == "IN_PROGRESS")
                    .values(status="FAILED", updated_at=now)
                )
            return self._woken({
                "success": True,
                "outcome": "cancelled",
                "project_id": project.id,
                "request_id": request.request_id,
                "batch_id": row["id"],
                "pr_url": meta["pr_url"],
            }, project.id, row["repository_id"], row["target_ref"])
        except PromotionRefusal as exc:
            return exc.response()
        except (GitError, GitHubAccessError, OSError, ValueError) as exc:
            return _unavailable(exc)

    async def _cmd_promote_status(self, args):
        return await self._promotion_cached(args, "status")

    async def _cmd_promote_list(self, args):
        return await self._promotion_cached(args, "listed")

    async def _promotion_cached(self, args, outcome):
        from src.commands.contracts.promote import PromoteReadArgs

        request = PromoteReadArgs.model_validate(args)
        try:
            project, repository, _, flow = await self._promotion_inputs(
                request.project_id, read=True
            )
            if request.step_id:
                _find_step(flow, request.step_id)
            async with self.db._engine.connect() as conn:
                query = select(integration_batches).where(
                    integration_batches.c.project_id == project.id,
                    integration_batches.c.repository_id == repository.id,
                    integration_batches.c.trigger == "promotion",
                )
                if request.step_id:
                    query = query.where(
                        integration_batches.c.policy_snapshot["promotion_step"]["id"].as_string()
                        == request.step_id
                    )
                rows = await conn.execute(
                    query.order_by(
                        integration_batches.c.created_at.desc(), integration_batches.c.id
                    ).limit(request.limit)
                )
                entries = []
                for row in rows.mappings():
                    meta = row["policy_snapshot"][PROMOTION_CONTEXT]
                    members = (
                        (
                            await conn.execute(
                                select(integration_batch_members).where(
                                    integration_batch_members.c.batch_id == row["id"],
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    if len(members) != 1:
                        continue
                    task_id = members[0]["task_id"]
                    checks = (
                        (
                            await conn.execute(
                                select(integration_check_evidence).where(
                                    integration_check_evidence.c.repository_id == repository.id,
                                    integration_check_evidence.c.sha == meta["source_sha"],
                                    integration_check_evidence.c.required_check_version
                                    == meta["checks_version"],
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    review = (
                        (
                            await conn.execute(
                                select(integration_review_evidence)
                                .where(
                                    integration_review_evidence.c.source_task_id == task_id,
                                    integration_review_evidence.c.repository_id == repository.id,
                                    integration_review_evidence.c.review_kind == "promotion_pr",
                                    integration_review_evidence.c.reviewed_head_sha
                                    == meta["source_sha"],
                                )
                                .order_by(
                                    integration_review_evidence.c.created_at.desc(),
                                    integration_review_evidence.c.id.desc(),
                                )
                                .limit(1)
                            )
                        )
                        .mappings()
                        .first()
                    )
                    result = await conn.scalar(
                        select(task_metadata.c.value).where(
                            task_metadata.c.task_id == task_id,
                            task_metadata.c.key == PROMOTION_RESULT,
                        )
                    )
                    age = max(0, time.time() - meta["requested_at"])
                    ttl = meta["step"]["gate"]["request_ttl"]
                    expired = (
                        age
                        > int(ttl[:-1])
                        * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[ttl[-1]]
                    )
                    entries.append(
                        {
                            "batch_id": row["id"],
                            "task_id": task_id,
                            "intent": row["intent"],
                            "lifecycle": row["lifecycle"],
                            "promotion": meta,
                            "age_seconds": age,
                            "expired": expired
                            and row["lifecycle"] != "promoted"
                            and row["intent"] != "aborted",
                            "checks": [dict(check) for check in checks],
                            "review": dict(review) if review else None,
                            "result": json.loads(result) if result else None,
                            "tag": _tag_name(meta),
                        }
                    )
            return {
                "success": True,
                "outcome": outcome,
                "project_id": project.id,
                "flow": flow,
                "promotions": entries,
                "evidence_source": "cache",
            }
        except PromotionRefusal as exc:
            return exc.response()


class PromotionRefusal(ValueError):
    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome

    def response(self):
        return {"success": False, "outcome": self.outcome, "error": str(self)}


def _unavailable(exc):
    limit = rate_limit_cause(exc)
    if limit is not None:
        return {
            "success": False,
            "outcome": "rate_limited",
            "error": str(limit),
            "retry_at": limit.retry_at,
        }
    return {"success": False, "outcome": "unavailable", "error": str(exc)}


def _find_step(flow, step_id):
    for step in flow:
        if step["id"] == step_id:
            return step
    raise PromotionRefusal("step_not_found", f"Unknown promotion step {step_id!r}.")


def _requester_identity(principal):
    return "human:local-operator" if principal.kind is PrincipalKind.LOCAL else principal.describe()


async def _fetch_commit(ops, repo, sha):
    present = await ops.git.arun_git_result(
        ["cat-file", "-e", sha + "^{commit}"], cwd=str(repo.store)
    )
    if present.returncode:
        await ops.git.afetch_repository_oid(
            str(repo.store),
            repository=repo.binding,
            oid=sha,
            destination_ref="refs/aq/promotion-admission/" + sha,
        )
    await ops.exact(repo, sha)


async def _human_login(client):
    user = await client.authenticated_user()
    if user.get("type") != "User" or not isinstance(user.get("login"), str) or not user["login"]:
        raise PromotionRefusal("unauthorized", "An authenticated human gh login is required.")
    return user["login"]


def _tag_name(meta):
    step = meta["step"]
    if step["versioning"]["kind"] == "none":
        return None
    date = datetime.fromtimestamp(meta["requested_at"], UTC).strftime("%Y-%m-%d")
    return step["versioning"]["tag_format"].format(
        version=meta["version"],
        sha12=meta["source_sha"][:12],
        utc_date=date,
        step=step["id"],
    )


async def _source_inputs(ops, repo, step, source, request):
    version = request.version
    versioning = step["versioning"]
    if versioning["kind"] == "semver_tag":
        selector = versioning["source"]
        if selector == "pyproject":
            import tomllib

            version_at_source = (
                tomllib.loads(await ops.run(repo, "show", source + ":pyproject.toml"))
                .get("project", {})
                .get("version")
            )
        elif selector == "package.json":
            version_at_source = json.loads(
                await ops.run(repo, "show", source + ":package.json")
            ).get("version")
        else:
            path, pattern = selector[5:].split(":", 1)
            match = re.search(pattern, await ops.run(repo, "show", source + ":" + path))
            if not match or not match.groups():
                raise PromotionRefusal("version_mismatch", "Version source did not match.")
            version_at_source = match.group(1)
        if version and version != version_at_source:
            raise PromotionRefusal(
                "version_mismatch", "Requested version differs from the pinned source."
            )
        version = version_at_source
        if (
            not isinstance(version, str)
            or re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None
        ):
            raise PromotionRefusal(
                "version_mismatch", "Pinned source must contain a semver release version."
            )
    elif versioning["kind"] == "none" and version:
        raise PromotionRefusal("step_not_versioned", "Unversioned step cannot take --version.")
    elif versioning["kind"] == "custom" and not version:
        raise PromotionRefusal("version_mismatch", "Custom versioned step requires --version.")
    notes_sha = None
    if step["notes"]["kind"] != "none":
        if not request.notes_reviewed:
            raise PromotionRefusal(
                "notes_not_reviewed",
                "Read the notes at the pinned source and pass --notes-reviewed.",
            )
        path = step["notes"]["path"].format(version=version)
        result = await ops.git.arun_git_result(["show", source + ":" + path], cwd=str(repo.store))
        if result.returncode:
            raise PromotionRefusal("notes_not_reviewed", "Pinned notes cannot be read.")
        notes_sha = hashlib.sha256(result.stdout.encode()).hexdigest()
    return version, notes_sha


async def _guard_backmerges(conn, ops, repo, repository_id, target_ref, source):
    # B10 can author this existing task metadata; no new ledger or scheduler.
    rows = (
        (
            await conn.execute(
                select(task_metadata.c.value)
                .join(
                    tasks,
                    tasks.c.id == task_metadata.c.task_id,
                )
                .where(
                    tasks.c.repo_id == repository_id,
                    tasks.c.task_type == "backmerge",
                    tasks.c.status.not_in(("COMPLETED", "FAILED")),
                    task_metadata.c.key == "backmerge",
                )
            )
        )
        .scalars()
        .all()
    )
    for value in rows:
        entry = json.loads(value)
        if entry.get("target_ref") == target_ref and not await ops.is_ancestor(
            repo, entry["source_sha"], source
        ):
            raise PromotionRefusal(
                "backmerge_pending", "Source does not contain an outstanding backmerge."
            )


async def _check_pull(client, binding, meta, *, allow_closed=False):
    pull = await client.request_json(
        "GET", f"/repositories/{binding.repository_id}/pulls/{meta['pr_number']}"
    )
    identity = {"id": binding.repository_id, "full_name": binding.full_name}
    head, base = pull.get("head") or {}, pull.get("base") or {}
    if (
        pull.get("number") != meta["pr_number"]
        or pull.get("html_url") != meta["pr_url"]
        or head.get("sha") != meta["source_sha"]
        or head.get("ref") != promotion_ref(meta["step"], meta).removeprefix("refs/heads/")
        or base.get("ref") != meta["step"]["target"]
        or any(
            (side.get("repo") or {}).get(k) != v
            for side in (head, base)
            for k, v in identity.items()
        )
        or pull.get("state") not in ({"open", "closed"} if allow_closed else {"open"})
        or pull.get("draft") is not False
        or pull.get("merged") is True
    ):
        raise PromotionRefusal(
            "promotion_pr_identity_mismatch", "PR is no longer the pinned open step PR."
        )
    return pull


async def _check_flow(conn, project_id, raw, repository_id, *, lock=False):
    query = select(projects.c.promotion_flow, projects.c.integration_repository_id).where(
        projects.c.id == project_id
    )
    if lock:
        query = query.with_for_update(read=True)
    current = (await conn.execute(query)).mappings().one()
    if current["promotion_flow"] != raw or current["integration_repository_id"] != repository_id:
        raise PromotionRefusal("promotion_flow_changed", "Flow changed; request again.")


def _intent_response(outcome, row):
    return {
        "success": True,
        "outcome": outcome,
        "project_id": row["project_id"],
        "request_id": row["request_id"],
        "batch_id": row["id"],
        "intent": row["intent"],
        "promotion": row["policy_snapshot"][PROMOTION_CONTEXT],
        "pr_url": row["policy_snapshot"][PROMOTION_CONTEXT]["pr_url"],
    }
