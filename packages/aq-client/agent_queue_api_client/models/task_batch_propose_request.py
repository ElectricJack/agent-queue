from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskBatchProposeRequest")


@_attrs_define
class TaskBatchProposeRequest:
    """
    Attributes:
        source (str): Where the proposal came from (e.g. a spec path or playbook id). Recorded as provenance on every
            task the commit creates.
        tasks (list[Any]): The tasks to create. Must be non-empty.
        project_id (None | str | Unset): Project to propose into (defaults to the active one).
        edges (list[Any] | None | Unset): Dependency edges between the batch's tasks.
    """

    source: str
    tasks: list[Any]
    project_id: None | str | Unset = UNSET
    edges: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        tasks = self.tasks

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        edges: list[Any] | None | Unset
        if isinstance(self.edges, Unset):
            edges = UNSET
        elif isinstance(self.edges, list):
            edges = self.edges

        else:
            edges = self.edges

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source": source,
                "tasks": tasks,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if edges is not UNSET:
            field_dict["edges"] = edges

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        tasks = cast(list[Any], d.pop("tasks"))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_edges(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                edges_type_0 = cast(list[Any], data)

                return edges_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        edges = _parse_edges(d.pop("edges", UNSET))

        task_batch_propose_request = cls(
            source=source,
            tasks=tasks,
            project_id=project_id,
            edges=edges,
        )

        task_batch_propose_request.additional_properties = d
        return task_batch_propose_request

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
