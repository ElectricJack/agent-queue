"""Document-review command surface and command-layer authorization."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from src.commands.principal import PrincipalKind, TRUSTED_LOCAL, current_principal
from src.models import TaskStatus
from src.reviews.service import ReviewError, ReviewHooks, ReviewService

logger = logging.getLogger(__name__)

_LIVE = frozenset({"starting", "running", "draining"})


def _error(code: str, message: str) -> dict[str, Any]:
    """Return the stable review-command refusal envelope."""
    return {"success": False, "error_code": code, "error": message}


class ReviewCommandsMixin:
    """``review_*`` handlers, while :mod:`src.reviews.service` owns state."""

    def _review_service(self) -> ReviewService:
        return ReviewService(
            self.db,
            self.config.vault_root,
            ReviewHooks(
                emit=self._emit_review_event,
                resolve_gate=lambda gate_id, by, resolution: self.orchestrator._resolve_gate_and_emit(
                    gate_id, resolved_by=by, resolution=resolution
                ),
                changes_requested=self._on_review_changes_requested,
            ),
        )

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
            and review["decider"] == "user_or_supervisor"
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
        try:
            if review_id:
                review, error = await self._review_for_caller(review_id)
                if error:
                    return error
                submitted_task_id: str | None = None
                if self._is_worker():
                    held, error = await self._worker_held_task()
                    if error:
                        return error
                    response = await self.db.get_task_meta(held.id, "review_response")
                    is_response_task = (
                        isinstance(response, dict)
                        and response.get("review_id") == review["id"]
                        and response.get("revision") == review["current_revision"]
                        and review["state"] == "changes_requested"
                    )
                    author_draft = (
                        review["state"] == "in_review" and review["author_task_id"] == held.id
                    )
                    if not is_response_task and not author_draft:
                        return _error("not_your_task", "only the assigned revision task may answer requested changes")
                    submitted_task_id = held.id
                return {
                    "success": True,
                    **await service.revise(
                        review_id=review["id"],
                        content=content,
                        changes_note=str(args.get("changes") or ""),
                        resolves=list(args.get("resolves") or []),
                        submitted_by=principal.describe(),
                        submitted_task_id=submitted_task_id,
                    ),
                }
            project_id, author_task_id, error = await self._submit_project(args)
            if error:
                return error
            project = await self.db.get_project(project_id)
            return {
                "success": True,
                **await service.submit(
                    project_id=project_id,
                    author_task_id=author_task_id,
                    kind=str(args.get("kind") or ""),
                    title=str(args.get("title") or ""),
                    content=content,
                    submitted_by=principal.describe(),
                    decider=(
                        "user_or_supervisor"
                        if project is not None and project.review_delegate_to == "supervisor"
                        else "user"
                    ),
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_show(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        try:
            shown = await self._review_service().show(
                review_id=review["id"],
                revision=args.get("revision"),
                comments=bool(args.get("comments", False)),
                diff_from=args.get("diff_from"),
            )
            current = next(
                row for row in shown["revisions"]
                if row["revision"] == review["current_revision"]
            )
            return {
                "success": True,
                **shown,
                "response_route": await self._review_response_route(review, current),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _review_response_route(self, review: dict, revision: dict) -> dict:
        """Preview the revision task route and the approval recipient."""
        selected_class = revision.get("responder_class")
        selected_profile = revision.get("responder_profile")
        route = {
            "kind": "new_task",
            "summary": "",
            "class_id": None,
            "profile_id": None,
            "source": None,
            "selected_class": selected_class,
            "selected_profile": selected_profile,
            "class_summaries": {},
        }
        project = await self.db.get_project(review["project_id"])
        implicit, error = await self._supervisor_default_worker_profile(project)
        if selected_profile and (error or implicit is None):
            profile = await self.db.get_profile(selected_profile)
            if profile is not None:
                class_id = selected_class or profile.default_class
                route.update(profile_id=profile.id, class_id=class_id, source="explicit")
                route["summary"] = (
                    f"Request changes → new revision task on {class_id} (explicit); "
                    "Approve → sent to the supervisor."
                )
                return route
        if error or implicit is None:
            route["summary"] = f"Request changes → worker route unavailable: {error or 'missing project'}. Approve → sent to the supervisor."
            return route

        def describe(class_id: str | None, source: str) -> str:
            label = class_id or "class unspecified"
            return (
                f"Request changes → new revision task on {label} ({source}); "
                "Approve → sent to the supervisor."
            )

        route["class_summaries"] = {}
        from src.intelligence_classes import load_intelligence_classes

        for class_id in load_intelligence_classes(self.config.data_dir):
            _, match_error = await self._resolve_class_route(class_id, implicit)
            if match_error:
                continue
            route["class_summaries"][class_id] = describe(class_id, "explicit")

        if selected_profile:
            profile = await self.db.get_profile(selected_profile)
            if profile is None:
                route["summary"] = f"Request changes → selected revision profile {selected_profile} is unavailable. Approve → sent to the supervisor."
                return route
            source = "explicit"
        elif selected_class:
            matched, match_error = await self._resolve_class_route(selected_class, implicit)
            if match_error:
                route["summary"] = f"Request changes → selected revision class {selected_class}: {match_error}. Approve → sent to the supervisor."
                return route
            profile = matched or implicit
            source = "explicit"
        else:
            profile = implicit
            source = "project_default"
        class_id = selected_class or profile.default_class
        route.update(profile_id=profile.id, class_id=class_id, source=source)
        route["summary"] = describe(class_id, source)
        return route

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
        if self._is_worker():
            held, error = await self._worker_held_task()
            if error:
                return error
            if review["author_task_id"] != held.id:
                return _error("not_decider", "only the author task may withdraw this review")
        try:
            return {
                "success": True,
                **await self._review_service().withdraw(
                    review_id=review["id"],
                    reason=str(args.get("reason") or ""),
                    by=(current_principal() or TRUSTED_LOCAL).describe(),
                ),
            }
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_decide(self, args: dict) -> dict:
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        label, error = await self._review_decider_label(review)
        if error:
            return error
        decision = args.get("decision")
        if decision not in {"approve", "request_changes"}:
            return _error("not_in_review", "decision must be approve or request_changes")
        responder_class = args.get("responder_class")
        responder_profile = args.get("responder_profile")
        if decision == "approve" and (responder_class is not None or responder_profile is not None):
            return _error("invalid_responder", "responder routing applies only to request_changes")
        if responder_profile is not None and responder_class is None:
            return _error("invalid_responder", "responder_profile requires responder_class")
        profile_source = "project_default"
        implicit = None
        if decision == "request_changes" and responder_profile is None:
            project = await self.db.get_project(review["project_id"])
            implicit, route_error = await self._supervisor_default_worker_profile(project)
            if route_error:
                return _error("invalid_responder_class", route_error)
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
            if responder_profile is not None:
                profile = await self.db.get_profile(responder_profile)
                eligible = {row.id for row in await self._eligible_worker_profiles()}
                if profile is None or profile.id not in eligible:
                    return _error(
                        "invalid_responder_profile",
                        f"responder profile '{responder_profile}' is not an enabled worker profile",
                    )
                if not self._profile_runs_class(profile, responder_class):
                    return _error(
                        "invalid_responder_profile",
                        f"responder profile '{responder_profile}' runs '{profile.default_class}', "
                        f"not '{responder_class}'",
                    )
                if class_error := self._validate_routing_class(responder_class, profile):
                    return _error("invalid_responder_profile", class_error)
                profile_source = "explicit"
            else:
                routed, route_error = await self._resolve_class_route(responder_class, implicit)
                if route_error:
                    return _error("invalid_responder_class", route_error)
                profile_source = "explicit"
        elif decision == "request_changes" and (
            implicit is None or not implicit.default_class
            or (class_error := self._validate_routing_class(implicit.default_class, implicit))
        ):
            return _error(
                "invalid_responder_class",
                class_error if implicit is not None and implicit.default_class else
                "project default worker profile has no intelligence class",
            )
        try:
            result = await self._review_service().decide(
                    review_id=review["id"],
                    revision=args.get("revision"),
                    approve=decision == "approve",
                    note=str(args.get("note") or ""),
                    decided_by=label,
                    responder_class=responder_class,
                    responder_profile=responder_profile,
                    responder_profile_source=profile_source,
                )
            if decision == "approve":
                notice = await self._cmd_message_send({
                    "project_id": review["project_id"],
                    "to_kind": "session",
                    "to_id": f"supervisor-{review['project_id']}",
                    "from_kind": "system",
                    "from_id": f"review:{review['id']}",
                    "subject": f"Review {review['id']} approved",
                    "body": (
                        f"Review {review['id']} approved: {review['kind']} document "
                        f"{review['vault_path']}."
                    ),
                })
                if not notice.get("success"):
                    logger.error("review %s: supervisor approval notice failed: %s", review["id"], notice)
            return {"success": True, **result}
        except ReviewError as error:
            return _error(error.code, error.message)

    async def _cmd_review_comment(self, args: dict) -> dict:
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

    async def _cmd_review_dispatch(self, args: dict) -> dict:
        principal = current_principal() or TRUSTED_LOCAL
        if not (
            principal.kind is PrincipalKind.LOCAL
            or (principal.kind is PrincipalKind.SESSION and principal.elevated)
        ):
            return _error("operator_only", "review dispatch requires an operator or supervisor")
        review, error = await self._review_for_caller(args.get("review_id"))
        if error:
            return error
        targets = args.get("to") or []
        if isinstance(targets, str):
            targets = [targets]
        if not isinstance(targets, list) or not targets or any(
            not isinstance(target, str) or not target.strip() for target in targets
        ):
            return _error("profile_required", "at least one --to profile is required")
        if len(set(targets)) != len(targets):
            return _error("duplicate_profile", "--to profiles must be distinct in one dispatch")
        profiles = []
        for profile_id in targets:
            profile = await self.db.get_profile(profile_id)
            if profile is None:
                return _error("unknown_profile", f"profile {profile_id!r} was not found")
            if not profile.enabled:
                return _error("disabled_profile", f"profile {profile_id!r} is disabled")
            if reason := self._task_execution_profile_error(profile):
                return _error("invalid_profile", reason)
            if profile.lifecycle not in {"pool", "task"}:
                return _error(
                    "invalid_profile", f"profile {profile_id!r} cannot execute a queued task"
                )
            if profile.aq_commands is not None and "review_show" not in profile.aq_commands:
                return _error(
                    "profile_cannot_review",
                    f"profile {profile_id!r} cannot read reviews (missing review_show grant)",
                )
            profiles.append(profile)

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
            if await self.db.get_review_revision(review["id"], revision, conn=lock_conn) is None:
                return _error("revision_not_found", f"review {review['id']!r} has no revision {revision}")
            existing = await self.db.list_review_dispatches(review["id"], conn=lock_conn)
            if not args.get("force"):
                for profile in profiles:
                    if any(d["profile_id"] == profile.id and d["revision"] == revision for d in existing):
                        return _error(
                            "duplicate_dispatch",
                            f"review {review['id']} revision {revision} was already dispatched to {profile.id}; use --force to repeat",
                        )
            for profile in profiles:
                show_command = f"aq review show --review-id {review['id']} --revision {revision}"
                if with_comments:
                    show_command += " --comments"
                description = (
                    f"ADVERSARIAL review of {current['kind']}: {current['title']}\n\n"
                    f"Read the pinned revision with `{show_command}`.\n"
                    "Find defects, unstated assumptions, missing acceptance criteria, and "
                    "claims unsupported by the code. Verify claims against the repository "
                    "rather than trusting the document. Agreement is allowed, but be specific.\n"
                    "Post each finding with `aq review comment --review-id "
                    f"{review['id']} --revision {revision} --quote ... --body ...` "
                    "or use `--heading-path` to anchor a section.\n"
                    "Close this task with a verdict summary: material defects found, or none. "
                    "Do not decide the review gate.\n"
                )
                if not with_comments:
                    description += "This is a clean-room read; do not request the existing comment thread.\n"
                if focus:
                    description += f"\nFocus: {focus}\n"
                dispatch = {
                    "id": f"dsp-{uuid.uuid4().hex}",
                    "review_id": review["id"],
                    "profile_id": profile.id,
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
                            "profile_id": entry["profile_id"],
                            "with_comments": entry["with_comments"],
                            "focus": entry["focus"],
                        },
                        conn=conn,
                    )

                created = await self._cmd_create_task({
                    "project_id": current["project_id"],
                    "title": f"Adversarial review: {current['title']}",
                    "description": description,
                    "profile_id": profile.id,
                    "task_type": "research",
                    "root": True,
                    "_after_create_on": record,
                    **({"intelligence_class": profile.default_class} if profile.default_class else {}),
                })
                if not created.get("success"):
                    creation_error = _error(
                        "task_creation_failed", created.get("error", "could not create task")
                    )
                    break
                capacity_reason = None
                if profile.lifecycle == "pool":
                    if profile.max_active == 0:
                        capacity_reason = f"pool {profile.id} has max_active=0"
                    elif not getattr(self.config.swarm, "enabled", True):
                        capacity_reason = "worker pools are disabled"
                result = {**dispatch, "task_id": created["task_id"], "task_state": created["status"]}
                if capacity_reason:
                    result["capacity_warning"] = capacity_reason
                results.append(result)
        for result in results:
            await self._emit_review_event(
                "review.dispatched",
                {
                    "review_id": review["id"],
                    "project_id": review["project_id"],
                    "task_id": result["task_id"],
                    "profile_id": result["profile_id"],
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

    async def _on_review_changes_requested(
        self, review: dict, revision: dict, feedback: str
    ) -> None:
        """Create one task to answer the feedback on this review revision."""
        key = f"review-revision:{review['id']}:{revision['revision']}"
        if await self.db.find_task_by_dedup_key(review["project_id"], key) is not None:
            return
        author_id = review.get("author_task_id")
        author = await self.db.get_task(author_id) if author_id else None
        archived = await self.db.get_archived_task(author_id) if author_id and author is None else None
        parent_id = (author.parent_task_id if author else archived.get("parent_task_id") if archived else None)
        if parent_id is not None and await self.db.get_task(parent_id) is None:
            parent_id = None
        selected_class = revision.get("responder_class")
        selected_profile = revision.get("responder_profile")
        if selected_profile:
            profile = await self.db.get_profile(selected_profile)
        else:
            project = await self.db.get_project(review["project_id"])
            implicit, route_error = await self._supervisor_default_worker_profile(project)
            if route_error or implicit is None:
                raise RuntimeError(f"review {review['id']} revision route unavailable: {route_error}")
            if selected_class:
                matched, route_error = await self._resolve_class_route(selected_class, implicit)
                if route_error:
                    raise RuntimeError(f"review {review['id']} revision route unavailable: {route_error}")
                profile = matched or implicit
            else:
                profile = implicit
        if profile is None:
            raise RuntimeError(f"review {review['id']} revision profile is unavailable")
        class_id = selected_class or profile.default_class
        source = "explicit" if selected_class else "project_default"
        comments = [
            c for c in await self.db.list_review_comments(review["id"])
            if c["resolved_in_revision"] is None
        ]
        lines = [
            feedback,
            "",
            f"Revision worker route: {class_id} on {profile.id} ({source}).",
            "",
            "Unresolved inline comments:",
        ]
        if comments:
            for comment in comments:
                anchor = " > ".join(comment["heading_path"] or []) or "document"
                if comment["quote"]:
                    anchor += f" — quote: {comment['quote']}"
                lines.extend([f"- {comment['id']} [{anchor}]", f"  {comment['body']}"])
        else:
            lines.append("(none)")
        lines.extend([
            "", f"Revise the document and resubmit with `aq review submit --review-id {review['id']} --file <draft.md>`.",
            "Pass `--resolves cmt-...` for each comment you addressed.",
        ])

        async def record(conn, task_id, _parent_id):
            await self.db._upsert_meta(task_id, "review_response", {
                "review_id": review["id"], "revision": revision["revision"],
                "profile_source": source,
            }, conn=conn)

        created = await self._cmd_create_task(
            {
                "project_id": review["project_id"],
                "title": f"Revise {review['title']} (review {review['id']})",
                "description": "\n".join(lines),
                "profile_id": profile.id,
                "intelligence_class": class_id,
                "provider_intent": "preferred" if selected_profile else "class_only",
                "parent_id": parent_id,
                "root": parent_id is None,
                "dedup_key": key,
                "_after_create_on": record,
            }
        )
        if not created.get("success"):
            raise RuntimeError(
                f"review {review['id']} response task creation failed: {created.get('error')}"
            )
