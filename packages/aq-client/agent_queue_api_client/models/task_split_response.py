from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_split_response_impact_type_0 import TaskSplitResponseImpactType0
    from ..models.task_split_response_owner_type_0 import TaskSplitResponseOwnerType0


T = TypeVar("T", bound="TaskSplitResponse")


@_attrs_define
class TaskSplitResponse:
    """
    Attributes:
        success (bool):
        task_id (None | str | Unset):
        moved_task_id (None | str | Unset):
        generation (int | None | Unset):
        impact (None | TaskSplitResponseImpactType0 | Unset):
        code (None | str | Unset):
        error (None | str | Unset):
        owner (None | TaskSplitResponseOwnerType0 | Unset):
        safe_continuation (None | str | Unset):
    """

    success: bool
    task_id: None | str | Unset = UNSET
    moved_task_id: None | str | Unset = UNSET
    generation: int | None | Unset = UNSET
    impact: None | TaskSplitResponseImpactType0 | Unset = UNSET
    code: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    owner: None | TaskSplitResponseOwnerType0 | Unset = UNSET
    safe_continuation: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_split_response_impact_type_0 import TaskSplitResponseImpactType0
        from ..models.task_split_response_owner_type_0 import TaskSplitResponseOwnerType0

        success = self.success

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        moved_task_id: None | str | Unset
        if isinstance(self.moved_task_id, Unset):
            moved_task_id = UNSET
        else:
            moved_task_id = self.moved_task_id

        generation: int | None | Unset
        if isinstance(self.generation, Unset):
            generation = UNSET
        else:
            generation = self.generation

        impact: dict[str, Any] | None | Unset
        if isinstance(self.impact, Unset):
            impact = UNSET
        elif isinstance(self.impact, TaskSplitResponseImpactType0):
            impact = self.impact.to_dict()
        else:
            impact = self.impact

        code: None | str | Unset
        if isinstance(self.code, Unset):
            code = UNSET
        else:
            code = self.code

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        owner: dict[str, Any] | None | Unset
        if isinstance(self.owner, Unset):
            owner = UNSET
        elif isinstance(self.owner, TaskSplitResponseOwnerType0):
            owner = self.owner.to_dict()
        else:
            owner = self.owner

        safe_continuation: None | str | Unset
        if isinstance(self.safe_continuation, Unset):
            safe_continuation = UNSET
        else:
            safe_continuation = self.safe_continuation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if moved_task_id is not UNSET:
            field_dict["moved_task_id"] = moved_task_id
        if generation is not UNSET:
            field_dict["generation"] = generation
        if impact is not UNSET:
            field_dict["impact"] = impact
        if code is not UNSET:
            field_dict["code"] = code
        if error is not UNSET:
            field_dict["error"] = error
        if owner is not UNSET:
            field_dict["owner"] = owner
        if safe_continuation is not UNSET:
            field_dict["safe_continuation"] = safe_continuation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_split_response_impact_type_0 import TaskSplitResponseImpactType0
        from ..models.task_split_response_owner_type_0 import TaskSplitResponseOwnerType0

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_moved_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        moved_task_id = _parse_moved_task_id(d.pop("moved_task_id", UNSET))

        def _parse_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        generation = _parse_generation(d.pop("generation", UNSET))

        def _parse_impact(data: object) -> None | TaskSplitResponseImpactType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                impact_type_0 = TaskSplitResponseImpactType0.from_dict(data)

                return impact_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskSplitResponseImpactType0 | Unset, data)

        impact = _parse_impact(d.pop("impact", UNSET))

        def _parse_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        code = _parse_code(d.pop("code", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_owner(data: object) -> None | TaskSplitResponseOwnerType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                owner_type_0 = TaskSplitResponseOwnerType0.from_dict(data)

                return owner_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskSplitResponseOwnerType0 | Unset, data)

        owner = _parse_owner(d.pop("owner", UNSET))

        def _parse_safe_continuation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        safe_continuation = _parse_safe_continuation(d.pop("safe_continuation", UNSET))

        task_split_response = cls(
            success=success,
            task_id=task_id,
            moved_task_id=moved_task_id,
            generation=generation,
            impact=impact,
            code=code,
            error=error,
            owner=owner,
            safe_continuation=safe_continuation,
        )

        task_split_response.additional_properties = d
        return task_split_response

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
