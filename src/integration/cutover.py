"""Promotion cutover inventory and generation-fenced configuration changes.

The workflow change rides the train before cutover. GitHub rulesets and the
repository's GitHub default are operator steps, printed in the plan. This
command never force-moves a branch or rewrites a frozen batch.
"""

from __future__ import annotations

import fnmatch
import copy
import json
import time
from urllib.parse import quote, urlencode
from types import SimpleNamespace

import yaml
from sqlalchemy import and_, case, cast, func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError

from src.database.tables import (
    archived_tasks, events, integration_batch_members, integration_batches, integration_branch_owners,
    integration_promotion_intents, integration_subjects, projects, repos,
    task_branch_origins, task_delivery_receipts, tasks,
)
from src.git.manager import RemoteRefState
from src.integration.batches import Batch, BatchStore
from src.integration.drain_owners import terminal_reservation_clause
from src.integration.lock import BranchLock
from src.integration.models import BranchKey, IntegrationTrainPolicy, RequiredCheckSet
from src.integration.promotion_steps import FlowSchema, create_missing_targets, flow_targets
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.delivery_truth import load_delivery_requests
from src.git.github_contracts import (
    GitHubAccessError, GitHubCredentialMode, credential_identity_from_client,
)
from src.integration.ci import IntegrationTrustManifest
from src.integration.protection import ATTESTED_ONLY, classify
from src.integration.trust_manifest import ATTESTATION_NAME
from src.integration.records import PolicyActivation
from src.integration.train import TrainTarget
from src.integration.train_sources import project_snapshot


def _epic(ref):
    return str(ref or "").removeprefix("refs/heads/").startswith("aq/epic/")


async def inventory_on(conn, project_id, repository_id, *, now):
    """Repository-scoped live authority, using the train's definition of open."""
    statements = {
        "subjects": select(integration_subjects).where(
            integration_subjects.c.project_id == project_id,
            integration_subjects.c.repository_id == repository_id,
            integration_subjects.c.phase != "done"),
        "owners": select(integration_branch_owners).where(
            integration_branch_owners.c.repository_id == repository_id,
            or_(and_(integration_branch_owners.c.handoff_state != "released",
                     ~terminal_reservation_clause(allow_cleanup_history=True)),
                and_(integration_branch_owners.c.holder.is_not(None),
                     integration_branch_owners.c.expires_at > now))),
        "intents": select(integration_promotion_intents).where(
            integration_promotion_intents.c.repository_id == repository_id,
            integration_promotion_intents.c.state.not_in(("committed", "conflict", "superseded"))),
        "batches": select(integration_batches).where(
            integration_batches.c.project_id == project_id,
            integration_batches.c.repository_id == repository_id,
            integration_batches.c.target_ref.is_not(None),
            integration_batches.c.intent != "aborted",
            integration_batches.c.lifecycle != "promoted"),
    }
    return {name: [dict(row) for row in (await conn.execute(statement)).mappings()]
            for name, statement in statements.items()}


def branch_spellings(branch):
    name = branch.removeprefix("refs/heads/")
    return (name, "refs/heads/" + name)


async def receipt_targets_on(conn, repository_id, branch, *, prior=None):
    statement = select(task_delivery_receipts.c.id, task_delivery_receipts.c.target_branch).where(
        task_delivery_receipts.c.repository_id == repository_id,
        task_delivery_receipts.c.target_branch.in_(branch_spellings(branch)))
    if prior is not None:
        statement = statement.where(task_delivery_receipts.c.id.in_(prior))
    return dict((await conn.execute(statement.order_by(task_delivery_receipts.c.id))).all())


