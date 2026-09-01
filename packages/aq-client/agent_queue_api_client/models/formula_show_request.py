from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.formula_show_request_vars_type_0 import FormulaShowRequestVarsType0


T = TypeVar("T", bound="FormulaShowRequest")


@_attrs_define
class FormulaShowRequest:
    """
    Attributes:
        name (None | str | Unset): Formula name
        project_id (None | str | Unset): Project scope (defaults to the active project)
        vars_ (FormulaShowRequestVarsType0 | None | Unset): Supplied var values, keyed by declared var name
        as_cooked (None | str | Unset): Container task id: render its formula_snapshot instead of resolving 'name' from
            the registry
    """

    name: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    vars_: FormulaShowRequestVarsType0 | None | Unset = UNSET
    as_cooked: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.formula_show_request_vars_type_0 import FormulaShowRequestVarsType0  # noqa: PLC0415

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        vars_: dict[str, Any] | None | Unset
        if isinstance(self.vars_, Unset):
            vars_ = UNSET
        elif isinstance(self.vars_, FormulaShowRequestVarsType0):
            vars_ = self.vars_.to_dict()
        else:
            vars_ = self.vars_

        as_cooked: None | str | Unset
        if isinstance(self.as_cooked, Unset):
            as_cooked = UNSET
        else:
            as_cooked = self.as_cooked

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if vars_ is not UNSET:
            field_dict["vars"] = vars_
        if as_cooked is not UNSET:
            field_dict["as_cooked"] = as_cooked

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.formula_show_request_vars_type_0 import FormulaShowRequestVarsType0  # noqa: PLC0415

        d = dict(src_dict)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_vars_(data: object) -> FormulaShowRequestVarsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                vars_type_0 = FormulaShowRequestVarsType0.from_dict(data)

                return vars_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FormulaShowRequestVarsType0 | None | Unset, data)

        vars_ = _parse_vars_(d.pop("vars", UNSET))

        def _parse_as_cooked(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        as_cooked = _parse_as_cooked(d.pop("as_cooked", UNSET))

        formula_show_request = cls(
            name=name,
            project_id=project_id,
            vars_=vars_,
            as_cooked=as_cooked,
        )

        formula_show_request.additional_properties = d
        return formula_show_request

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
