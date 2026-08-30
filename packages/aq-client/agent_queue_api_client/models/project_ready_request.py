from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProjectReadyRequest")


@_attrs_define
class ProjectReadyRequest:
    """
    Attributes:
        project_id (None | str | Unset): Project id (falls back to the active project when omitted).
        labels (list[Any] | None | Unset): Restrict frontier to tasks carrying ALL of these labels.
        any_label (list[Any] | None | Unset): Restrict frontier to tasks carrying ANY of these labels.
        profile_id (None | str | Unset): Restrict the frontier to tasks this profile would be offered. Uses the same
            widening as the work query: when this is the project's default profile, unassigned tasks count as its work too.
        brief (bool | None | Unset): Project each ready task to id, title, status, priority, is_blocked, profile_id
            instead of the default shape.
    """

    project_id: None | str | Unset = UNSET
    labels: list[Any] | None | Unset = UNSET
    any_label: list[Any] | None | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    brief: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        labels: list[Any] | None | Unset
        if isinstance(self.labels, Unset):
            labels = UNSET
        elif isinstance(self.labels, list):
            labels = self.labels

        else:
            labels = self.labels

        any_label: list[Any] | None | Unset
        if isinstance(self.any_label, Unset):
            any_label = UNSET
        elif isinstance(self.any_label, list):
            any_label = self.any_label

        else:
            any_label = self.any_label

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        brief: bool | None | Unset
        if isinstance(self.brief, Unset):
            brief = UNSET
        else:
            brief = self.brief

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if labels is not UNSET:
            field_dict["labels"] = labels
        if any_label is not UNSET:
            field_dict["any_label"] = any_label
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if brief is not UNSET:
            field_dict["brief"] = brief

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_labels(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                labels_type_0 = cast(list[Any], data)

                return labels_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        labels = _parse_labels(d.pop("labels", UNSET))

        def _parse_any_label(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                any_label_type_0 = cast(list[Any], data)

                return any_label_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        any_label = _parse_any_label(d.pop("any_label", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_brief(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        brief = _parse_brief(d.pop("brief", UNSET))

        project_ready_request = cls(
            project_id=project_id,
            labels=labels,
            any_label=any_label,
            profile_id=profile_id,
            brief=brief,
        )

        project_ready_request.additional_properties = d
        return project_ready_request

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