async def aborted_members_on(conn, project_id, repository_id, default_branch):
    # Other event types may carry plain text. Do not parse them as JSON.
    payload = case((events.c.event_type.in_(("integration.batch_intent",
        "integration.batch_ejected", "integration.batch_superseded")),
        cast(events.c.payload, JSONB)), else_=None)
    aborted_at = select(func.min(events.c.timestamp)).where(
        events.c.project_id == project_id,
        payload["batch_id"].astext == integration_batches.c.id,
        or_(and_(events.c.event_type == "integration.batch_intent",
                 payload["intent"].astext == "aborted"),
            events.c.event_type.in_(("integration.batch_ejected", "integration.batch_superseded"))),
    ).correlate(integration_batches).scalar_subquery()
    return [dict(row) for row in (await conn.execute(select(
        integration_batch_members.c.batch_id, integration_batch_members.c.task_id,
        integration_batch_members.c.source_sha,
        integration_batches.c.human_abort_reason.label("abort_reason"),
        aborted_at.label("aborted_at")).join(integration_batches,
            integration_batches.c.id == integration_batch_members.c.batch_id).join(tasks,
                tasks.c.id == integration_batch_members.c.task_id).where(
                    integration_batches.c.project_id == project_id,
                    integration_batches.c.repository_id == repository_id,
                    integration_batches.c.target_ref.in_(branch_spellings(default_branch)),
                    integration_batches.c.intent == "aborted", tasks.c.status == "COMPLETED",
                    ~select(task_delivery_receipts.c.id).where(
                        task_delivery_receipts.c.source_task_id == tasks.c.id,
                        task_delivery_receipts.c.repository_id == repository_id,
                        task_delivery_receipts.c.target_branch.in_(
                            branch_spellings(default_branch))).exists(),
                ).order_by(integration_batch_members.c.batch_id,
                           integration_batch_members.c.task_id))).mappings()]


async def origin_ids_on(conn, project_id, repository_id, default_branch, *, prior=None, lock=False):
    # Roots include leaves and closed/archived containers. Filing origins remain immutable.
    project_tasks = select(tasks.c.id).where(tasks.c.project_id == project_id).union(
        select(archived_tasks.c.id).where(archived_tasks.c.project_id == project_id))
    statement = select(task_branch_origins.c.id).where(
        task_branch_origins.c.task_id.in_(project_tasks),
        task_branch_origins.c.repository_id == repository_id,
        task_branch_origins.c.retired_at.is_(None),
        task_branch_origins.c.parent_task_id.is_(None),
        or_(task_branch_origins.c.parent_repository_id.is_(None),
            task_branch_origins.c.parent_repository_id == repository_id),
        task_branch_origins.c.parent_ref.in_(branch_spellings(default_branch)))
    if prior is not None:
        statement = statement.where(task_branch_origins.c.id.in_(prior))
    statement = statement.order_by(task_branch_origins.c.id)
    if lock:
        statement = statement.with_for_update()
    return list((await conn.execute(statement)).scalars())


def barrier(inventory, *, allow_epics, targets, project_id):
    blockers = []
    for kind, field in (("subjects", "target_ref"), ("owners", "ref"),
                        ("intents", "target_branch"), ("batches", "target_ref")):
        for row in inventory[kind]:
            ref = row[field]
            if allow_epics and _epic(ref) and str(ref).removeprefix("refs/heads/") not in targets:
                continue
            remedy = f"aq integration status {project_id}"
            if kind == "batches":
                remedy = f"aq integration abort-batch {row['id']} --reason cutover --apply"
            elif kind == "owners":
                remedy = f"aq integration release-owner --owner-row-id {row['id']} --dry-run"
            blockers.append({"code": "live_" + kind, "id": row["id"], "ref": ref,
                             "control": remedy})
    return blockers


def workflow_branches(text):
    """BaseLoader keeps YAML's 'on' key literal (SafeLoader treats it as True)."""
    document = yaml.load(text, Loader=yaml.BaseLoader)
    triggers = document.get("on") if isinstance(document, dict) else None
    trigger = triggers.get("pull_request") if isinstance(triggers, dict) else None
    branches = trigger.get("branches") if isinstance(trigger, dict) else None
    if not isinstance(branches, list) or not all(isinstance(item, str) for item in branches):
        raise ValueError("tests.yml must list pull_request branches explicitly")
    return branches


