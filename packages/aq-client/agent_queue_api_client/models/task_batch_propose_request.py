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
        project_id (None | str | Unset): Project to propose into (defaults to the active one).
        dry_run (bool | None | Unset): Validate and return the diff without storing a proposal.
        edits (list[Any] | None | Unset): Existing task edits and controls. Live control changes are refused.
        remove_edges (list[Any] | None | Unset): Typed edges to remove: from, to, dep_type (default blocks).
        comments (list[Any] | None | Unset): Append comments to existing ids or tempIds. Author comes from the caller.
        tasks (list[Any] | None | Unset): Tasks to create with temporary ids; optional for edit-only change sets.
        edges (list[Any] | None | Unset): Dependency edges between the batch's tasks.
    """

    source: str
    project_id: None | str | Unset = UNSET
    dry_run: bool | None | Unset = UNSET
    edits: list[Any] | None | Unset = UNSET
    remove_edges: list[Any] | None | Unset = UNSET
    comments: list[Any] | None | Unset = UNSET
    tasks: list[Any] | None | Unset = UNSET
    edges: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        edits: list[Any] | None | Unset
        if isinstance(self.edits, Unset):
            edits = UNSET
        elif isinstance(self.edits, list):
            edits = self.edits

        else:
            edits = self.edits

        remove_edges: list[Any] | None | Unset
        if isinstance(self.remove_edges, Unset):
            remove_edges = UNSET
        elif isinstance(self.remove_edges, list):
            remove_edges = self.remove_edges

        else:
            remove_edges = self.remove_edges

        comments: list[Any] | None | Unset
        if isinstance(self.comments, Unset):
            comments = UNSET
        elif isinstance(self.comments, list):
            comments = self.comments

        else:
            comments = self.comments

        tasks: list[Any] | None | Unset
        if isinstance(self.tasks, Unset):
            tasks = UNSET
        elif isinstance(self.tasks, list):
            tasks = self.tasks

        else:
            tasks = self.tasks

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
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if edits is not UNSET:
            field_dict["edits"] = edits
        if remove_edges is not UNSET:
            field_dict["remove_edges"] = remove_edges
        if comments is not UNSET:
            field_dict["comments"] = comments
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if edges is not UNSET:
            field_dict["edges"] = edges

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        def _parse_edits(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                edits_type_0 = cast(list[Any], data)

                return edits_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        edits = _parse_edits(d.pop("edits", UNSET))

        def _parse_remove_edges(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                remove_edges_type_0 = cast(list[Any], data)

                return remove_edges_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        remove_edges = _parse_remove_edges(d.pop("remove_edges", UNSET))

        def _parse_comments(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                comments_type_0 = cast(list[Any], data)

                return comments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        comments = _parse_comments(d.pop("comments", UNSET))

        def _parse_tasks(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                tasks_type_0 = cast(list[Any], data)

                return tasks_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        tasks = _parse_tasks(d.pop("tasks", UNSET))

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
            project_id=project_id,
            dry_run=dry_run,
            edits=edits,
            remove_edges=remove_edges,
            comments=comments,
            tasks=tasks,
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
