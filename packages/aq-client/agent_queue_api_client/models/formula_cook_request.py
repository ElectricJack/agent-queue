from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.formula_cook_request_vars_type_0 import FormulaCookRequestVarsType0


T = TypeVar("T", bound="FormulaCookRequest")


@_attrs_define
class FormulaCookRequest:
    """
    Attributes:
        name (str): Formula name
        project_id (str): Owning project
        vars_ (FormulaCookRequestVarsType0 | None | Unset): Supplied var values, keyed by declared var name
        parent_id (None | str | Unset): Cook the graph under an existing container instead of a new one
        dry_run (bool | Unset): Validate and report without writing Default: False.
    """

    name: str
    project_id: str
    vars_: FormulaCookRequestVarsType0 | None | Unset = UNSET
    parent_id: None | str | Unset = UNSET
    dry_run: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.formula_cook_request_vars_type_0 import FormulaCookRequestVarsType0  # noqa: PLC0415

        name = self.name

        project_id = self.project_id

        vars_: dict[str, Any] | None | Unset
        if isinstance(self.vars_, Unset):
            vars_ = UNSET
        elif isinstance(self.vars_, FormulaCookRequestVarsType0):
            vars_ = self.vars_.to_dict()
        else:
            vars_ = self.vars_

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        dry_run = self.dry_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "project_id": project_id,
            }
        )
        if vars_ is not UNSET:
            field_dict["vars"] = vars_
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.formula_cook_request_vars_type_0 import FormulaCookRequestVarsType0  # noqa: PLC0415

        d = dict(src_dict)
        name = d.pop("name")

        project_id = d.pop("project_id")

        def _parse_vars_(data: object) -> FormulaCookRequestVarsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                vars_type_0 = FormulaCookRequestVarsType0.from_dict(data)

                return vars_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FormulaCookRequestVarsType0 | None | Unset, data)

        vars_ = _parse_vars_(d.pop("vars", UNSET))

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        dry_run = d.pop("dry_run", UNSET)

        formula_cook_request = cls(
            name=name,
            project_id=project_id,
            vars_=vars_,
            parent_id=parent_id,
            dry_run=dry_run,
        )

        formula_cook_request.additional_properties = d
        return formula_cook_request

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