class CutoverGit:
    """Read Git/PR facts and create a missing default at the captured source OID."""

    def __init__(self, db, client):
        self.db, self.client = db, client

    async def remote_state(self, branches):
        root = f"/repositories/{self.client.repository.repository_id}"
        repo = await self.client.request_json("GET", root)
        rows = await self.client.paged_list(
            root + "/rulesets?per_page=100&includes_parents=true", max_pages=10)
        rulesets = [await self.client.request_json("GET", f"{root}/rulesets/{row['id']}")
                    for row in rows]
        protection = {}
        for branch in sorted(branches):
            path = f"{root}/branches/{quote(branch, safe='')}/protection"
            try:
                protection[branch] = await self.client.request_json("GET", path)
            except GitHubAccessError as exc:
                if exc.category != "not_found_or_hidden":
                    raise
                protection[branch] = None
        # Remove response metadata, retaining every policy field for exact reverse.
        rulesets = [{key: row[key] for key in (
            "id", "name", "target", "enforcement", "conditions", "rules", "bypass_actors")
            if key in row} for row in rulesets]
        return {"default_branch": repo["default_branch"],
                "rulesets": sorted(rulesets, key=lambda row: row["id"]),
                "protection": protection}

    async def observe(self, project_id, repository, default_branch, *, branches):
        snapshot = await project_snapshot(self.db, TrainTarget(
            project_id, repository.id, "refs/heads/" + repository.default_branch))
        if snapshot is None or snapshot.error or not snapshot.target_oid:
            raise ValueError("integration Git observation is unavailable")
        self.snapshot, self.repository = snapshot, repository
        observed = snapshot.observation
        remote = await observed.git.als_remote_ref(
            observed.store, default_branch, repository_url=repository.url)
        if remote.state is RemoteRefState.ERROR:
            raise ValueError("new default branch cannot be observed")
        self.target_oid = remote.oid
        text = await observed.git._arun(
            ["show", f"{snapshot.target_oid}:.github/workflows/tests.yml"], cwd=observed.store)
        manifest = json.loads(await observed.git._arun(
            ["show", f"{snapshot.target_oid}:.github/agent-queue-integration.json"],
            cwd=observed.store))
        prs = []
        # Explicit pagination: an incomplete PR inventory cannot authorize a flip.
        for page in range(1, 101):
            query = urlencode({"state": "open", "base": repository.default_branch,
                               "per_page": 100, "page": page})
            response = await self.client.request(
                "GET", f"repos/{self.client.repository.full_name}/pulls?{query}")
            rows = json.loads(response.body)
            if not isinstance(rows, list):
                raise ValueError("open PR inventory is unavailable")
            prs.extend({key: row[key] for key in ("number", "html_url", "title")} for row in rows)
            if len(rows) < 100:
                break
        else:
            raise ValueError("open PR inventory exceeds the cutover bound")
        return {"cut_oid": snapshot.target_oid, "target_oid": remote.oid,
                "workflow_branches": workflow_branches(text), "open_prs": prs,
                "manifest": manifest, "remote_state": await self.remote_state(branches)}

    async def retained(self, project_id, repository, ids):
        observed = self.snapshot.observation
        requests = await load_delivery_requests(self.db, ids, repository_id=repository.id,
            target_ref="refs/heads/" + repository.default_branch, reduced=True)
        provenance = GitProvenance(observed.git, observed.store, repository_url=repository.url)
        retained = []
        for task_id, request in requests.items():
            record = await provenance.read_completion(CompletionIdentity(
                project_id, repository.id, task_id, request.completion_id or request.legacy_generation),
                refs=observed.source_heads)
            if record is not None:
                retained.append(task_id)
        return sorted(retained)

    async def verify(self, plan, *, policy):
        branches = set(plan["remote_state"]["protection"])
        state = await self.remote_state(branches)
        if plan["reverse"]:
            if state != plan["prior"]["remote_state"]:
                raise ValueError("github_state_not_restored: restore the recorded default/rulesets/protection")
            return
        if state["default_branch"] != plan["default_branch"]:
            raise ValueError("github_default_pending: operator must flip the GitHub default branch")
        root = f"/repositories/{self.client.repository.repository_id}"
        manifest = plan["manifest"]
        trust = IntegrationTrustManifest.model_validate(manifest)
        identity = credential_identity_from_client(self.client)
        if (trust.canonical_repository_id != plan["repository_id"]
                or trust.repository_id != self.client.repository.repository_id
                or trust.full_name != self.client.repository.full_name
                or identity.mode is not GitHubCredentialMode.APP
                or identity.app_id != trust.attestation_app_id):
            raise ValueError("github_trust_mismatch: verify the repository and attestation App")
        raw_checks = (policy or {}).get("root", {}).get("required_checks")
        protection_policy = (SimpleNamespace(root=SimpleNamespace(
            required_checks=RequiredCheckSet.model_validate(raw_checks))) if raw_checks else None)
        for branch, attestation in [(plan["default_branch"], ATTESTATION_NAME), *(
                (step["target"], step["gate"]["attestation"]) for step in plan["flow"])]:
            effective = await self.client.paged_list(
                f"{root}/rules/branches/{quote(branch, safe='')}?per_page=100", max_pages=10)
            bypass = {}
            for rule in effective:
                id_ = rule.get("ruleset_id")
                if id_ is not None and id_ not in bypass:
                    row = await self.client.request_json("GET", f"{root}/rulesets/{id_}")
                    bypass[id_] = row.get("current_user_can_bypass")
            # The existing classifier knows the root context. Normalize only the
            # flow's trusted context; every other check retains its identity.
            normalized = copy.deepcopy(effective)
            classic = copy.deepcopy(state["protection"][branch])
            for rule in normalized:
                for check in rule.get("parameters", {}).get("required_status_checks", []):
                    if attestation != ATTESTATION_NAME and check.get("context") == ATTESTATION_NAME:
                        raise ValueError(f"github_rulesets_pending: {branch}: wrong attestation context")
                    if check.get("context") == attestation:
                        check["context"] = ATTESTATION_NAME
            if classic:
                for check in classic.get("required_status_checks", {}).get("checks", []):
                    if attestation != ATTESTATION_NAME and check.get("context") == ATTESTATION_NAME:
                        raise ValueError(f"github_rulesets_pending: {branch}: wrong attestation context")
                    if check.get("context") == attestation:
                        check["context"] = ATTESTATION_NAME
            reading = classify(normalized, bypass, classic,
                               app_id=manifest.get("attestation_app_id"), policy=protection_policy)
            if reading.classification != ATTESTED_ONLY:
                raise ValueError(f"github_rulesets_pending: {branch}: "
                                 f"{reading.reason or reading.classification}")
        for step in plan["flow"]:
            if step["versioning"]["kind"] == "none":
                continue
            pattern = "refs/tags/" + step["versioning"]["tag_format"].replace("{version}", "*")
            matching = [row for row in state["rulesets"] if row.get("target") == "tag"
                        and row.get("enforcement") == "active"
                        and pattern in row.get("conditions", {}).get("ref_name", {}).get("include", [])
                        and not row.get("conditions", {}).get("ref_name", {}).get("exclude")]
            immutable = any(not row.get("bypass_actors") and {"update", "deletion"} <= {
                rule["type"] for rule in row.get("rules", [])} for row in matching)
            creation = any(row.get("bypass_actors") == [{"actor_type": "Integration",
                               "actor_id": trust.attestation_app_id, "bypass_mode": "always"}]
                           and any(rule["type"] == "creation" for rule in row.get("rules", []))
                           for row in matching)
            if not immutable or not creation:
                raise ValueError(f"github_tag_rulesets_pending: {pattern}")

    async def prepare(self, default_branch, *, reverse):
        observed, old_oid = self.snapshot.observation, self.snapshot.target_oid
        if not await self.snapshot.is_fresh():
            raise ValueError("source moved; regenerate cutover-plan")
        remote = await observed.git.als_remote_ref(
            observed.store, default_branch, repository_url=self.repository.url)
        if remote.state is RemoteRefState.ERROR or remote.oid != self.target_oid:
            raise ValueError("target moved; regenerate cutover-plan")
        if remote.state is RemoteRefState.ABSENT:
            if reverse:
                raise ValueError("prior default is missing; restore it through the operator control")
            await observed.git._apush_oid(
                observed.store, old_oid, default_branch,
                expected_old_oid="0" * 40,
                repository_url=self.repository.url)
        elif not reverse and remote.oid != old_oid:
            raise ValueError("new default must equal the captured root OID; no branch was moved")
        elif reverse and not await GitProvenance(observed.git, observed.store,
                                    repository_url=self.repository.url).ancestor(old_oid, remote.oid):
            raise ValueError("prior default must contain the current root; fast-forward it by hand")
        verified = await observed.git.als_remote_ref(
            observed.store, default_branch, repository_url=self.repository.url)
        if verified.state is not RemoteRefState.PRESENT or verified.oid != (remote.oid or old_oid):
            raise ValueError("default branch push was not verified; no configuration changed")

    async def prepare_flow(self, flow):
        observed = self.snapshot.observation
        refusal, _created = await create_missing_targets(
            observed.git, observed.store, flow or [], repository_url=self.repository.url)
        if refusal:
            raise ValueError(refusal.get("message") or refusal["outcome"])


