from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_ref import TaskRef


T = TypeVar("T", bound="TaskDetail")


@_attrs_define
class TaskDetail:
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
        requires_approval (bool | Unset):  Default: False.
        is_plan_subtask (bool | Unset):  Default: False.
        task_type (None | str | Unset):
        parent_task_id (None | str | Unset):
        profile_id (None | str | Unset):
        auto_approve_plan (bool | Unset):  Default: False.
        pr_url (None | str | Unset):
        depends_on (list[TaskRef] | Unset):
        blocks (list[TaskRef] | Unset):
        subtasks (list[TaskRef] | Unset):
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
    requires_approval: bool | Unset = False
    is_plan_subtask: bool | Unset = False
    task_type: None | str | Unset = UNSET
    parent_task_id: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    auto_approve_plan: bool | Unset = False
    pr_url: None | str | Unset = UNSET
    depends_on: list[TaskRef] | Unset = UNSET
    blocks: list[TaskRef] | Unset = UNSET
    subtasks: list[TaskRef] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
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

        requires_approval = self.requires_approval

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

        auto_approve_plan = self.auto_approve_plan

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
        if requires_approval is not UNSET:
            field_dict["requires_approval"] = requires_approval
        if is_plan_subtask is not UNSET:
            field_dict["is_plan_subtask"] = is_plan_subtask
        if task_type is not UNSET:
            field_dict["task_type"] = task_type
        if parent_task_id is not UNSET:
            field_dict["parent_task_id"] = parent_task_id
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if auto_approve_plan is not UNSET:
            field_dict["auto_approve_plan"] = auto_approve_plan
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if depends_on is not UNSET:
            field_dict["depends_on"] = depends_on
        if blocks is not UNSET:
            field_dict["blocks"] = blocks
        if subtasks is not UNSET:
            field_dict["subtasks"] = subtasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_ref import TaskRef

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

        requires_approval = d.pop("requires_approval", UNSET)

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

        auto_approve_plan = d.pop("auto_approve_plan", UNSET)

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

        task_detail = cls(
            id=id,
            project_id=project_id,
            title=title,
            description=description,
            status=status,
            priority=priority,
            assigned_agent=assigned_agent,
            retry_count=retry_count,
            max_retries=max_retries,
            requires_approval=requires_approval,
            is_plan_subtask=is_plan_subtask,
            task_type=task_type,
            parent_task_id=parent_task_id,
            profile_id=profile_id,
            auto_approve_plan=auto_approve_plan,
            pr_url=pr_url,
            depends_on=depends_on,
            blocks=blocks,
            subtasks=subtasks,
        )

        task_detail.additional_properties = d
        return task_detail

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
