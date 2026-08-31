from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claimed_by import ClaimedBy
    from ..models.task_completion_detail import TaskCompletionDetail
    from ..models.task_ref import TaskRef
    from ..models.task_set_response_children_type_0 import TaskSetResponseChildrenType0
    from ..models.task_set_response_context_item import TaskSetResponseContextItem
    from ..models.task_set_response_parent_type_0 import TaskSetResponseParentType0
    from ..models.task_set_response_provenance_item import TaskSetResponseProvenanceItem


T = TypeVar("T", bound="TaskSetResponse")


@_attrs_define
class TaskSetResponse:
    """
    Attributes:
        id (str):
        project_id (str):
        title (str):
        description (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        priority (int | Unset):  Default: 0.
        assigned_agent (None | str | Unset):
        retry_count (int | Unset):  Default: 0.
        max_retries (int | Unset):  Default: 3.
        integration_mode (None | str | Unset):
        effective_integration_mode (None | str | Unset):
        integration_mode_source (None | str | Unset):
        is_blocked (bool | Unset):  Default: False.
        is_plan_subtask (bool | Unset):  Default: False.
        task_type (None | str | Unset):
        parent_task_id (None | str | Unset):
        profile_id (None | str | Unset):
        intelligence_class (None | str | Unset):
        skip_verification (bool | Unset):  Default: False.
        pr_url (None | str | Unset):
        depends_on (list[TaskRef] | Unset):
        blocks (list[TaskRef] | Unset):
        subtasks (list[TaskRef] | Unset):
        created_at (float | Unset):  Default: 0.0.
        updated_at (float | Unset):  Default: 0.0.
        parent (None | TaskSetResponseParentType0 | Unset):
        children (None | TaskSetResponseChildrenType0 | Unset):
        completion (None | TaskCompletionDetail | Unset):
        needs_attention (None | str | Unset):
        context (list[TaskSetResponseContextItem] | Unset):
        labels (list[str] | Unset):
        provenance (list[TaskSetResponseProvenanceItem] | Unset):
        claimed_by (ClaimedBy | None | Unset):
        fields_changed (list[str] | Unset):
    """

    id: str
    project_id: str
    title: str
    description: str | Unset = ""
    status: str | Unset = ""
    priority: int | Unset = 0
    assigned_agent: None | str | Unset = UNSET
    retry_count: int | Unset = 0
    max_retries: int | Unset = 3
    integration_mode: None | str | Unset = UNSET
    effective_integration_mode: None | str | Unset = UNSET
    integration_mode_source: None | str | Unset = UNSET
    is_blocked: bool | Unset = False
    is_plan_subtask: bool | Unset = False
    task_type: None | str | Unset = UNSET
    parent_task_id: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    skip_verification: bool | Unset = False
    pr_url: None | str | Unset = UNSET
    depends_on: list[TaskRef] | Unset = UNSET
    blocks: list[TaskRef] | Unset = UNSET
    subtasks: list[TaskRef] | Unset = UNSET
    created_at: float | Unset = 0.0
    updated_at: float | Unset = 0.0
    parent: None | TaskSetResponseParentType0 | Unset = UNSET
    children: None | TaskSetResponseChildrenType0 | Unset = UNSET
    completion: None | TaskCompletionDetail | Unset = UNSET
    needs_attention: None | str | Unset = UNSET
    context: list[TaskSetResponseContextItem] | Unset = UNSET
    labels: list[str] | Unset = UNSET
    provenance: list[TaskSetResponseProvenanceItem] | Unset = UNSET
    claimed_by: ClaimedBy | None | Unset = UNSET
    fields_changed: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.claimed_by import ClaimedBy  # noqa: PLC0415
        from ..models.task_completion_detail import TaskCompletionDetail  # noqa: PLC0415
        from ..models.task_set_response_children_type_0 import TaskSetResponseChildrenType0  # noqa: PLC0415
        from ..models.task_set_response_parent_type_0 import TaskSetResponseParentType0  # noqa: PLC0415

        id = self.id

        project_id = self.project_id

        title = self.title

        description = self.description

        status = self.status

        priority = self.priority

        assigned_agent: None | str | Unset
        if isinstance(self.assigned_agent, Unset):
            assigned_agent = UNSET
        else:
            assigned_agent = self.assigned_agent

        retry_count = self.retry_count

        max_retries = self.max_retries

        integration_mode: None | str | Unset
        if isinstance(self.integration_mode, Unset):
            integration_mode = UNSET
        else:
            integration_mode = self.integration_mode

        effective_integration_mode: None | str | Unset
        if isinstance(self.effective_integration_mode, Unset):
            effective_integration_mode = UNSET
        else:
            effective_integration_mode = self.effective_integration_mode

        integration_mode_source: None | str | Unset
        if isinstance(self.integration_mode_source, Unset):
            integration_mode_source = UNSET
        else:
            integration_mode_source = self.integration_mode_source

        is_blocked = self.is_blocked

        is_plan_subtask = self.is_plan_subtask

        task_type: None | str | Unset
        if isinstance(self.task_type, Unset):
            task_type = UNSET
        else:
            task_type = self.task_type

        parent_task_id: None | str | Unset
        if isinstance(self.parent_task_id, Unset):
            parent_task_id = UNSET
        else:
            parent_task_id = self.parent_task_id

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

        skip_verification = self.skip_verification

        pr_url: None | str | Unset
        if isinstance(self.pr_url, Unset):
            pr_url = UNSET
        else:
            pr_url = self.pr_url

        depends_on: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.depends_on, Unset):
            depends_on = []
            for depends_on_item_data in self.depends_on:
                depends_on_item = depends_on_item_data.to_dict()
                depends_on.append(depends_on_item)

        blocks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blocks, Unset):
            blocks = []
            for blocks_item_data in self.blocks:
                blocks_item = blocks_item_data.to_dict()
                blocks.append(blocks_item)

        subtasks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.subtasks, Unset):
            subtasks = []
            for subtasks_item_data in self.subtasks:
                subtasks_item = subtasks_item_data.to_dict()
                subtasks.append(subtasks_item)

        created_at = self.created_at

        updated_at = self.updated_at

        parent: dict[str, Any] | None | Unset
        if isinstance(self.parent, Unset):
            parent = UNSET
        elif isinstance(self.parent, TaskSetResponseParentType0):
            parent = self.parent.to_dict()
        else:
            parent = self.parent

        children: dict[str, Any] | None | Unset
        if isinstance(self.children, Unset):
            children = UNSET
        elif isinstance(self.children, TaskSetResponseChildrenType0):
            children = self.children.to_dict()
        else:
            children = self.children

        completion: dict[str, Any] | None | Unset
        if isinstance(self.completion, Unset):
            completion = UNSET
        elif isinstance(self.completion, TaskCompletionDetail):
            completion = self.completion.to_dict()
        else:
            completion = self.completion

        needs_attention: None | str | Unset
        if isinstance(self.needs_attention, Unset):
            needs_attention = UNSET
        else:
            needs_attention = self.needs_attention

        context: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.context, Unset):
            context = []
            for context_item_data in self.context:
                context_item = context_item_data.to_dict()
                context.append(context_item)

        labels: list[str] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels

        provenance: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.provenance, Unset):
            provenance = []
            for provenance_item_data in self.provenance:
                provenance_item = provenance_item_data.to_dict()
                provenance.append(provenance_item)

        claimed_by: dict[str, Any] | None | Unset
        if isinstance(self.claimed_by, Unset):
            claimed_by = UNSET
        elif isinstance(self.claimed_by, ClaimedBy):
            claimed_by = self.claimed_by.to_dict()
        else:
            claimed_by = self.claimed_by

        fields_changed: list[str] | Unset = UNSET
        if not isinstance(self.fields_changed, Unset):
            fields_changed = self.fields_changed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "project_id": project_id,
                "title": title,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if status is not UNSET:
            field_dict["status"] = status
        if priority is not UNSET:
            field_dict["priority"] = priority
        if assigned_agent is not UNSET:
            field_dict["assigned_agent"] = assigned_agent
        if retry_count is not UNSET:
            field_dict["retry_count"] = retry_count
        if max_retries is not UNSET:
            field_dict["max_retries"] = max_retries
        if integration_mode is not UNSET:
            field_dict["integration_mode"] = integration_mode
        if effective_integration_mode is not UNSET:
            field_dict["effective_integration_mode"] = effective_integration_mode
        if integration_mode_source is not UNSET:
            field_dict["integration_mode_source"] = integration_mode_source
        if is_blocked is not UNSET:
            field_dict["is_blocked"] = is_blocked
        if is_plan_subtask is not UNSET:
            field_dict["is_plan_subtask"] = is_plan_subtask
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if parent_task_id is not UNSET:
            field_dict["parent_task_id"] = parent_task_id
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if skip_verification is not UNSET:
            field_dict["skip_verification"] = skip_verification
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if depends_on is not UNSET:
            field_dict["depends_on"] = depends_on
        if blocks is not UNSET:
            field_dict["blocks"] = blocks
        if subtasks is not UNSET:
            field_dict["subtasks"] = subtasks
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if updated_at is not UNSET:
            field_dict["updated_at"] = updated_at
        if parent is not UNSET:
            field_dict["parent"] = parent
        if children is not UNSET:
            field_dict["children"] = children
        if completion is not UNSET:
            field_dict["completion"] = completion
        if needs_attention is not UNSET:
            field_dict["needs_attention"] = needs_attention
        if context is not UNSET:
            field_dict["context"] = context
        if labels is not UNSET:
            field_dict["labels"] = labels
        if provenance is not UNSET:
            field_dict["provenance"] = provenance
        if claimed_by is not UNSET:
            field_dict["claimed_by"] = claimed_by
        if fields_changed is not UNSET:
            field_dict["fields_changed"] = fields_changed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claimed_by import ClaimedBy  # noqa: PLC0415
        from ..models.task_completion_detail import TaskCompletionDetail  # noqa: PLC0415
        from ..models.task_ref import TaskRef  # noqa: PLC0415
        from ..models.task_set_response_children_type_0 import TaskSetResponseChildrenType0  # noqa: PLC0415
        from ..models.task_set_response_context_item import TaskSetResponseContextItem  # noqa: PLC0415
        from ..models.task_set_response_parent_type_0 import TaskSetResponseParentType0  # noqa: PLC0415
        from ..models.task_set_response_provenance_item import TaskSetResponseProvenanceItem  # noqa: PLC0415

        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id")

        title = d.pop("title")

        description = d.pop("description", UNSET)

        status = d.pop("status", UNSET)

        priority = d.pop("priority", UNSET)

        def _parse_assigned_agent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assigned_agent = _parse_assigned_agent(d.pop("assigned_agent", UNSET))

        retry_count = d.pop("retry_count", UNSET)

        max_retries = d.pop("max_retries", UNSET)

        def _parse_integration_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        integration_mode = _parse_integration_mode(d.pop("integration_mode", UNSET))

        def _parse_effective_integration_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        effective_integration_mode = _parse_effective_integration_mode(d.pop("effective_integration_mode", UNSET))

        def _parse_integration_mode_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        integration_mode_source = _parse_integration_mode_source(d.pop("integration_mode_source", UNSET))

        is_blocked = d.pop("is_blocked", UNSET)

        is_plan_subtask = d.pop("is_plan_subtask", UNSET)

        def _parse_task_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_type = _parse_task_type(d.pop("task_type", UNSET))

        def _parse_parent_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_task_id = _parse_parent_task_id(d.pop("parent_task_id", UNSET))

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

        skip_verification = d.pop("skip_verification", UNSET)

        def _parse_pr_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pr_url = _parse_pr_url(d.pop("pr_url", UNSET))

        _depends_on = d.pop("depends_on", UNSET)
        depends_on: list[TaskRef] | Unset = UNSET
        if _depends_on is not UNSET:
            depends_on = []
            for depends_on_item_data in _depends_on:
                depends_on_item = TaskRef.from_dict(depends_on_item_data)

                depends_on.append(depends_on_item)

        _blocks = d.pop("blocks", UNSET)
        blocks: list[TaskRef] | Unset = UNSET
        if _blocks is not UNSET:
            blocks = []
            for blocks_item_data in _blocks:
                blocks_item = TaskRef.from_dict(blocks_item_data)

                blocks.append(blocks_item)

        _subtasks = d.pop("subtasks", UNSET)
        subtasks: list[TaskRef] | Unset = UNSET
        if _subtasks is not UNSET:
            subtasks = []
            for subtasks_item_data in _subtasks:
                subtasks_item = TaskRef.from_dict(subtasks_item_data)

                subtasks.append(subtasks_item)

        created_at = d.pop("created_at", UNSET)

        updated_at = d.pop("updated_at", UNSET)

        def _parse_parent(data: object) -> None | TaskSetResponseParentType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                parent_type_0 = TaskSetResponseParentType0.from_dict(data)

                return parent_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskSetResponseParentType0 | Unset, data)

        parent = _parse_parent(d.pop("parent", UNSET))

        def _parse_children(data: object) -> None | TaskSetResponseChildrenType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                children_type_0 = TaskSetResponseChildrenType0.from_dict(data)

                return children_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskSetResponseChildrenType0 | Unset, data)

        children = _parse_children(d.pop("children", UNSET))

        def _parse_completion(data: object) -> None | TaskCompletionDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                completion_type_0 = TaskCompletionDetail.from_dict(data)

                return completion_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskCompletionDetail | Unset, data)

        completion = _parse_completion(d.pop("completion", UNSET))

        def _parse_needs_attention(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        needs_attention = _parse_needs_attention(d.pop("needs_attention", UNSET))

        _context = d.pop("context", UNSET)
        context: list[TaskSetResponseContextItem] | Unset = UNSET
        if _context is not UNSET:
            context = []
            for context_item_data in _context:
                context_item = TaskSetResponseContextItem.from_dict(context_item_data)

                context.append(context_item)

        labels = cast(list[str], d.pop("labels", UNSET))

        _provenance = d.pop("provenance", UNSET)
        provenance: list[TaskSetResponseProvenanceItem] | Unset = UNSET
        if _provenance is not UNSET:
            provenance = []
            for provenance_item_data in _provenance:
                provenance_item = TaskSetResponseProvenanceItem.from_dict(provenance_item_data)

                provenance.append(provenance_item)

        def _parse_claimed_by(data: object) -> ClaimedBy | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                claimed_by_type_0 = ClaimedBy.from_dict(data)

                return claimed_by_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClaimedBy | None | Unset, data)

        claimed_by = _parse_claimed_by(d.pop("claimed_by", UNSET))

        fields_changed = cast(list[str], d.pop("fields_changed", UNSET))

        task_set_response = cls(
            id=id,
            project_id=project_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assigned_agent=assigned_agent,
            retry_count=retry_count,
            max_retries=max_retries,
            integration_mode=integration_mode,
            effective_integration_mode=effective_integration_mode,
            integration_mode_source=integration_mode_source,
            is_blocked=is_blocked,
            is_plan_subtask=is_plan_subtask,
            task_type=task_type,
            parent_task_id=parent_task_id,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            skip_verification=skip_verification,
            pr_url=pr_url,
            depends_on=depends_on,
            blocks=blocks,
            subtasks=subtasks,
            created_at=created_at,
            updated_at=updated_at,
            parent=parent,
            children=children,
            completion=completion,
            needs_attention=needs_attention,
            context=context,
            labels=labels,
            provenance=provenance,
            claimed_by=claimed_by,
            fields_changed=fields_changed,
        )

        task_set_response.additional_properties = d
        return task_set_response

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
