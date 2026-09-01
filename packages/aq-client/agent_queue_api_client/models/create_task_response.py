from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.create_task_response_depends_on_item import CreateTaskResponseDependsOnItem


T = TypeVar("T", bound="CreateTaskResponse")


@_attrs_define
class CreateTaskResponse:
    """
    Attributes:
        created (str):
        title (str):
        project_id (str):
        integration_mode (None | str | Unset):
        task_type (None | str | Unset):
        profile_id (None | str | Unset):
        intelligence_class (None | str | Unset):
        preferred_workspace_id (None | str | Unset):
        attachments (list[str] | None | Unset):
        skip_verification (bool | Unset):  Default: False.
        warning (None | str | Unset):
        success (bool | None | Unset):
        task_id (None | str | Unset):
        gate_id (None | str | Unset):
        status (None | str | Unset):
        reason (None | str | Unset):
        depends_on (list[CreateTaskResponseDependsOnItem] | Unset):
    """

    created: str
    title: str
    project_id: str
    integration_mode: None | str | Unset = UNSET
    task_type: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    preferred_workspace_id: None | str | Unset = UNSET
    attachments: list[str] | None | Unset = UNSET
    skip_verification: bool | Unset = False
    warning: None | str | Unset = UNSET
    success: bool | None | Unset = UNSET
    task_id: None | str | Unset = UNSET
    gate_id: None | str | Unset = UNSET
    status: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    depends_on: list[CreateTaskResponseDependsOnItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        title = self.title

        project_id = self.project_id

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

        attachments: list[str] | None | Unset
        if isinstance(self.attachments, Unset):
            attachments = UNSET
        elif isinstance(self.attachments, list):
            attachments = self.attachments

        else:
            attachments = self.attachments

        skip_verification = self.skip_verification

        warning: None | str | Unset
        if isinstance(self.warning, Unset):
            warning = UNSET
        else:
            warning = self.warning

        success: bool | None | Unset
        if isinstance(self.success, Unset):
            success = UNSET
        else:
            success = self.success

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        gate_id: None | str | Unset
        if isinstance(self.gate_id, Unset):
            gate_id = UNSET
        else:
            gate_id = self.gate_id

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        depends_on: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.depends_on, Unset):
            depends_on = []
            for depends_on_item_data in self.depends_on:
                depends_on_item = depends_on_item_data.to_dict()
                depends_on.append(depends_on_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "title": title,
                "project_id": project_id,
            }
        )
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
        if skip_verification is not UNSET:
            field_dict["skip_verification"] = skip_verification
        if warning is not UNSET:
            field_dict["warning"] = warning
        if success is not UNSET:
            field_dict["success"] = success
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if gate_id is not UNSET:
            field_dict["gate_id"] = gate_id
        if status is not UNSET:
            field_dict["status"] = status
        if reason is not UNSET:
            field_dict["reason"] = reason
        if depends_on is not UNSET:
            field_dict["depends_on"] = depends_on

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_task_response_depends_on_item import CreateTaskResponseDependsOnItem  # noqa: PLC0415

        d = dict(src_dict)
        created = d.pop("created")

        title = d.pop("title")

        project_id = d.pop("project_id")

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

        def _parse_attachments(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                attachments_type_0 = cast(list[str], data)

                return attachments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        attachments = _parse_attachments(d.pop("attachments", UNSET))

        skip_verification = d.pop("skip_verification", UNSET)

        def _parse_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        warning = _parse_warning(d.pop("warning", UNSET))

        def _parse_success(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        success = _parse_success(d.pop("success", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_gate_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        gate_id = _parse_gate_id(d.pop("gate_id", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        _depends_on = d.pop("depends_on", UNSET)
        depends_on: list[CreateTaskResponseDependsOnItem] | Unset = UNSET
        if _depends_on is not UNSET:
            depends_on = []
            for depends_on_item_data in _depends_on:
                depends_on_item = CreateTaskResponseDependsOnItem.from_dict(depends_on_item_data)

                depends_on.append(depends_on_item)

        create_task_response = cls(
            created=created,
            title=title,
            project_id=project_id,
            integration_mode=integration_mode,
            task_type=task_type,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            preferred_workspace_id=preferred_workspace_id,
            attachments=attachments,
            skip_verification=skip_verification,
            warning=warning,
            success=success,
            task_id=task_id,
            gate_id=gate_id,
            status=status,
            reason=reason,
            depends_on=depends_on,
        )

        create_task_response.additional_properties = d
        return create_task_response

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
