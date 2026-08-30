from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.formula_cook_response_errors_item import FormulaCookResponseErrorsItem
    from ..models.formula_cook_response_nodes_item import FormulaCookResponseNodesItem
    from ..models.formula_cook_response_provenance_type_0 import FormulaCookResponseProvenanceType0
    from ..models.formula_cook_response_warnings_item import FormulaCookResponseWarningsItem


T = TypeVar("T", bound="FormulaCookResponse")


@_attrs_define
class FormulaCookResponse:
    """``formula_cook`` shares ``create_task_graph``'s build_report envelope
    (``parent_id``/``nodes``/``dry_run``/...) plus formula-specific fields
    (``container_id``, ``provenance``) and an error envelope on failure.

        Attributes:
            success (bool):
            error (None | str | Unset):
            errors (list[FormulaCookResponseErrorsItem] | Unset):
            warnings (list[FormulaCookResponseWarningsItem] | Unset):
            container_id (None | str | Unset):
            project_id (None | str | Unset):
            parent_id (None | str | Unset):
            parent_title (None | str | Unset):
            provisional (bool | None | Unset):
            task_ids (list[str] | Unset):
            nodes (list[FormulaCookResponseNodesItem] | Unset):
            dependency_count (int | None | Unset):
            context_count (int | None | Unset):
            dry_run (bool | None | Unset):
            created (bool | None | Unset):
            provenance (FormulaCookResponseProvenanceType0 | None | Unset):
    """

    success: bool
    error: None | str | Unset = UNSET
    errors: list[FormulaCookResponseErrorsItem] | Unset = UNSET
    warnings: list[FormulaCookResponseWarningsItem] | Unset = UNSET
    container_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    parent_id: None | str | Unset = UNSET
    parent_title: None | str | Unset = UNSET
    provisional: bool | None | Unset = UNSET
    task_ids: list[str] | Unset = UNSET
    nodes: list[FormulaCookResponseNodesItem] | Unset = UNSET
    dependency_count: int | None | Unset = UNSET
    context_count: int | None | Unset = UNSET
    dry_run: bool | None | Unset = UNSET
    created: bool | None | Unset = UNSET
    provenance: FormulaCookResponseProvenanceType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.formula_cook_response_provenance_type_0 import FormulaCookResponseProvenanceType0

        success = self.success

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        warnings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)

        container_id: None | str | Unset
        if isinstance(self.container_id, Unset):
            container_id = UNSET
        else:
            container_id = self.container_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        parent_title: None | str | Unset
        if isinstance(self.parent_title, Unset):
            parent_title = UNSET
        else:
            parent_title = self.parent_title

        provisional: bool | None | Unset
        if isinstance(self.provisional, Unset):
            provisional = UNSET
        else:
            provisional = self.provisional

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        nodes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.nodes, Unset):
            nodes = []
            for nodes_item_data in self.nodes:
                nodes_item = nodes_item_data.to_dict()
                nodes.append(nodes_item)

        dependency_count: int | None | Unset
        if isinstance(self.dependency_count, Unset):
            dependency_count = UNSET
        else:
            dependency_count = self.dependency_count

        context_count: int | None | Unset
        if isinstance(self.context_count, Unset):
            context_count = UNSET
        else:
            context_count = self.context_count

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        created: bool | None | Unset
        if isinstance(self.created, Unset):
            created = UNSET
        else:
            created = self.created

        provenance: dict[str, Any] | None | Unset
        if isinstance(self.provenance, Unset):
            provenance = UNSET
        elif isinstance(self.provenance, FormulaCookResponseProvenanceType0):
            provenance = self.provenance.to_dict()
        else:
            provenance = self.provenance

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if error is not UNSET:
            field_dict["error"] = error
        if errors is not UNSET:
            field_dict["errors"] = errors
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if container_id is not UNSET:
            field_dict["container_id"] = container_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if parent_title is not UNSET:
            field_dict["parent_title"] = parent_title
        if provisional is not UNSET:
            field_dict["provisional"] = provisional
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if dependency_count is not UNSET:
            field_dict["dependency_count"] = dependency_count
        if context_count is not UNSET:
            field_dict["context_count"] = context_count
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if created is not UNSET:
            field_dict["created"] = created
        if provenance is not UNSET:
            field_dict["provenance"] = provenance

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.formula_cook_response_errors_item import FormulaCookResponseErrorsItem
        from ..models.formula_cook_response_nodes_item import FormulaCookResponseNodesItem
        from ..models.formula_cook_response_provenance_type_0 import FormulaCookResponseProvenanceType0
        from ..models.formula_cook_response_warnings_item import FormulaCookResponseWarningsItem

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        _errors = d.pop("errors", UNSET)
        errors: list[FormulaCookResponseErrorsItem] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = FormulaCookResponseErrorsItem.from_dict(errors_item_data)

                errors.append(errors_item)

        _warnings = d.pop("warnings", UNSET)
        warnings: list[FormulaCookResponseWarningsItem] | Unset = UNSET
        if _warnings is not UNSET:
            warnings = []
            for warnings_item_data in _warnings:
                warnings_item = FormulaCookResponseWarningsItem.from_dict(warnings_item_data)

                warnings.append(warnings_item)

        def _parse_container_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        container_id = _parse_container_id(d.pop("container_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        def _parse_parent_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_title = _parse_parent_title(d.pop("parent_title", UNSET))

        def _parse_provisional(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        provisional = _parse_provisional(d.pop("provisional", UNSET))

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        _nodes = d.pop("nodes", UNSET)
        nodes: list[FormulaCookResponseNodesItem] | Unset = UNSET
        if _nodes is not UNSET:
            nodes = []
            for nodes_item_data in _nodes:
                nodes_item = FormulaCookResponseNodesItem.from_dict(nodes_item_data)

                nodes.append(nodes_item)

        def _parse_dependency_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        dependency_count = _parse_dependency_count(d.pop("dependency_count", UNSET))

        def _parse_context_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        context_count = _parse_context_count(d.pop("context_count", UNSET))

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        def _parse_created(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        created = _parse_created(d.pop("created", UNSET))

        def _parse_provenance(data: object) -> FormulaCookResponseProvenanceType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                provenance_type_0 = FormulaCookResponseProvenanceType0.from_dict(data)

                return provenance_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FormulaCookResponseProvenanceType0 | None | Unset, data)

        provenance = _parse_provenance(d.pop("provenance", UNSET))

        formula_cook_response = cls(
            success=success,
            error=error,
            errors=errors,
            warnings=warnings,
            container_id=container_id,
            project_id=project_id,
            parent_id=parent_id,
            parent_title=parent_title,
            provisional=provisional,
            task_ids=task_ids,
            nodes=nodes,
            dependency_count=dependency_count,
            context_count=context_count,
            dry_run=dry_run,
            created=created,
            provenance=provenance,
        )

        formula_cook_response.additional_properties = d
        return formula_cook_response

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
