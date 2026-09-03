from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateTaskRequest")


@_attrs_define
class CreateTaskRequest:
    """
    Attributes:
        title (str): Short task title
        project_id (None | str | Unset): Project ID (optional — inferred from active project)
        description (None | str | Unset): Complete, self-contained instructions for the agent. Include ALL context: file
            paths, requirements, error messages, expected behavior, relevant code snippets, and design decisions from this
            conversation. Write as if the agent has never seen this conversation.
        priority (int | Unset): Priority (lower = higher priority, default 100) Default: 100.
        integration_mode (None | str | Unset): Integration-policy override: 'pull_request' pushes the task branch and
            opens a PR (review pipeline owns the merge); 'direct' merges into the default branch on completion. Omit to
            inherit the project/system policy.
        task_type (None | str | Unset): Categorize the task type for display and filtering (optional)
        profile_id (None | str | Unset): Agent profile ID to configure the agent with specific tools/capabilities
            (optional)
        intelligence_class (None | str | Unset): Execution intelligence class id, e.g. deep-high or standard-medium. Use
            list_intelligence_classes for current IDs. Set profile_id and this field together at creation to route work
            atomically.
        preferred_workspace_id (None | str | Unset): Workspace ID to prefer when assigning this task to an agent. Use
            this when the task must run in a specific workspace (e.g. one that contains a merge conflict). Get the ID from
            find_merge_conflict_workspaces or list_workspaces.
        attachments (list[Any] | None | Unset): List of absolute file paths to images or files that the agent should
            have access to when working on this task. These are typically paths to Discord attachment images that were
            downloaded locally. The agent will be told to read these files using the Read tool.
        deliverables (list[Any] | None | Unset): Plan-derived implementation contract checked before a passing close.
        skip_verification (bool | Unset): If true, skip git verification on task completion. Use for
            investigation/research tasks that don't produce code changes requiring git cleanup. Default: False.
        affinity_agent_id (None | str | Unset): Preferred agent ID for context continuity. The scheduler will prefer
            this agent when assigning the task, but will fall back to any available agent if the preferred one is busy.
        affinity_reason (None | str | Unset): Why this agent is preferred: 'context' (has relevant conversation
            history), 'workspace' (already has the workspace locked), 'type' (matches the required agent type).
        workspace_mode (None | str | Unset): Workspace lock mode. 'exclusive' (default): one agent per workspace.
            'branch-isolated': DEPRECATED — it is now an alias for 'exclusive'. The worktree fallback that made it mean
            'multiple agents on separate branches in the same repo' was retired (worktree-execution spec §7.4); parallel
            work in one repo is provided by worktree slots, which are chosen by the workspace kind's 'mode', not by this
            field. 'directory-isolated': multiple agents on separate directories (not yet implemented).
        requires_kinds (list[Any] | None | Unset): Workspace kinds this task needs (workspaces-v2 spec §5). Each entry
            is either a kind id string (e.g. 'game-repo') or a dict {kind, alias?}. Auto-attached kinds (e.g. 'vault') do
            NOT need to be listed. When omitted, the task implicitly requires 'project-repo' — preserving today's single-
            workspace behavior. Each kind must resolve via project-scoped or system-wide vault/workspace-kinds/<id>.md.
        parent_id (None | str | Unset): Create as a child of this container; the id becomes <parent>.<n>
        root (bool | Unset): Explicitly create at project level instead of under a container. Mutually exclusive with
            parent_id. Use it when filing cross-cutting work from inside a task so the placement reads as deliberate rather
            than as a forgotten parent_id (swarm-work-model §12). Default: False.
        depends_on (list[Any] | None | Unset): Task IDs or described dependency edges (optional).
        discovered_from (None | str | Unset): Task ID this work was discovered from (provenance, swarm-work-model §9; a
            worker-filed caller is restricted to the held task's subtree).
        reason (None | str | Unset): WHY this task exists: the reason it was spawned, not just what it does. REQUIRED
            when creating work from inside another task; stored on the parent-child or discovered-from edge back to its
            origin.
        dedup_key (None | str | Unset): Idempotency key for find-or-create semantics (see ensure_task).
    """

    title: str
    project_id: None | str | Unset = UNSET
    description: None | str | Unset = UNSET
    priority: int | Unset = 100
    integration_mode: None | str | Unset = UNSET
    task_type: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    preferred_workspace_id: None | str | Unset = UNSET
    attachments: list[Any] | None | Unset = UNSET
    deliverables: list[Any] | None | Unset = UNSET
    skip_verification: bool | Unset = False
    affinity_agent_id: None | str | Unset = UNSET
    affinity_reason: None | str | Unset = UNSET
    workspace_mode: None | str | Unset = UNSET
    requires_kinds: list[Any] | None | Unset = UNSET
    parent_id: None | str | Unset = UNSET
    root: bool | Unset = False
    depends_on: list[Any] | None | Unset = UNSET
    discovered_from: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    dedup_key: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        title = self.title

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        priority = self.priority

        integration_mode: None | str | Unset
        if isinstance(self.integration_mode, Unset):
            integration_mode = UNSET
        else:
            integration_mode = self.integration_mode

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        preferred_workspace_id: None | str | Unset
        if isinstance(self.preferred_workspace_id, Unset):
            preferred_workspace_id = UNSET
        else:
            preferred_workspace_id = self.preferred_workspace_id

        attachments: list[Any] | None | Unset
        if isinstance(self.attachments, Unset):
            attachments = UNSET
        elif isinstance(self.attachments, list):
            attachments = self.attachments

        else:
            attachments = self.attachments

        deliverables: list[Any] | None | Unset
        if isinstance(self.deliverables, Unset):
            deliverables = UNSET
        elif isinstance(self.deliverables, list):
            deliverables = self.deliverables

        else:
            deliverables = self.deliverables

        skip_verification = self.skip_verification

        affinity_agent_id: None | str | Unset
        if isinstance(self.affinity_agent_id, Unset):
            affinity_agent_id = UNSET
        else:
            affinity_agent_id = self.affinity_agent_id

        affinity_reason: None | str | Unset
        if isinstance(self.affinity_reason, Unset):
            affinity_reason = UNSET
        else:
            affinity_reason = self.affinity_reason

        workspace_mode: None | str | Unset
        if isinstance(self.workspace_mode, Unset):
            workspace_mode = UNSET
        else:
            workspace_mode = self.workspace_mode

        requires_kinds: list[Any] | None | Unset
        if isinstance(self.requires_kinds, Unset):
            requires_kinds = UNSET
        elif isinstance(self.requires_kinds, list):
            requires_kinds = self.requires_kinds

        else:
            requires_kinds = self.requires_kinds

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        root = self.root

        depends_on: list[Any] | None | Unset
        if isinstance(self.depends_on, Unset):
            depends_on = UNSET
        elif isinstance(self.depends_on, list):
            depends_on = self.depends_on

        else:
            depends_on = self.depends_on

        discovered_from: None | str | Unset
        if isinstance(self.discovered_from, Unset):
            discovered_from = UNSET
        else:
            discovered_from = self.discovered_from

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        dedup_key: None | str | Unset
        if isinstance(self.dedup_key, Unset):
            dedup_key = UNSET
        else:
            dedup_key = self.dedup_key

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if description is not UNSET:
            field_dict["description"] = description
        if priority is not UNSET:
            field_dict["priority"] = priority
        if integration_mode is not UNSET:
            field_dict["integration_mode"] = integration_mode
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if preferred_workspace_id is not UNSET:
            field_dict["preferred_workspace_id"] = preferred_workspace_id
        if attachments is not UNSET:
            field_dict["attachments"] = attachments
        if deliverables is not UNSET:
            field_dict["deliverables"] = deliverables
        if skip_verification is not UNSET:
            field_dict["skip_verification"] = skip_verification
        if affinity_agent_id is not UNSET:
            field_dict["affinity_agent_id"] = affinity_agent_id
        if affinity_reason is not UNSET:
            field_dict["affinity_reason"] = affinity_reason
        if workspace_mode is not UNSET:
            field_dict["workspace_mode"] = workspace_mode
        if requires_kinds is not UNSET:
            field_dict["requires_kinds"] = requires_kinds
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if root is not UNSET:
            field_dict["root"] = root
        if depends_on is not UNSET:
            field_dict["depends_on"] = depends_on
        if discovered_from is not UNSET:
            field_dict["discovered_from"] = discovered_from
        if reason is not UNSET:
            field_dict["reason"] = reason
        if dedup_key is not UNSET:
            field_dict["dedup_key"] = dedup_key

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        title = d.pop("title")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        priority = d.pop("priority", UNSET)

        def _parse_integration_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        integration_mode = _parse_integration_mode(d.pop("integration_mode", UNSET))

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_preferred_workspace_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preferred_workspace_id = _parse_preferred_workspace_id(d.pop("preferred_workspace_id", UNSET))

        def _parse_attachments(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                attachments_type_0 = cast(list[Any], data)

                return attachments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        attachments = _parse_attachments(d.pop("attachments", UNSET))

        def _parse_deliverables(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                deliverables_type_0 = cast(list[Any], data)

                return deliverables_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        deliverables = _parse_deliverables(d.pop("deliverables", UNSET))

        skip_verification = d.pop("skip_verification", UNSET)

        def _parse_affinity_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        affinity_agent_id = _parse_affinity_agent_id(d.pop("affinity_agent_id", UNSET))

        def _parse_affinity_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        affinity_reason = _parse_affinity_reason(d.pop("affinity_reason", UNSET))

        def _parse_workspace_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace_mode = _parse_workspace_mode(d.pop("workspace_mode", UNSET))

        def _parse_requires_kinds(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                requires_kinds_type_0 = cast(list[Any], data)

                return requires_kinds_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        requires_kinds = _parse_requires_kinds(d.pop("requires_kinds", UNSET))

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        root = d.pop("root", UNSET)

        def _parse_depends_on(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                depends_on_type_0 = cast(list[Any], data)

                return depends_on_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        depends_on = _parse_depends_on(d.pop("depends_on", UNSET))

        def _parse_discovered_from(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        discovered_from = _parse_discovered_from(d.pop("discovered_from", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_dedup_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dedup_key = _parse_dedup_key(d.pop("dedup_key", UNSET))

        create_task_request = cls(
            title=title,
            project_id=project_id,
            description=description,
            priority=priority,
            integration_mode=integration_mode,
            task_type=task_type,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            deliverables=deliverables,
            skip_verification=skip_verification,
            affinity_agent_id=affinity_agent_id,
            affinity_reason=affinity_reason,
            workspace_mode=workspace_mode,
            requires_kinds=requires_kinds,
            parent_id=parent_id,
            root=root,
            depends_on=depends_on,
            discovered_from=discovered_from,
            reason=reason,
            dedup_key=dedup_key,
        )

        create_task_request.additional_properties = d
        return create_task_request

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