class Cutover:
    def __init__(self, db, git, *, clock=time.time):
        self.db, self.git, self.clock = db, git, clock

    async def plan(self, project_id, flow, *, allow_epics=False, reverse=False):
        async with self.db._engine.connect() as conn:
            project = (await conn.execute(select(projects).where(
                projects.c.id == project_id))).mappings().one_or_none()
            if project is None:
                raise ValueError("project is missing")
            repository = await self.db.get_repo(project["integration_repository_id"] or "")
            if repository is None or repository.project_id != project_id:
                raise ValueError("project has no designated integration repository")
            prior = None
            if reverse:
                payload = await conn.scalar(select(events.c.payload).where(
                    events.c.project_id == project_id,
                    events.c.event_type == "integration.cutover").order_by(events.c.id.desc()).limit(1))
                prior = json.loads(payload) if payload else None
                if not prior or prior["reverse"] or prior["repository_id"] != repository.id:
                    raise ValueError("no unreversed cutover for this repository")
                if (not isinstance(prior.get("receipt_targets"), dict)
                        or set(prior["receipt_targets"]) != set(prior["receipt_ids"])):
                    raise ValueError("cutover_audit_incomplete: exact receipt targets are missing; "
                                     "inspect the cutover audit before reversing")
                if (repository.default_branch != prior["new_default"]
                        or project["promotion_flow"] != prior["new_flow"]):
                    raise ValueError("cutover configuration changed; inspect before reversing")
                default, flow = prior["old_default"], prior["old_flow"]
            else:
                raw = flow.get("promotion_flow") if isinstance(flow, dict) else flow
                if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
                    raise ValueError("a nonempty promotion flow is required")
                default = raw[0].get("source", "")
                result = FlowSchema.validate(flow, default_branch=default)
                if any(problem.layer < 3 for problem in result.problems):
                    raise ValueError("invalid promotion flow: " + result.problems[0].code)
                flow = result.flow
            inventory = await inventory_on(conn, project_id, repository.id, now=self.clock())
            receipt_targets = await receipt_targets_on(conn, repository.id,
                repository.default_branch, prior=prior["receipt_ids"] if reverse else None)
            receipt_ids = list(receipt_targets)
            origin_ids = await origin_ids_on(conn, project_id, repository.id,
                prior["old_default"] if reverse else repository.default_branch,
                prior=prior["origin_ids"] if reverse else None)
            # The receipt projection is advisory, never delivery authority.
            missing = (await conn.execute(select(tasks.c.id).where(
                tasks.c.project_id == project_id, tasks.c.status == "COMPLETED",
                select(task_branch_origins.c.id).where(
                    task_branch_origins.c.task_id == tasks.c.id,
                    task_branch_origins.c.repository_id == repository.id).exists(),
                ~select(task_delivery_receipts.c.id).where(
                    task_delivery_receipts.c.source_task_id == tasks.c.id,
                    task_delivery_receipts.c.repository_id == repository.id,
                    task_delivery_receipts.c.target_branch.in_(
                        branch_spellings(repository.default_branch))).exists(),
            ).order_by(tasks.c.id))).scalars().all()
        targets = set(flow_targets(flow)) | set(flow_targets(project["promotion_flow"]))
        branches = {repository.default_branch, default, *targets}
        facts = await self.git.observe(project_id, repository, default, branches=branches)
        missing = await self.git.retained(project_id, repository, missing)
        blockers = barrier(inventory, allow_epics=allow_epics, targets=targets, project_id=project_id)
        if not reverse and project["default_branch_cutover"]:
            blockers.append({"code": "cutover_already_active",
                             "control": "reverse the active cutover before replacing it"})
        if project["repo_default_branch"] != repository.default_branch:
            blockers.append({"code": "binding_mismatch",
                             "control": "reconcile project/repository default before cutover"})
        validated = FlowSchema.validate(flow, default_branch=default, manifest=facts["manifest"])
        if not validated.valid:
            blockers.append({"code": "invalid_promotion_flow", "problems": validated.as_dict()})
        async with self.db._engine.connect() as conn:
            aborted_fence = await aborted_members_on(conn, project_id, repository.id,
                                                     repository.default_branch)
            aborted = [{**row, "retained_provenance": row["task_id"] in missing}
                       for row in aborted_fence]
        required = sorted({default, *flow_targets(flow)})
        absent = [branch for branch in required if not any(
            fnmatch.fnmatchcase(branch, pattern) for pattern in facts["workflow_branches"])]
        if absent:
            blockers.append({"code": "workflow_targets_missing", "branches": absent,
                             "control": "deliver tests.yml pull_request branches through the train"})
        if reverse and facts["workflow_branches"] != prior["workflow_branches"]:
            blockers.append({"code": "workflow_changed", "expected": prior["workflow_branches"],
                             "control": "restore the prior tests.yml PR list through the train"})
        cadence = IntegrationTrainPolicy.model_validate(
            (project["hierarchical_integration_policy"] or {}).get("train") or {})
        full_name = repository.url.removeprefix("https://github.com/").removesuffix(".git")
        return {"project_id": project_id, "repository_id": repository.id,
                "generation": project["hierarchical_integration_generation"],
                "old_default": repository.default_branch, "default_branch": default,
                "binding": {"repository_url": repository.url,
                            "project_default": project["repo_default_branch"],
                            "old_flow": project["promotion_flow"],
                            "cutover": project["default_branch_cutover"]},
                "flow": flow, "reverse": reverse, "allow_epics": allow_epics,
                "inventory": inventory, "undelivered_completions": list(missing),
                "aborted_members": aborted,
                "receipt_ids": receipt_ids, "receipt_targets": receipt_targets,
                "receipt_count": len(receipt_ids),
                "origin_ids": origin_ids, "origin_count": len(origin_ids),
                "aborted_member_fence": aborted_fence,
                "aborted_member_notice": "Aborted inputs with retained provenance will be selected "
                    "on the new default after rebind. Reopen or archive content-aborted work before "
                    "applying; inputs without provenance remain held by the ordinary Git gate.",
                "cadence": cadence.model_dump(mode="json"), "blockers": blockers,
                "ready": not blockers, **facts,
                "steps": [
                    {"id": "quiesce", "change": f"aq project pause {project_id}; "
                     "finish or abort root batches; leave allowed epic targets running"},
                    {"id": "workflow", "change": f"deliver tests.yml PR triggers for {required}"},
                    {"id": "branch", "change": f"verify/create {default} from the captured root OID"},
                    *[{"id": "flow_target_" + step["id"],
                       "change": f"verify/create {step['target']} from {step['source']} "
                       "at its exact OID if absent"} for step in flow or []],
                    {"id": "github", "manual": True,
                     "change": "Restore recorded rulesets/classic protection, remove added rulesets"
                     if reverse else "Install active branch rulesets: root integration attestation, "
                     "each flow target's trusted attestation, no App bypass; install tag creation "
                     "(App only) and update/deletion (no bypass) rulesets for versioned steps",
                     "default_command": f"gh repo edit {full_name} --default-branch {default}",
                     "verify_commands": [f"aq integration app-verify {project_id}",
                                         "aq doctor --check integration.trust"],
                     "expected_state": prior["remote_state"] if reverse else None},
                    {"id": "binding", "change": "atomically rebind default, flow and scoped data fixes"},
                    {"id": "prs", "manual": True,
                     "commands": [f"gh pr edit {pr['number']} --repo {full_name} --base {default}"
                                  for pr in facts["open_prs"]]},
                    {"id": "resume", "change": f"resume and verify root delivery to {default}"},
                ], "prior": prior}

    async def run(self, project_id, flow, *, expected_generation=None, dry_run=True,
                  allow_epics=False, reverse=False, operator_id, baseline=None):
        if not dry_run and baseline is None:
            raise ValueError("apply requires the saved cutover plan; preview again")
        plan = await self.plan(project_id, flow, allow_epics=allow_epics, reverse=reverse)
        if plan["blockers"]:
            return {"outcome": "blocked", "plan": plan, "blockers": plan["blockers"]}
        if dry_run:
            return {"outcome": "preview", "plan": plan, "dry_run": True}
        if expected_generation is None:
            raise ValueError("apply requires the generation returned by cutover-plan")
        state = baseline.get("remote_state")
        if (not isinstance(state, dict)
                or not reverse and state.get("default_branch") != plan["old_default"]
                or not isinstance(state.get("rulesets"), list)
                or not isinstance(state.get("protection"), dict)):
            raise ValueError("saved_plan_invalid: capture GitHub state before manual changes")
        for key in ("project_id", "repository_id", "generation", "old_default", "default_branch",
                    "flow", "cut_oid", "workflow_branches", "binding", "aborted_members",
                    "aborted_member_fence", "reverse", "allow_epics", "receipt_ids",
                    "receipt_targets", "origin_ids"):
            if baseline.get(key) != plan[key]:
                raise ValueError(f"plan changed; preview again: {key}")
        plan["baseline"] = baseline
        repository = await self.db.get_repo(plan["repository_id"])
        result = await PolicyActivation(self.db, clock=self.clock).configure(
            project_id, updates={"integration_repository": {
                "id": repository.id, "url": repository.url,
                "default_branch": plan["default_branch"]}, "promotion_flow": plan["flow"]},
            expected_generation=expected_generation, reason="reverse cutover" if reverse else "cutover",
            operator_id=operator_id, promotion_manifest=plan["manifest"],
            cutover=_Activation(self, plan, operator_id))
        if result["outcome"] == "configured" and plan["receipt_ids"]:
            async with self.db._engine.connect() as conn:
                enabled = await conn.scalar(text(
                    "SELECT tgenabled::text FROM pg_trigger WHERE tgrelid = "
                    "'task_delivery_receipts'::regclass AND "
                    "tgname = 'trg_task_delivery_receipts_update'"))
            if enabled != "O":
                raise RuntimeError("cutover committed but receipt update guard is not enabled")
        return {**result, "plan": plan, "dry_run": False}


