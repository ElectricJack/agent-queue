from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_update_request_payload_type_0 import TaskBatchUpdateRequestPayloadType0


T = TypeVar("T", bound="TaskBatchUpdateRequest")


@_attrs_define
class TaskBatchUpdateRequest:
    """
    Attributes:
        proposal_id (str):
        project_id (None | str | Unset):
        payload (None | TaskBatchUpdateRequestPayloadType0 | Unset):
        expected_revision (int | None | Unset):
        operations (list[Any] | None | Unset):
    """

    proposal_id: str
    project_id: None | str | Unset = UNSET
    payload: None | TaskBatchUpdateRequestPayloadType0 | Unset = UNSET
    expected_revision: int | None | Unset = UNSET
    operations: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_batch_update_request_payload_type_0 import TaskBatchUpdateRequestPayloadType0

        proposal_id = self.proposal_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        payload: dict[str, Any] | None | Unset
        if isinstance(self.payload, Unset):
            payload = UNSET
        elif isinstance(self.payload, TaskBatchUpdateRequestPayloadType0):
            payload = self.payload.to_dict()
        else:
            payload = self.payload

        expected_revision: int | None | Unset
        if isinstance(self.expected_revision, Unset):
            expected_revision = UNSET
        else:
            expected_revision = self.expected_revision

        operations: list[Any] | None | Unset
        if isinstance(self.operations, Unset):
            operations = UNSET
        elif isinstance(self.operations, list):
            operations = self.operations

        else:
            operations = self.operations

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "proposal_id": proposal_id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if payload is not UNSET:
            field_dict["payload"] = payload
        if expected_revision is not UNSET:
            field_dict["expected_revision"] = expected_revision
        if operations is not UNSET:
            field_dict["operations"] = operations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_update_request_payload_type_0 import TaskBatchUpdateRequestPayloadType0

        d = dict(src_dict)
        proposal_id = d.pop("proposal_id")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_payload(data: object) -> None | TaskBatchUpdateRequestPayloadType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                payload_type_0 = TaskBatchUpdateRequestPayloadType0.from_dict(data)

                return payload_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchUpdateRequestPayloadType0 | Unset, data)

        payload = _parse_payload(d.pop("payload", UNSET))

        def _parse_expected_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_revision = _parse_expected_revision(d.pop("expected_revision", UNSET))

        def _parse_operations(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                operations_type_0 = cast(list[Any], data)

                return operations_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        operations = _parse_operations(d.pop("operations", UNSET))

        task_batch_update_request = cls(
            proposal_id=proposal_id,
            project_id=project_id,
            payload=payload,
            expected_revision=expected_revision,
            operations=operations,
        )

        task_batch_update_request.additional_properties = d
        return task_batch_update_request

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
