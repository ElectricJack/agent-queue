from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.formula_summary import FormulaSummary


T = TypeVar("T", bound="FormulaListResponse")


@_attrs_define
class FormulaListResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        formulas (list[FormulaSummary] | Unset):
    """

    success: bool | Unset = True
    formulas: list[FormulaSummary] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        formulas: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.formulas, Unset):
            formulas = []
            for formulas_item_data in self.formulas:
                formulas_item = formulas_item_data.to_dict()
                formulas.append(formulas_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if formulas is not UNSET:
            field_dict["formulas"] = formulas

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.formula_summary import FormulaSummary  # noqa: PLC0415

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _formulas = d.pop("formulas", UNSET)
        formulas: list[FormulaSummary] | Unset = UNSET
        if _formulas is not UNSET:
            formulas = []
            for formulas_item_data in _formulas:
                formulas_item = FormulaSummary.from_dict(formulas_item_data)

                formulas.append(formulas_item)

        formula_list_response = cls(
            success=success,
            formulas=formulas,
        )

        formula_list_response.additional_properties = d
        return formula_list_response

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
