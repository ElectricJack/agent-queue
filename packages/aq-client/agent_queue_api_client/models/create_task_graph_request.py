from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.create_task_graph_request_graph_type_0 import CreateTaskGraphRequestGraphType0


T = TypeVar("T", bound="CreateTaskGraphRequest")


@_attrs_define
class CreateTaskGraphRequest:
    """
    Attributes:
        profile_id (None | str | Unset): Default profile for graph nodes without an explicit profile.
        intelligence_class (None | str | Unset): Default intelligence class for graph nodes without an explicit class.
        project_id (None | str | Unset): Owning project
        graph (CreateTaskGraphRequestGraphType0 | None | Unset): Graph document (version/vars/defaults/parent/nodes)
        spec_path (None | str | Unset): Vault spec path whose fenced aq-graph block defines the graph, relative to the
            vault root (e.g. 'projects/<pid>/specs/x.md'). Paths that resolve outside the vault are refused.
        dry_run (bool | Unset): Validate and report assigned ids without writing Default: False.
        parent_id (None | str | Unset):
    """

    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    graph: CreateTaskGraphRequestGraphType0 | None | Unset = UNSET
    spec_path: None | str | Unset = UNSET
    dry_run: bool | Unset = False
    parent_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.create_task_graph_request_graph_type_0 import CreateTaskGraphRequestGraphType0  # noqa: PLC0415

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

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        graph: dict[str, Any] | None | Unset
        if isinstance(self.graph, Unset):
            graph = UNSET
        elif isinstance(self.graph, CreateTaskGraphRequestGraphType0):
            graph = self.graph.to_dict()
        else:
            graph = self.graph

        spec_path: None | str | Unset
        if isinstance(self.spec_path, Unset):
            spec_path = UNSET
        else:
            spec_path = self.spec_path

        dry_run = self.dry_run

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if graph is not UNSET:
            field_dict["graph"] = graph
        if spec_path is not UNSET:
            field_dict["spec_path"] = spec_path
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.create_task_graph_request_graph_type_0 import CreateTaskGraphRequestGraphType0  # noqa: PLC0415

        d = dict(src_dict)

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

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_graph(data: object) -> CreateTaskGraphRequestGraphType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                graph_type_0 = CreateTaskGraphRequestGraphType0.from_dict(data)

                return graph_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CreateTaskGraphRequestGraphType0 | None | Unset, data)

        graph = _parse_graph(d.pop("graph", UNSET))

        def _parse_spec_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        spec_path = _parse_spec_path(d.pop("spec_path", UNSET))

        dry_run = d.pop("dry_run", UNSET)

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        create_task_graph_request = cls(
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            project_id=project_id,
            graph=graph,
            spec_path=spec_path,
            dry_run=dry_run,
            parent_id=parent_id,
        )

        create_task_graph_request.additional_properties = d
        return create_task_graph_request

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
