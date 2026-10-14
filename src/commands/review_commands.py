"""Document-review command surface and command-layer authorization."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from src.task_templates import render_task_template
from src.api.task_attachments import MAX_ATTACHMENT_BYTES, _ALLOWED_TYPES, _verify_image
from src.api.auth import RequestScope
from src.api.scope import held_task_for_session

from src.commands.principal import TRUSTED_LOCAL, PrincipalKind, current_principal
from src.api.auth import operator_viewer_allowed, request_surface
from src.database.queries.pull_request_queries import (
    list_known_pull_requests, read_pull_request_snapshot,
)
from src.git.github import GitHubAccess, GitHubClient
from src.git.github_contracts import GitHubAccessError
from src.models import TaskStatus
from src.reviews.service import PlaybookPin, ReviewError, ReviewHooks, ReviewService

logger = logging.getLogger(__name__)

_LIVE = frozenset({"starting", "running", "draining"})


def _error(code: str, message: str) -> dict[str, Any]:
    """Return the stable review-command refusal envelope."""
    return {"success": False, "error_code": code, "error": message}


#: ``aq review dispatch`` defaults (mandatory-routing spec §5.3): one reviewer
#: on a deep-high class hint.  The router picks the profile, and the
#: dispatch's ``exclude_providers`` constraint keeps it off the author's family.
DISPATCH_DEFAULT_CLASS = "deep-high"
DISPATCH_MAX_COUNT = 10
#: The ``created_by_kind`` of a dispatched reviewer task: the routing policy's
#: ``origins.review_dispatch`` rule matches it.
REVIEW_DISPATCH_ORIGIN = "review_dispatch"


#: The ``review_submit`` arguments that attach a playbook to a revision.
_PLAYBOOK_ARGS = ("playbook_id", "semantic_body", "semantic_body_path", "activate_on_approval")


def _activate_command(playbook: dict) -> str:
    return (
        f"aq playbook activate --playbook-id {playbook['playbook_id']} "
        f"--artifact-sha256 {playbook['artifact_sha256']}"
    )


def playbook_outcome_text(outcome: dict) -> str:
    """One paragraph for the supervisor: what approval did with the artifact."""
    playbook_id, sha = outcome["playbook_id"], outcome["artifact_sha256"]
    if not outcome["stored"]:
        return (
            f"Playbook {playbook_id}: the approved artifact {sha} could not be stored: "
            f"{outcome['error']}. Nothing was activated. Recompile it and submit a new "
            "revision, or store it once the cause is fixed with "
            "`aq doctor --check reviews.playbook_artifacts --fix`."
        )
    if outcome["activated"]:
        return f"Playbook {playbook_id}: the approved artifact {sha} is stored and activated."
    lines = [f"Playbook {playbook_id}: the approved artifact {sha} is stored, not activated."]
    if outcome.get("activation_error") or outcome.get("activation_blockers"):
        why = outcome.get("activation_error") or "; ".join(outcome["activation_blockers"])
        lines.append(f"The review asked for activation, which was refused: {why}.")
    lines.append(f"Activate it with: {outcome['next_step']}")
    return " ".join(lines)


class ReviewCommandsMixin:
    """``review_*`` handlers, while :mod:`src.reviews.service` owns state."""

    async def _cmd_approve_pull_request(self, args: dict) -> dict:
        """Approve a pinned PR using the host's human gh login, never the App."""
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL or not operator_viewer_allowed():
            return _error("unauthorized", "Only the local operator may approve pull requests")
        task_id = args.get("task_id")
        sha = args.get("head_sha")
        if (not isinstance(task_id, str) or not isinstance(sha, str)
                or re.fullmatch(r"[a-fA-F0-9]{40}", sha) is None):
            return _error("invalid", "A task and 40-character head SHA are required")
        links = [row for row in await list_known_pull_requests(self.db)
                 if row["task_id"] == task_id]
        if len(links) != 1:
            return _error("not_found", "Task has no unique active-project pull request")
        row = links[0]
        if not row["repository_url"]:
            return _error("invalid", "Task has no bound GitHub repository")
        snapshot, _ = await read_pull_request_snapshot(self.db)
        if not any(item.get("task_id") == task_id and item.get("url") == row["pr_url"]
                   and item.get("head_sha") == sha for item in snapshot):
            return _error("stale_head", "PR is absent from the current snapshot or head moved")

        # The daemon may use a GitHub App for all other operations.  Remove
        # ambient token overrides so this separate client reads gh's saved
        # login on the operator machine, and therefore submits as a User.
        environment = {key: value for key, value in os.environ.items() if key not in {
            "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
            "GH_CONFIG_DIR",
        }}
        access = GitHubAccess.from_config(None, env=environment)
        try:
            identity_result = await access.runner.run(
                ["api", "user"], hostname="github.com", max_stdout_bytes=65536,
            )
            operator = json.loads(identity_result.stdout)
            if (not isinstance(operator, dict) or operator.get("type") != "User"
                    or not isinstance(operator.get("login"), str)
                    or not operator["login"]):
                return _error("credentials", "The saved gh login is not a human GitHub user")
            binding = await access.bind_repository(row["repository_url"])
            number = GitHubAccess.validate_pr_url(binding, row["pr_url"])
            client = GitHubClient(binding, access=access)
            pull = await client.pull_request(row["pr_url"])
            head = pull.get("head")
            if pull.get("state") != "open" or not isinstance(head, dict) or head.get("sha") != sha:
                return _error("stale_head", "PR head moved or the PR closed")
            review = await client.request_json(
                "POST", f"repos/{binding.full_name}/pulls/{number}/reviews",
                json_body={"event": "APPROVE", "commit_id": sha},
                expected_statuses={200, 201},
            )
            if (review.get("state") != "APPROVED" or review.get("commit_id") != sha
                    or not isinstance(review.get("user"), dict)
                    or review["user"].get("type") != "User"
                    or str(review["user"].get("login", "")).lower()
                    != operator["login"].lower()):
                return _error("unexpected_response", "GitHub did not confirm a human head-pinned approval")
        except (ValueError, TypeError):
            return _error("credentials", "Could not read the saved gh user identity")
        except GitHubAccessError as exc:
            return _error(exc.category, str(exc))

        flush = None
        if row["integration_mode"] == "train":
            try:
                flush = await self.execute("integration_flush", {"project_id": row["project_id"]})
            except Exception:
                logger.exception("Could not request train flush after PR approval")
                flush = {"success": False, "error": "Integration flush failed"}
        return {"success": True, "task_id": task_id, "url": row["pr_url"],
                "head_sha": sha, "review_id": review.get("id"), "integration_flush": flush}

    def _review_service(self) -> ReviewService:
        return ReviewService(
            self.db,
            self.config.vault_root,
            ReviewHooks(
                emit=self._emit_review_application_event,
                resolve_gate=lambda gate_id, by, resolution: self.orchestrator._resolve_gate_and_emit(
                    gate_id, resolved_by=by, resolution=resolution
                ),
                changes_requested=self._on_review_changes_requested,
                file_feedback=self._file_review_feedback,
                record_playbook=self._record_review_decision_playbook,
            ),
            input_config=self.config,
        )

    async def _review_attachment_access(
        self, review: dict, revision: int, *, write: bool
    ) -> dict | None:
        """Authorize the held author/revision task, or a pinned dispatch read."""
        if not self._is_worker():
            return None
        scope = self._current_scope or {}
        held = await held_task_for_session(self.db, RequestScope(
            kind="session",
            session_id=scope.get("session_id"),
            session_instance_token=scope.get("session_instance_token"),
            task_id=scope.get("task_id"),
            project_id=scope.get("project_id"),
        ))
        if held is None:
            return _error("not_your_task", "a live held task is required")
        if held.project_id != review["project_id"]:
            return _error("not_your_task", "review belongs to another project")
        revision_row = await self.db.get_review_revision(review["id"], revision)
        if revision_row is None:
            return _error("revision_not_found", "review revision was not found")
        if held.id in (review["author_task_id"], revision_row.get("submitted_task_id")):
            return None
        dispatch = await self.db.get_review_dispatch_for_task(held.id)
        if not write and dispatch and (
            dispatch["review_id"] == review["id"] and dispatch["revision"] == revision
        ):
            return None
        return _error("not_dispatched", "this task cannot access that review packet")

    async def _cmd_review_attachment_add(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        revision = args.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return _error("bad_revision", "revision must be a positive integer")
        if error := await self._review_attachment_access(review, revision, write=True):
            return error
        claimed_type = args.get("content_type")
        if claimed_type not in _ALLOWED_TYPES:
            return _error("bad_image", "only PNG, JPEG, GIF, and WebP are allowed")
        fields = {}
        for field, limit in (("caption", 500), ("view_id", 120), ("candidate_id", 120)):
            value = args.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return _error("bad_metadata", f"{field} must have 1 to {limit} characters")
            if field != "caption" and not re.fullmatch(r"[A-Za-z0-9._:/-]+", value):
                return _error("bad_metadata", f"{field} must be a stable identifier")
            if any(ord(char) < 32 or ord(char) == 127 for char in value):
                return _error("bad_metadata", f"{field} cannot contain control characters")
            fields[field] = value.strip()
        encoded = args.get("data_base64")
        if not isinstance(encoded, str) or len(encoded) > ((MAX_ATTACHMENT_BYTES + 2) // 3) * 4:
            return _error("too_large", "image exceeds the 10 MiB cap")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return _error("bad_image", "image must be valid base64")
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            return _error("too_large", "image must be 1 byte to 10 MiB")
        attachment_id = uuid4().hex
        ext = _ALLOWED_TYPES[claimed_type][1]
        directory = Path(self.config.data_dir).expanduser().resolve() / "review-attachments"
        destination = directory / review["id"] / str(revision) / f"{attachment_id}{ext}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("xb") as handle:
                handle.write(data)
            try:
                _verify_image(destination, claimed_type)
            except HTTPException as exc:
                return _error("bad_image", str(exc.detail))
            async with self.db.immediate() as conn:
                locked = await self.db.lock_review_for_mutation(review["id"], conn)
                if (locked is None or locked["state"] != "in_review"
                        or locked["current_revision"] != revision or locked["pending_decision_id"]):
                    return _error("stale_revision", "only the current open review revision accepts images")
                if error := await self._review_attachment_access(review, revision, write=True):
                    return error
                if any(
                    item["revision"] == revision
                    for item in await self.db.list_review_dispatches(review["id"], conn=conn)
                ):
                    return _error("packet_dispatched", "a dispatched review packet cannot gain images")
                # A revision/decision can race the upload. The row lock keeps
                # this append before or after that transition, never between.
                await self.db.insert_review_attachment({
                    "id": attachment_id,
                    "review_id": review["id"],
                    "revision": revision,
                    "path": str(destination),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content_type": claimed_type,
                    "size": len(data),
                    "created_at": time.time(),
                    **fields,
                }, conn=conn)
            return {"success": True, "attachment": {
                "id": attachment_id, "review_id": review["id"], "revision": revision,
                "sha256": hashlib.sha256(data).hexdigest(), "content_type": claimed_type,
                "size": len(data), "url": f"/api/reviews/{review['id']}/revisions/{revision}/attachments/{attachment_id}",
                **fields,
            }}
        finally:
            # The DB row is the durable ownership marker. On a refusal there
            # is no row, so an orphan upload must not survive.
            if await self.db.get_review_attachment(attachment_id) is None:
                destination.unlink(missing_ok=True)

    async def _cmd_review_attachment_list(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        revision = args.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return _error("bad_revision", "revision must be a positive integer")
        if error := await self._review_attachment_access(review, revision, write=False):
            return error
        rows = await self.db.list_review_attachments(review["id"], revision)
        return {"success": True, "attachments": [
            {key: value for key, value in row.items() if key not in {"path", "created_at"}}
            | {"url": f"/api/reviews/{review['id']}/revisions/{revision}/attachments/{row['id']}"}
            for row in rows
        ]}

    async def _emit_review_event(self, event_type: str, payload: dict) -> None:
        """Persist then fan out a review event; neither observer may undo a write."""
        body = dict(payload)
        try:
            body["seq"] = await self.db.log_event(
                event_type,
                project_id=payload.get("project_id"),
                task_id=payload.get("author_task_id") or payload.get("task_id"),
                payload=json.dumps(payload, default=str)[:4000],
            )
        except Exception:
            logger.warning("persisting %s failed", event_type, exc_info=True)
        try:
            await self.orchestrator.bus.emit(event_type, body)
        except Exception:
            logger.warning("emitting %s failed", event_type, exc_info=True)

    async def _review_decider_label(self, review: dict) -> tuple[str | None, dict | None]:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL:
            return "human:local-operator", None
        if (
            principal.kind is PrincipalKind.SESSION
            and principal.elevated
            and review["decider"] in {"user_or_supervisor", "supervisor"}
        ):
            row = await self.db.get_session(principal.session_id)
            if (
                row is not None
                and row.profile_id == "supervisor"
                and row.lifecycle == "named"
                and row.state in _LIVE
                and row.desired_state == "running"
                and row.project_id in (None, review["project_id"])
            ):
                return f"supervisor (delegated) session:{principal.session_id}", None
        why = (
            "this review is not delegated to the supervisor"
            if review["decider"] == "user"
            else "a live supervisor session is required"
        )
        if review["decider"] == "supervisor":
            return None, _error("not_decider", "a live project supervisor must decide this internal review")
        return None, _error("not_decider", f"only Jack (dashboard or local CLI) may decide: {why}")

    async def _review_for_caller(self, review_id: object) -> tuple[dict | None, dict | None]:
        """Load a review and apply the project boundary before revealing it."""
        if not isinstance(review_id, str) or not review_id:
            return None, _error("not_found", "review_id is required")
        review = await self.db.get_review(review_id)
        if review is None:
            return None, _error("not_found", f"review {review_id!r} was not found")
        principal = current_principal() or TRUSTED_LOCAL
        if (
            principal.kind is PrincipalKind.SESSION
            and principal.project_id is not None
            and principal.project_id != review["project_id"]
        ):
            return None, _error("not_your_task", "review belongs to another project")
        return review, None

    async def _worker_held_task(self) -> tuple[object | None, dict | None]:
        """Return the current worker's held task, or the review-specific refusal."""
        held_id = await self._scoped_held_task_id()
        task = await self.db.get_task(held_id) if held_id else None
        if task is None:
            return None, _error("not_your_task", "a worker may submit only from its held task")
        return task, None

    @staticmethod
    def _is_worker() -> bool:
        principal = current_principal() or TRUSTED_LOCAL
        return principal.kind is PrincipalKind.SESSION and not principal.elevated

    async def _submit_project(self, args: dict) -> tuple[str | None, str | None, dict | None]:
        """Resolve a new review's project and optional author task."""
        principal = current_principal() or TRUSTED_LOCAL
        task_id = args.get("task_id")
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return None, None, error
            if task_id != held.id:
                return None, None, _error("not_your_task", "task_id must be the task this worker holds")
            if args.get("project_id") not in (None, held.project_id):
                return None, None, _error("not_your_task", "project belongs to another task")
            return held.project_id, held.id, None
        if task_id is not None:
            task = await self.db.get_task(str(task_id))
            if task is None:
                return None, None, _error("not_found", f"task {task_id!r} was not found")
            if (
                principal.kind is PrincipalKind.SESSION
                and principal.project_id is not None
                and task.project_id != principal.project_id
            ):
                return None, None, _error("not_your_task", "task belongs to another project")
            return task.project_id, task.id, None
        project_id = args.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return None, None, _error("not_found", "project_id is required when task_id is omitted")
        project = await self.db.get_project(project_id)
        if project is None:
            return None, None, _error("not_found", f"project {project_id!r} was not found")
        if (
            principal.kind is PrincipalKind.SESSION
            and principal.project_id is not None
            and project_id != principal.project_id
        ):
            return None, None, _error("not_your_task", "project belongs to another session")
        return project_id, None, None

    async def _cmd_review_submit(self, args: dict) -> dict:
        service = self._review_service()
        principal = current_principal() or TRUSTED_LOCAL
        content = args.get("content")
        if not isinstance(content, str):
            return _error("not_utf8", "content must be UTF-8 text")
        review_id = args.get("review_id")
        held = None
        response = None
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if args.get("task_id") not in (None, held.id):
                return _error("not_your_task", "task_id must be the task this worker holds")
            response = await self.db.get_task_meta(held.id, "review_response")
            if isinstance(response, dict):
                assigned_id = response["review_id"]
                if review_id and review_id != assigned_id:
                    return _error(
                        "not_your_task",
                        f"this revision task must update review {assigned_id}; use "
                        f"aq review submit --review-id {assigned_id} --file <draft.md> "
                        '--changes "<comment dispositions and evidence>"',
                    )
                review_id = assigned_id
        try:
            if review_id:
                review, error = await self._review_for_caller(review_id)
                if error:
                    return error
                from src.object_loop.reviews import experiment_context, result_text_error

                experiment = await experiment_context(self.db, review["author_task_id"])
                if experiment and experiment["final_result"]:
                    if error := await result_text_error(
                        self.db, experiment, content + "\n" + str(args.get("changes") or ""),
                    ):
                        return _error("object_result_language", error)
                submitted_task_id: str | None = None
                if self._is_worker():
                    is_response_task = (
                        isinstance(response, dict)
                        and response.get("review_id") == review["id"]
                        and response.get("revision") == review["current_revision"]
                        and review["state"] in {"changes_requested", "rejected"}
                    )
                    author_draft = (
                        review["state"] == "in_review" and review["author_task_id"] == held.id
                    )
                    if not is_response_task and not author_draft:
                        return _error("not_your_task", "only the assigned revision task may answer requested changes")
                    submitted_task_id = held.id
                previous = await self.db.get_review_revision(
                    review["id"], review["current_revision"]
                )
                playbook, error = await self._review_playbook_pin(args, review["kind"], previous)
                if error:
                    return error
                return {
                    "success": True,
                    **await service.revise(
                        review_id=review["id"],
                        content=content,
                        changes_note=str(args.get("changes") or ""),
                        resolves=list(args.get("resolves") or []),
                        submitted_by=principal.describe(),
                        submitted_task_id=submitted_task_id,
                        playbook=playbook,
                    ),
                    **({"playbook": playbook.meta} if playbook else {}),
                }
            project_id, author_task_id, error = await self._submit_project(args)
            if error:
                return error
            kind = str(args.get("kind") or "")
            if not kind and args.get("playbook_id") is not None:
                kind = "other"
            playbook, error = await self._review_playbook_pin(args, kind, None)
            if error:
                return error
            project = await self.db.get_project(project_id)
            from src.object_loop.reviews import experiment_context, result_text_error

            experiment = await experiment_context(self.db, author_task_id)
            decider = (
                "user_or_supervisor"
                if project is not None and project.review_delegate_to == "supervisor"
                else "user"
            )
            if experiment:
                decider = "user" if experiment["final_result"] else "supervisor"
                if experiment["final_result"]:
                    if kind != "other":
                        return _error("object_result_kind", "Submit the final result as an other review.")
                    if error := await result_text_error(
                        self.db, experiment, str(args.get("title") or "") + "\n" + content,
                    ):
                        return _error("object_result_language", error)
            return {
                "success": True,
                **await service.submit(
                    project_id=project_id,
                    author_task_id=author_task_id,
                    kind=kind,
                    title=str(args.get("title") or ""),
                    content=content,
                    submitted_by=principal.describe(),
                    decider=decider,
                    single_author=bool(experiment and experiment["final_result"]),
                    playbook=playbook,
                ),
                **({"playbook": playbook.meta} if playbook else {}),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _review_playbook_pin(
        self, args: dict, kind: str, previous: dict | None
    ) -> tuple[PlaybookPin | None, dict | None]:
        """Compile the playbook a submission names into its revision's pin.

        A new review pins one only when ``playbook_id`` is given.  A revision
        of a review that already pins one always pins again: it recompiles
        against the current vault source, from the semantic body it was sent
        or, when none was, from the previous artifact's rules and steps.  The
        submission is refused unless the proposal is activatable, so approval
        can only ever name an artifact that validated when it was submitted.
        """
        previous_pin = PlaybookPin.from_revision(previous) if previous else None
        given = {key: args.get(key) for key in _PLAYBOOK_ARGS if args.get(key) is not None}
        if not given and previous_pin is None:
            return None, None
        if previous_pin is None and "playbook_id" not in given:
            return None, _error(
                "playbook_required",
                f"{', '.join(sorted(given))} needs playbook_id: name the playbook the "
                "review is for",
            )
        playbook_id = given.get("playbook_id")
        if previous_pin is not None and playbook_id not in (
            None, previous_pin.meta["playbook_id"]
        ):
            return None, _error(
                "playbook_mismatch",
                f"this review is for playbook {previous_pin.meta['playbook_id']!r}, "
                f"not {playbook_id!r}",
            )
        if previous_pin is not None:
            playbook_id = previous_pin.meta["playbook_id"]
        if not isinstance(playbook_id, str) or not playbook_id.strip():
            return None, _error("playbook_required", "playbook_id must be a playbook id")
        if kind != "other":
            return None, _error(
                "bad_kind",
                "a playbook review is kind 'other' (a spec approval would start spec ingest)",
            )
        activate = given.get(
            "activate_on_approval",
            bool(previous_pin.meta.get("activate_on_approval")) if previous_pin else False,
        )
        if not isinstance(activate, bool):
            return None, _error("playbook_invalid", "activate_on_approval must be a boolean")
        if "semantic_body" in given and "semantic_body_path" in given:
            return None, _error(
                "playbook_invalid", "send semantic_body or semantic_body_path, not both"
            )

        from src.commands.playbook_v2_commands import (
            V2_COMPILER_DISABLED_ERROR,
            _diagnostic_counts,
            _diagnostic_dict,
        )
        from src.playbooks.definition import canonical_bytes
        from src.playbooks.proposal import (
            DuplicateSemanticKey,
            load_semantic_body_json,
            propose,
        )

        if not self._v2_compiler_enabled():
            return None, _error("playbook_unavailable", V2_COMPILER_DISABLED_ERROR)
        source, source_error = self._v2_find_source(playbook_id)
        if source_error:
            return None, _error("playbook_unavailable", source_error)
        if "semantic_body" in given:
            text = given["semantic_body"]
            if not isinstance(text, str):
                return None, _error("playbook_invalid", "semantic_body must be JSON text")
        elif "semantic_body_path" in given:
            path, path_error = self._v2_resolve_vault_path(
                given["semantic_body_path"], "semantic_body_path"
            )
            if path_error:
                return None, _error("playbook_unavailable", path_error)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return None, _error("playbook_unavailable", f"semantic body is unreadable: {exc}")
        elif previous_pin is not None:
            try:
                prior = json.loads(previous_pin.artifact)
                text = json.dumps({"rules": prior["rules"], "steps": prior["steps"]})
            except (ValueError, KeyError, TypeError) as exc:
                return None, _error(
                    "playbook_invalid", f"the previous artifact has no rules/steps to reuse: {exc}"
                )
        else:
            return None, _error(
                "playbook_required",
                "a playbook review needs the semantic body to compile: semantic_body "
                "(the CLI's --playbook-body file) or semantic_body_path (in the vault)",
            )
        try:
            body = load_semantic_body_json(text)
        except (ValueError, DuplicateSemanticKey, json.JSONDecodeError) as exc:
            return None, _error("playbook_invalid", f"invalid semantic body: {exc}")

        version = source.frontmatter.get("version")
        contracts, profiles, events = await self._v2_lookups()
        proposal = propose(
            source,
            body,
            contracts=contracts,
            profiles=profiles,
            events=events,
            version=version if isinstance(version, int) and version >= 1 else 1,
        )
        diagnostics = [_diagnostic_dict(diagnostic) for diagnostic in proposal.diagnostics]
        counts = _diagnostic_counts(proposal.diagnostics)
        if not proposal.activatable:
            refusal = _error(
                "playbook_invalid",
                f"playbook {playbook_id!r} does not compile to an activatable artifact "
                f"({counts['error']} error(s), {counts['question']} question(s)); fix the "
                "source or the semantic body and submit again",
            )
            refusal["diagnostics"] = diagnostics
            return None, refusal
        artifact = proposal.artifact
        artifact_bytes = canonical_bytes(artifact)
        scope, scope_identifier = self._v2_scope(artifact)
        meta = {
            "playbook_id": artifact.id,
            "artifact_sha256": "sha256:" + hashlib.sha256(artifact_bytes).hexdigest(),
            "source_sha256": artifact.source_hash,
            "source_path": source.vault_path,
            "source_markdown": source.raw,
            "contract_fingerprint": artifact.contract_fingerprint(),
            "scope": scope,
            "scope_identifier": scope_identifier or None,
            "version": artifact.version,
            "activate_on_approval": activate,
            "counts": counts,
        }
        return PlaybookPin(meta=meta, artifact=artifact_bytes.decode("utf-8")), None

    async def _review_record_playbook(
        self, review: dict, revision: int, decided_by: str
    ) -> dict | None:
        """Store an approved revision's pinned artifact; activate it if asked.

        Runs as a recoverable child of the accepted decision before gate release.
        A failed store or activation retains the pending review; reconciliation
        resumes from the pinned artifact and activation receipts.
        """
        row = await self.db.get_review_revision(review["id"], revision)
        pin = PlaybookPin.from_revision(row) if row else None
        if pin is None:
            return None
        outcome = {
            "playbook_id": pin.meta["playbook_id"],
            "artifact_sha256": pin.meta["artifact_sha256"],
            "activate_on_approval": bool(pin.meta.get("activate_on_approval")),
            "stored": False,
            "activated": False,
            "error": None,
            "next_step": _activate_command(pin.meta),
        }
        try:
            stored = await self._review_store_playbook(review, revision, pin, decided_by)
            if not stored.get("success"):
                outcome["error"] = stored.get("error") or "artifact could not be stored"
                if stored.get("diagnostics"):
                    outcome["diagnostics"] = stored["diagnostics"]
                return outcome
            outcome["stored"] = True
            if not outcome["activate_on_approval"]:
                return outcome
            activated = await self._cmd_playbook_activate(
                {"playbook_id": outcome["playbook_id"], "artifact_sha256": outcome["artifact_sha256"]}
            )
            if activated.get("error"):
                outcome["activation_error"] = activated["error"]
            elif activated.get("blocked"):
                outcome["activation_blockers"] = list(activated.get("blockers") or [])
            else:
                outcome["activated"] = True
                outcome["next_step"] = None
        except Exception as exc:
            logger.exception("review %s: recording the approved playbook failed", review["id"])
            if outcome["stored"]:
                outcome["activation_error"] = f"activating the artifact failed: {exc}"
            else:
                outcome["error"] = outcome["error"] or f"storing the artifact failed: {exc}"
        return outcome

    async def _review_store_playbook(
        self, review: dict, revision: int, pin: PlaybookPin, decided_by: str | None = None
    ) -> dict:
        """Store exactly the pinned bytes, after checking they are what was pinned."""
        from src.commands.playbook_v2_commands import V2_API_DISABLED_ERROR
        from src.playbooks.definition import canonical_bytes, load_definition_json

        if not self._v2_api_enabled():
            return {"success": False, "error": V2_API_DISABLED_ERROR}
        artifact_bytes = pin.artifact.encode("utf-8")
        try:
            definition = load_definition_json(pin.artifact)
        except ValueError as exc:  # pydantic's ValidationError and JSON errors included
            return {"success": False, "error": f"the pinned artifact is invalid: {exc}"}
        sha = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
        if canonical_bytes(definition) != artifact_bytes or sha != pin.meta["artifact_sha256"]:
            return {
                "success": False,
                "error": "the pinned artifact bytes do not match the pinned artifact_sha256",
            }
        return await self._v2_store_artifact(
            definition,
            artifact_bytes,
            source=pin.meta.get("source_markdown"),
            provenance={
                "review": {
                    "review_id": review["id"],
                    "revision": revision,
                    "decided_by": decided_by or review.get("decided_by"),
                }
            },
        )

    async def _cmd_review_show(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        dispatch = None
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            dispatch = await self.db.get_review_dispatch_for_task(held.id)
            if dispatch and (
                dispatch["review_id"] != review["id"]
                or args.get("revision", dispatch["revision"]) != dispatch["revision"]
                or args.get("diff_from") is not None
            ):
                return _error("not_dispatched", "dispatched reviewer may read only its pinned packet")
            if dispatch:
                if error := await self._review_attachment_access(
                    review, dispatch["revision"], write=False
                ):
                    return error
                args = {**args, "revision": dispatch["revision"]}
        try:
            shown = await self._review_service().show(
                review_id=review["id"],
                revision=args.get("revision"),
                comments=bool(args.get("comments", False)),
                diff_from=args.get("diff_from"),
            )
            if dispatch:
                shown["revisions"] = [
                    row for row in shown["revisions"]
                    if row["revision"] == dispatch["revision"]
                ]
                shown["dispatches"] = [
                    row for row in shown["dispatches"]
                    if row["task_id"] == held.id
                ]
                if dispatch["with_comments"] and "comments" in shown:
                    shown["comments"] = [
                        row for row in shown["comments"]
                        if row["revision"] == dispatch["revision"]
                    ]
                else:
                    shown.pop("comments", None)
            current = next(
                row for row in shown["revisions"]
                if row["revision"] == (dispatch["revision"] if dispatch else review["current_revision"])
            )
            viewed_revision = shown["revision"]["revision"]
            attachment_access = await self._review_attachment_access(
                review, viewed_revision, write=False
            )
            attachments = []
            if attachment_access is None:
                attachments = (await self._cmd_review_attachment_list({
                    "review_id": review["id"], "revision": viewed_revision,
                }))["attachments"]
            if not self._is_worker():
                from dataclasses import asdict
                from src.asks.models import SourceIdentity
                from src.asks.service import version_of
                async with self.db.immediate() as conn:
                    publication = await self._ask_service().publish(
                        SourceIdentity(review["project_id"], "review", review["id"], {"id": review["id"]}),
                        conn=conn,
                    )
                    shown["ask"] = {"id": publication.ask["id"],
                                    "expected": asdict(version_of(publication.ask))}
            return {
                "success": True,
                **shown,
                "attachments": attachments,
                "response_route": await self._review_response_route(review, current),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _review_response_route(self, review: dict, revision: dict) -> dict:
        """Preview the revision task route and the approval recipient.

        A revision task is filed unrouted with the decision's class as its
        hint, and the project's router picks the profile (mandatory-routing
        spec §5.3); so the preview names the hint, never a profile.
        """
        selected_class = revision.get("responder_class")

        def describe(class_id: str | None) -> str:
            hint = f"class hint {class_id}" if class_id else "no class hint"
            return (
                f"Request changes → new revision task, routed by the project's router "
                f"({hint}); Approve → sent to the supervisor."
            )

        from src.intelligence_classes import load_intelligence_classes

        return {
            "kind": "new_task",
            "summary": describe(selected_class),
            "class_id": selected_class,
            "profile_id": None,
            "source": "router",
            "selected_class": selected_class,
            "selected_profile": None,
            "class_summaries": {
                class_id: describe(class_id)
                for class_id in load_intelligence_classes(self.config.data_dir)
            },
        }

    async def _cmd_review_list(self, args: dict) -> dict:
        principal = current_principal() or TRUSTED_LOCAL
        project_id = args.get("project_id")
        task_id = args.get("task_id")
        if principal.kind is PrincipalKind.SESSION and principal.project_id is not None:
            if project_id not in (None, principal.project_id):
                return _error("not_your_task", "project belongs to another session")
            project_id = principal.project_id
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if task_id not in (None, held.id):
                return _error("not_your_task", "task_id must be the task this worker holds")
            task_id = held.id
        return {
            "success": True,
            "reviews": await self.db.list_reviews(
                project_id=project_id,
                state=args.get("state"),
                kind=args.get("kind"),
                task_id=task_id,
            ),
        }

    async def _cmd_review_withdraw(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        by_author = self._is_worker()
        if by_author:
            held, error = await self._worker_held_task()
            if error:
                return error
            if review["author_task_id"] != held.id:
                return _error("not_decider", "only the author task may withdraw this review")
        principal = current_principal() or TRUSTED_LOCAL
        by = (
            "human:local-operator"
            if principal.kind is PrincipalKind.LOCAL
            else principal.describe()
        )
        reason = str(args.get("reason") or "").strip()
        try:
            result = await self._review_service().withdraw(
                review_id=review["id"], reason=reason, by=by,
                via=request_surface() if principal.kind is PrincipalKind.LOCAL else "agent",
            )
        except ReviewError as error:
            return _error(error.code, error.message)
        if not by_author:
            await self._tell_author_review_withdrawn(review, reason, by)
        # Authors can already be archived, and a task comment alone never
        # reaches the project supervisor. Queue a durable recovery notice.
        try:
            notice = await self._cmd_message_send({
                "project_id": review["project_id"], "to_kind": "session",
                "to_id": f"supervisor-{review['project_id']}", "from_kind": "system",
                "from_id": f"review:{review['id']}",
                "subject": f"Review {review['id']} withdrawn: dependent work needs attention",
                "body": (
                    f"Review {review['id']} ({review['title']}) withdrawn by {by}. "
                    f"Reason: {reason or '(none given)'}. Gate {review['gate_id']} cancelled; "
                    "approval is still required. Dependent tasks: "
                    f"{', '.join(result['flagged_task_ids']) or '(none)'}. "
                    f"Reopen with aq review reopen --review-id {review['id']} "
                    f"--revision {review['current_revision']}, or decide the dependent tasks' fate."
                ),
            })
            if notice.get("success") is False or "error" in notice:
                logger.error("review %s: withdrawal notice failed: %s", review["id"], notice)
        except Exception:
            logger.exception("review %s: withdrawal notice failed", review["id"])
        return {"success": True, **result}

    async def _cmd_review_reopen(self, args: dict) -> dict:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is PrincipalKind.LOCAL and not operator_viewer_allowed():
            return _error("unauthorized", "only the local operator may reopen from the dashboard")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        submitted_task_id = None
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if review["author_task_id"] != held.id:
                return _error("not_your_task", "only the author task may reopen this review")
            submitted_task_id = held.id
        revision = args.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            return _error("bad_revision", "revision must be a positive integer")
        by = "human:local-operator" if principal.kind is PrincipalKind.LOCAL else principal.describe()
        try:
            result = await self._review_service().reopen(
                review_id=review["id"], revision=revision, by=by,
                submitted_task_id=submitted_task_id,
            )
            return {"success": True, **result}
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _tell_author_review_withdrawn(self, review: dict, reason: str, by: str) -> None:
        """Comment on the authoring task, which also wakes its live session.

        The withdrawal is already committed, so a failure here is logged and
        never reported as a failed withdrawal.
        """
        author_id = review.get("author_task_id")
        if not author_id or await self.db.get_task(author_id) is None:
            return
        body = "\n".join([
            f"Review {review['id']} ({review['title']}) was withdrawn by {by}.",
            f"Reason: {reason or '(none given)'}",
            (
                "Its gate is cancelled; approval is still required for dependent work. "
                "Waiting tasks are flagged needs_attention=review_withdrawn. "
                f"Reopen with `aq review reopen --review-id {review['id']} "
                f"--revision {review['current_revision']}` to retain the dependent links."
            ),
        ])
        try:
            result = await self._cmd_task_comment({"task_id": author_id, "body": body})
        except Exception:
            logger.warning(
                "review %s: telling author task %s about the withdrawal failed",
                review["id"], author_id, exc_info=True,
            )
            return
        if "error" in result:
            logger.warning(
                "review %s: telling author task %s about the withdrawal failed: %s",
                review["id"], author_id, result["error"],
            )

    async def _cmd_review_decide(self, args: dict) -> dict:
        from src.asks.models import AskError
        try:
            args = self._ask_decision_args(args)
        except AskError as exc:
            return _error(exc.code, str(exc))
        if set(args) - {"review_id", "revision", "decision", "note", "responder_class",
                        "responder_profile", "preview_id", "expected", "idempotency_key", "dispositions"}:
            return _error("ask.not_decider", "Unexpected decision fields; authority is server-owned")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        label, error = await self._review_decider_label(review)
        if error:
            return error
        decision = args.get("decision")
        if decision not in {"approve", "request_changes", "reject", "fold_in", "decline"}:
            return _error("not_in_review", "decision must be approve, request_changes or reject")
        responder_class = args.get("responder_class")
        if args.get("responder_profile") is not None:
            # The revision task is routed by the project's router; a decision
            # may give it a class hint, never a profile (mandatory-routing §5.3).
            return _error(
                "routing.choice_forbidden",
                "responder_profile is not accepted: the revision task is routed by the "
                "project's router; pass responder_class as its class hint",
            )
        if decision in {"request_changes", "reject"} and responder_class is None:
            from src.commands.github_issue_commands import INVESTIGATION_KEY

            author = await self.db.get_task(review.get("author_task_id")) if review.get("author_task_id") else None
            if author is not None and (author.dedup_key or "").startswith(INVESTIGATION_KEY):
                responder_class = "standard-high"
        if decision == "approve" and responder_class is not None:
            return _error("invalid_responder", "responder routing applies only to feedback decisions")
        if responder_class is not None:
            from src.intelligence_classes import load_intelligence_classes

            classes = load_intelligence_classes(self.config.data_dir)
            available = f"available classes: {', '.join(sorted(classes)) or '(none)'}"
            if not isinstance(responder_class, str) or responder_class not in classes:
                return _error(
                    "invalid_responder_class",
                    f"intelligence class {responder_class!r} not found in vault; {available}",
                )
            if class_error := self._validate_routing_class(responder_class):
                return _error(
                    "invalid_responder_class",
                    f"{class_error}; {available}",
                )
        try:
            result = await self._review_service().decide(
                    review_id=review["id"],
                    revision=args.get("revision"),
                    decision=decision,
                    note=str(args.get("note") or ""),
                    decided_by=label,
                    responder_class=responder_class,
                    responder_profile=None,
                    responder_profile_source="router",
                    preview_id=args.get("preview_id"), expected=args.get("expected"),
                    idempotency_key=args.get("idempotency_key"), dispositions=args.get("dispositions"),
                )
            return {"success": result["application_state"] == "applied", **result}
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_comment(self, args: dict) -> dict:
        if (
            args.get("source_discussion_id")
            and (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL
        ):
            return _error("human_only", "Only a human can promote discussion into formal feedback")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        if self._is_worker():
            dispatch = await self._held_review_dispatch(review["id"])
            if dispatch is None:
                return _error("not_dispatched", "this worker holds no dispatch for this review")
            if args.get("revision") != dispatch["revision"]:
                return _error("wrong_revision", "comment on the revision pinned to this dispatch")
            if not args.get("quote") and not args.get("heading_path"):
                return _error("anchor_required", "a dispatched reviewer must anchor each finding")
            label = f"adversarial reviewer session:{(current_principal() or TRUSTED_LOCAL).session_id}"
        else:
            label, error = await self._review_decider_label(review)
            if error:
                return error
        try:
            return {
                "success": True,
                **await self._review_service().comment(
                    review_id=review["id"],
                    revision=args.get("revision"),
                    quote=args.get("quote"),
                    heading_path=list(args.get("heading_path") or []),
                    body=str(args.get("body") or ""),
                    author=label,
                    source_discussion_id=args.get("source_discussion_id"),
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _held_review_dispatch(self, review_id: str) -> dict | None:
        """A grant bound to the authenticated live session's current task."""
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.SESSION or principal.elevated:
            return None
        session = await self.db.get_session(principal.session_id or "")
        if (
            session is None
            or session.task_id is None
            or session.project_id != principal.project_id
            or session.instance_token != principal.session_instance_token
            or session.lifecycle not in {"task", "pool"}
            or session.state not in {"starting", "running"}
            or session.desired_state != "running"
        ):
            return None
        task = await self.db.get_task(session.task_id)
        if (
            task is None
            or task.project_id != session.project_id
            or task.status is not TaskStatus.IN_PROGRESS
            or (session.agent_id is not None and task.assigned_agent_id != session.agent_id)
            or (
                session.last_claim_epoch is not None
                and task.claim_epoch != session.last_claim_epoch
            )
        ):
            return None
        dispatch = await self.db.get_review_dispatch_for_task(task.id)
        return dispatch if dispatch is not None and dispatch["review_id"] == review_id else None

    async def _review_author_provider(
        self, review: dict, revision: dict | None
    ) -> str | None:
        """The provider the author revision was written on (spec §5.3, D5).

        The observed attempt is the latest session attempt of the task that
        submitted the revision that started no later than the submission (its
        latest attempt otherwise), falling back to the review's author task.
        The answer is the availability key the router's candidates carry
        (``harness.base or harness.id``).  ``None`` when no attempt was
        observed, e.g. a revision the operator submitted by hand.
        """
        from src.providers.availability import provider_key

        task_ids: list[str] = []
        for task_id in ((revision or {}).get("submitted_task_id"), review.get("author_task_id")):
            if task_id and task_id not in task_ids:
                task_ids.append(task_id)
        submitted_at = (revision or {}).get("submitted_at")
        for task_id in task_ids:
            attempts = await self.db.list_task_session_attempts(task_id)
            if not attempts:
                continue
            attempt = next(
                (
                    row for row in attempts
                    if submitted_at is None or (row.get("started_at") or 0) <= submitted_at
                ),
                attempts[0],
            )
            harness = str(attempt.get("harness") or "").strip()
            if not harness:
                continue
            availability = getattr(self.orchestrator, "provider_availability", None)
            if availability is not None:
                return availability.provider_for_harness(harness, review["project_id"]) or harness
            registry = getattr(self.orchestrator, "harness_registry", None)
            resolved = registry.get(harness, review["project_id"]) if registry is not None else None
            return provider_key(resolved if resolved is not None else harness)
        return None

    async def _cmd_review_dispatch(self, args: dict) -> dict:
        """File *count* adversarial reviewer tasks for one review revision.

        Each reviewer is filed unrouted with the ``--class`` hint (default
        ``deep-high``), the origin ``review_dispatch`` and the constraint
        ``exclude_providers: [<author's provider>]``, so the project's router
        picks another model family for it (mandatory-routing spec §5.3, D5).
        A dispatch never names a profile.
        """
        principal = current_principal() or TRUSTED_LOCAL
        if not (
            principal.kind is PrincipalKind.LOCAL
            or (principal.kind is PrincipalKind.SESSION and principal.elevated)
        ):
            return _error("operator_only", "review dispatch requires an operator or supervisor")
        if args.get("to"):
            return _error(
                "routing.choice_forbidden",
                "review dispatch no longer names reviewer profiles: pass --count and --class; "
                "the router picks each reviewer's profile, excluding the author's provider",
            )
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        count = args.get("count", 1)
        if count is None:
            count = 1
        if (
            not isinstance(count, int) or isinstance(count, bool)
            or not 1 <= count <= DISPATCH_MAX_COUNT
        ):
            return _error(
                "invalid_count", f"count must be an integer from 1 to {DISPATCH_MAX_COUNT}"
            )
        class_id = args.get("intelligence_class") or DISPATCH_DEFAULT_CLASS
        if not isinstance(class_id, str) or (class_error := self._validate_routing_class(class_id)):
            return _error("invalid_class", class_error if isinstance(class_id, str) else "class must be a string")

        requested_revision = args.get("revision")
        with_comments = bool(args.get("with_comments", True))
        focus = str(args.get("focus") or "").strip()
        if len(focus) > 4000:
            return _error("focus_too_large", "focus must be at most 4000 characters")
        results = []
        creation_error = None
        async with self.db.immediate() as lock_conn:
            current = await self.db.lock_review_for_dispatch(review["id"], conn=lock_conn)
            if current is None:
                return _error("not_found", f"review {review['id']!r} was not found")
            if current["state"] not in {"in_review", "changes_requested"}:
                return _error("review_closed", f"review {review['id']!r} is {current['state']}")
            revision = current["current_revision"] if requested_revision is None else requested_revision
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                return _error("revision_not_found", f"invalid review revision {revision!r}")
            revision_row = await self.db.get_review_revision(review["id"], revision, conn=lock_conn)
            if revision_row is None:
                return _error("revision_not_found", f"review {review['id']!r} has no revision {revision}")
            existing = await self.db.list_review_dispatches(review["id"], conn=lock_conn)
            prior = [d for d in existing if d["revision"] == revision]
            if prior and not args.get("force"):
                return _error(
                    "duplicate_dispatch",
                    f"review {review['id']} revision {revision} was already dispatched to "
                    f"{len(prior)} reviewer(s); use --force to dispatch more",
                )
            author_provider = await self._review_author_provider(current, revision_row)
            constraints = {"exclude_providers": [author_provider]} if author_provider else None
            for ordinal in range(1, count + 1):
                show_command = f"aq review show --review-id {review['id']} --revision {revision}"
                if with_comments:
                    show_command += " --comments"
                description = (
                    render_task_template(
                        "adversarial-review",
                        current_kind=current["kind"],
                        current_title=current["title"],
                        show_command=show_command,
                        review_id=review["id"],
                        revision=revision,
                    )
                )
                if not with_comments:
                    description += render_task_template("adversarial-review-clean-room")
                if focus:
                    description += f"\nFocus: {focus}\n"
                dispatch = {
                    "id": f"dsp-{uuid.uuid4().hex}",
                    "review_id": review["id"],
                    "profile_id": None,
                    "intelligence_class": class_id,
                    "revision": revision,
                    "with_comments": with_comments,
                    "focus": focus or None,
                    "dispatched_by": principal.describe(),
                    "created_at": time.time(),
                }

                async def record(conn, task_id, _parent_id, *, entry=dispatch):
                    await self.db.insert_review_dispatch({**entry, "task_id": task_id}, conn=conn)
                    await self.db._upsert_meta(
                        task_id,
                        "review_dispatch",
                        {
                            "review_id": entry["review_id"],
                            "revision": entry["revision"],
                            "intelligence_class": entry["intelligence_class"],
                            "with_comments": entry["with_comments"],
                            "focus": entry["focus"],
                        },
                        conn=conn,
                    )

                title = f"Adversarial review: {current['title']}"
                if count > 1:
                    title += f" ({ordinal}/{count})"
                created = await self._cmd_create_task({
                    "project_id": current["project_id"],
                    "title": title,
                    "description": description,
                    "task_type": "research",
                    "intelligence_class": class_id,
                    "root": True,
                    "_after_create_on": record,
                    "_created_by_kind": REVIEW_DISPATCH_ORIGIN,
                    "_created_by_id": review["id"],
                    **({"_route_constraints": constraints} if constraints else {}),
                })
                if not created.get("success"):
                    creation_error = _error(
                        "task_creation_failed", created.get("error", "could not create task")
                    )
                    break
                result = {
                    **dispatch,
                    "task_id": created["task_id"],
                    "task_state": created["status"],
                    "exclude_providers": list((constraints or {}).get("exclude_providers", [])),
                }
                if not getattr(self.config.swarm, "enabled", True):
                    result["capacity_warning"] = "worker pools are disabled"
                results.append(result)
        for result in results:
            await self._emit_review_event(
                "review.dispatched",
                {
                    "review_id": review["id"],
                    "project_id": review["project_id"],
                    "task_id": result["task_id"],
                    "intelligence_class": result["intelligence_class"],
                    "exclude_providers": result["exclude_providers"],
                    "revision": result["revision"],
                    "with_comments": result["with_comments"],
                },
            )
        if creation_error is not None:
            return {**creation_error, "review_id": review["id"], "dispatches": results}
        return {"success": True, "review_id": review["id"], "dispatches": results}

    async def _cmd_review_delegate(self, args: dict) -> dict:
        if (current_principal() or TRUSTED_LOCAL).kind is not PrincipalKind.LOCAL:
            return _error("local_operator_only", "review delegation requires local operator")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        from src.object_loop.reviews import experiment_context

        if await experiment_context(self.db, review["author_task_id"]):
            return _error(
                "object_review_audience",
                "Experiment reviews go to the supervisor; the final result goes to Jack.",
            )
        try:
            return {
                "success": True,
                **await self._review_service().delegate(
                    review_id=review["id"], to=str(args.get("to") or "")
                ),
            }
        except (ReviewError, ValueError) as error:
            return _error(getattr(error, "code", "not_found"), str(error))

    async def _cmd_review_import_edits(self, args: dict) -> dict:
        principal = current_principal() or TRUSTED_LOCAL
        if principal.kind is not PrincipalKind.LOCAL:
            return _error("local_operator_only", "importing review edits requires local operator")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        try:
            return {
                "success": True,
                **await self._review_service().import_edits(review_id=review["id"], by=principal.describe()),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _emit_review_application_event(self, event_type, payload):
        if "seq" not in payload:
            return await self._emit_review_event(event_type, payload)
        # Durable decision operations already persisted their event; propagate failures.
        await self.orchestrator.bus.emit(event_type, payload)

    async def _record_review_decision_playbook(self, decision):
        domain = decision["packet"]["domain"]
        pin = domain["playbook"]
        # Storage and activation have their own exact artifact source receipts.
        stored = await self.db.get_playbook_artifact_row(pin["artifact_sha256"])
        activations = await self.db.list_playbook_activations()
        activated = any(a["playbook_id"] == pin["playbook_id"]
                        and a["enabled"] and a["scope"] == pin["scope"]
                        and (a.get("scope_identifier") or "") == (pin.get("scope_identifier") or "")
                        and a.get("active_artifact_sha256") == pin["artifact_sha256"] for a in activations)
        if stored and (activated or not pin.get("activate_on_approval")):
            return {"stored": True, "activated": activated, "playbook_id": pin["playbook_id"],
                    "scope": pin["scope"], "scope_identifier": pin.get("scope_identifier"),
                    "next_step": _activate_command(pin),
                    "activate_on_approval": bool(pin.get("activate_on_approval")),
                    "artifact_sha256": pin["artifact_sha256"]}
        return await self._review_record_playbook(
            domain["review"], decision["review_revision"], decision["actor_id"],
        )

    async def _file_review_feedback(self, decision, before_create, after_create):
        from src.asks.models import AskError
        from src.asks.operations import task_key

        domain = decision["packet"]["domain"]
        review = domain["review"]
        author_id = review.get("author_task_id")
        author = await self.db.get_task(author_id) if author_id else None
        archived = await self.db.get_archived_task(author_id) if author_id and author is None else None
        parent_id = author.parent_task_id if author else archived.get("parent_task_id") if archived else None
        parent = await self.db.get_task(parent_id) if parent_id else None
        if parent is None or parent.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            parent_id = None
        revision = domain["task_kind"] == "revision"
        lines = [
            f"Frozen review decision {decision['id']} for {review['id']} revision {decision['review_revision']}.",
            f"Packet SHA-256: {decision['packet_sha256']}",
            f"Original author task: {author_id or '(none)'}. Parent fallback: {parent_id or 'project root'}.",
            f"Route: chosen by the project's router (class hint {domain['responder_class']}).",
            "", "Overall note (verbatim):",
            decision["overall_note"], "", "Feedback acceptance items:",
        ]
        for item in domain["feedback"]:
            anchor = " / ".join(item["original_anchor"].get("heading_path") or [])
            if item["original_anchor"].get("quote"):
                anchor += f" — quote: {item['original_anchor']['quote']}"
            lines.extend([f"{item['feedback_key']} ({item['disposition']})",
                          f"Original author: {item['original_author']}; revision {item['original_revision']}",
                          f"Anchor: {anchor}", item["original_text"], ""])
        lines.extend([
            "Return a new revision for final approval; do not autoapprove it." if revision else
                "Complete the accepted follow-ups; approved document bytes remain immutable.",
            f"Use aq review submit --review-id {review['id']} --file <draft.md> --changes <summary>"
                if revision else "Use a new amendment review for changes to approved requirements.",
            "Author --resolves is a proposed response, not decider acceptance.",
            "At passing close, each feedback acceptance row needs its own completion evidence.",
        ])
        if revision:
            lines.append(render_task_template("review-revision", review_id=review["id"]))
        # Keep the historical reject-and-revise issue behavior explicit.
        if domain["effective_option"] == "reject":
            from src.commands.github_issue_commands import INVESTIGATION_KEY

            author_key = author.dedup_key if author else archived.get("dedup_key") if archived else ""
            if (author_key or "").startswith(INVESTIGATION_KEY):
                lines.append(render_task_template("review-rejected-issue", review_id=review["id"]))
            else:
                lines.append(f"Close an associated issue only on an explicit human instruction; "
                             f"use aq github-issue close-rejected --review-id {review['id']}.")
        created = await self._cmd_create_task({
            "project_id": review["project_id"],
            "title": f"{'Revise' if revision else 'Follow up on'} {review['title']} (review {review['id']})",
            "description": "\n".join(lines), "parent_id": parent_id, "root": parent_id is None,
            "dedup_key": task_key(decision), "_created_by_kind": "review_response",
            "_created_by_id": decision["id"],
            **({"intelligence_class": domain["responder_class"]} if domain["responder_class"] else {}),
            "_before_create_on": before_create, "_after_create_on": after_create,
        })
        if not created.get("success"):
            raise AskError("ask.application_unknown", f"Feedback task filing failed: {created}")
        return created

    async def _on_review_changes_requested(self, review, revision, feedback):
        raise ReviewError("ask.stale_source", "Feedback work requires a frozen decision operation")