class _Activation:
    """Private hook; ordinary PolicyActivation callers always keep the strict barrier."""

    def __init__(self, service, plan, operator):
        self.service, self.plan, self.operator = service, plan, operator
        self.default_branch, self.allow_epics = plan["default_branch"], plan["allow_epics"]

    async def check_on(self, conn, project):
        plan, db = self.plan, self.service.db
        repository = (await conn.execute(select(repos).where(
            repos.c.id == plan["repository_id"]).with_for_update())).mappings().one_or_none()
        if (repository is None or project["integration_repository_id"] != plan["repository_id"]
                or project["repo_default_branch"] != plan["binding"]["project_default"]
                or project["promotion_flow"] != plan["binding"]["old_flow"]
                or project["default_branch_cutover"] != plan["binding"]["cutover"]
                or repository["default_branch"] != plan["old_default"]
                or repository["url"] != plan["binding"]["repository_url"]):
            return {"outcome": "refused", "error": "plan changed; preview again: binding"}
        # Match batch admission and ref publication locks before re-reading authority.
        for branch in sorted({plan["old_default"], self.default_branch,
                              *flow_targets(plan["flow"]), *flow_targets(project["promotion_flow"])}):
            await BatchStore._target_lock(conn, Batch(
                "cutover", project["id"], plan["repository_id"], "refs/heads/" + branch))
            await BranchLock(db).lock_on(conn, BranchKey(
                repository_id=plan["repository_id"], branch=branch))
        live = await inventory_on(conn, project["id"], plan["repository_id"], now=self.service.clock())
        await conn.execute(select(tasks.c.id).where(tasks.c.id.in_(
            [row["task_id"] for row in plan["aborted_member_fence"]])).order_by(
                tasks.c.id).with_for_update())
        aborted = await aborted_members_on(conn, project["id"], plan["repository_id"],
                                           plan["old_default"])
        if aborted != plan["aborted_member_fence"]:
            return {"outcome": "refused", "error": "plan changed; preview again: aborted members"}
        receipts = await receipt_targets_on(conn, plan["repository_id"], plan["old_default"],
            prior=plan["prior"]["receipt_ids"] if plan["reverse"] else None)
        if receipts != plan["receipt_targets"]:
            return {"outcome": "refused", "error": "plan changed; preview again: receipts"}
        if await origin_ids_on(conn, project["id"], plan["repository_id"],
                plan["prior"]["old_default"] if plan["reverse"] else plan["old_default"],
                prior=plan["prior"]["origin_ids"] if plan["reverse"] else None) != plan["origin_ids"]:
            return {"outcome": "refused", "error": "plan changed; preview again: root origins"}
        enabled = await conn.scalar(text(
            "SELECT tgenabled::text FROM pg_trigger WHERE tgrelid = 'task_delivery_receipts'::regclass "
            "AND tgname = 'trg_task_delivery_receipts_update'"))
        if enabled != "O":
            return {"outcome": "refused", "error": "receipt update guard is not enabled"}
        blockers = barrier(live, allow_epics=self.allow_epics,
                           targets=set(flow_targets(plan["flow"])) | set(
                               flow_targets(project["promotion_flow"])), project_id=project["id"])
        if blockers:
            return {"outcome": "blocked", "error": "cutover barrier is busy", "blockers": blockers}
        return None

    async def prepare(self):
        await self.service.git.prepare(self.default_branch, reverse=self.plan["reverse"])
        await self.service.git.prepare_flow(self.plan["flow"])
        project = await self.service.db.get_project(self.plan["project_id"])
        await self.service.git.verify(self.plan, policy=project.hierarchical_integration_policy)

    async def verify_on(self, conn, generation):
        row = (await conn.execute(select(
            projects.c.repo_default_branch, projects.c.promotion_flow,
            projects.c.hierarchical_integration_generation, repos.c.default_branch,
        ).join(repos, repos.c.id == projects.c.integration_repository_id).where(
            projects.c.id == self.plan["project_id"], repos.c.id == self.plan["repository_id"],
        ))).one()
        if tuple(row) != (self.default_branch, self.plan["flow"], generation, self.default_branch):
            raise RuntimeError("cutover bindings disagree; transaction rolled back")

    async def write_on(self, conn, project):
        plan, old, new = self.plan, self.plan["old_default"], self.default_branch
        await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        try:
            await conn.execute(text("LOCK TABLE task_delivery_receipts IN ACCESS EXCLUSIVE MODE"))
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            raise ValueError("cutover_lock_timeout: receipt table is busy; retry cutover") from exc
        receipts = await receipt_targets_on(conn, plan["repository_id"], old,
            prior=plan["prior"]["receipt_ids"] if plan["reverse"] else None)
        receipt_ids = list(receipts)
        origin_ids = await origin_ids_on(conn, project["id"], plan["repository_id"],
            plan["prior"]["old_default"] if plan["reverse"] else old,
            prior=plan["prior"]["origin_ids"] if plan["reverse"] else None, lock=True)
        if receipts != plan["receipt_targets"] or origin_ids != plan["origin_ids"]:
            raise ValueError("plan changed; preview again: receipts or root origins")
        if plan["reverse"] and set(receipt_ids) != set(plan["prior"]["receipt_ids"]):
            raise ValueError("cutover_data_changed: inspect audited rows before reversing")
        if receipt_ids:
            # Suspend only the receipt update guard under the table lock.
            # Transaction rollback restores both trigger DDL and rows on cancellation/failure.
            await conn.execute(text("ALTER TABLE task_delivery_receipts "
                                    "DISABLE TRIGGER trg_task_delivery_receipts_update"))
            changes = {}
            for receipt_id, original in receipts.items():
                target = (plan["prior"]["receipt_targets"][receipt_id] if plan["reverse"] else
                          ("refs/heads/" if original.startswith("refs/heads/") else "") + new)
                changes.setdefault((original, target), []).append(receipt_id)
            for (original, target), ids in changes.items():
                changed = await conn.execute(update(task_delivery_receipts).where(
                    task_delivery_receipts.c.id.in_(ids),
                    task_delivery_receipts.c.repository_id == plan["repository_id"],
                    task_delivery_receipts.c.target_branch == original).values(target_branch=target))
                if changed.rowcount != len(ids):
                    raise ValueError("plan changed; preview again: receipts")
            await conn.execute(text("ALTER TABLE task_delivery_receipts "
                                    "ENABLE TRIGGER trg_task_delivery_receipts_update"))
        cutover_at = self.service.clock()
        await conn.execute(update(projects).where(projects.c.id == project["id"]).values(
            default_branch_cutover=None if plan["reverse"] else {
                "repository_id": plan["repository_id"], "old_default": old, "new_default": new,
                "generation": project["hierarchical_integration_generation"] + 1,
                "cutover_at": cutover_at}))
        await conn.execute(insert(events).values(
            event_type="integration.cutover", project_id=project["id"], timestamp=cutover_at,
            payload=json.dumps({"repository_id": plan["repository_id"], "reverse": plan["reverse"],
                                "old_default": old, "old_flow": project["promotion_flow"],
                                "new_default": new, "new_flow": plan["flow"],
                                "receipt_ids": receipt_ids, "receipt_targets": receipts,
                                "origin_ids": origin_ids, "cutover_at": cutover_at,
                                "workflow_branches": plan.get("baseline", plan)["workflow_branches"],
                                "remote_state": plan.get("baseline", plan)["remote_state"],
                                "operator": self.operator})))
