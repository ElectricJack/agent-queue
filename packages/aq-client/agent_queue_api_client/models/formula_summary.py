from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.formula_summary_vars import FormulaSummaryVars


T = TypeVar("T", bound="FormulaSummary")


@_attrs_define
class FormulaSummary:
    """One entry in ``formula_list``'s ``formulas`` array.

    Attributes:
        name (str):
        description (str | Unset):  Default: ''.
        scope (str | Unset):  Default: ''.
        extends (None | str | Unset):
        vars_ (FormulaSummaryVars | Unset):
        path (str | Unset):  Default: ''.
    """

    name: str
    description: str | Unset = ""
    scope: str | Unset = ""
    extends: None | str | Unset = UNSET
    vars_: FormulaSummaryVars | Unset = UNSET
    path: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        description = self.description

        scope = self.scope

        extends: None | str | Unset
        if isinstance(self.extends, Unset):
            extends = UNSET
        else:
            extends = self.extends

        vars_: dict[str, Any] | Unset = UNSET
        if not isinstance(self.vars_, Unset):
            vars_ = self.vars_.to_dict()

        path = self.path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if scope is not UNSET:
            field_dict["scope"] = scope
        if extends is not UNSET:
            field_dict["extends"] = extends
        if vars_ is not UNSET:
            field_dict["vars"] = vars_
        if path is not UNSET:
            field_dict["path"] = path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.formula_summary_vars import FormulaSummaryVars

        d = dict(src_dict)
        name = d.pop("name")

        description = d.pop("description", UNSET)

        scope = d.pop("scope", UNSET)

        def _parse_extends(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        extends = _parse_extends(d.pop("extends", UNSET))

        _vars_ = d.pop("vars", UNSET)
        vars_: FormulaSummaryVars | Unset
        if isinstance(_vars_, Unset):
            vars_ = UNSET
        else:
            vars_ = FormulaSummaryVars.from_dict(_vars_)

        path = d.pop("path", UNSET)

        formula_summary = cls(
            name=name,
            description=description,
            scope=scope,
            extends=extends,
            vars_=vars_,
            path=path,
        )

        formula_summary.additional_properties = d
        return formula_summary

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
