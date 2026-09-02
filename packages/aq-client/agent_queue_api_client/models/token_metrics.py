from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.token_metrics_by_model import TokenMetricsByModel


T = TypeVar("T", bound="TokenMetrics")


@_attrs_define
class TokenMetrics:
    """Rates over the trailing 60 seconds of the token ledger.

    ``unattributed_per_min`` is ledger volume that carried no input/output
    split — reported separately rather than folded into a model's rate, the
    same honesty rule ``get_costs`` applies to pricing.

        Attributes:
            input_per_min (float | Unset):  Default: 0.0.
            output_per_min (float | Unset):  Default: 0.0.
            total_per_min (float | Unset):  Default: 0.0.
            unattributed_per_min (float | Unset):  Default: 0.0.
            by_model (TokenMetricsByModel | Unset):
    """

    input_per_min: float | Unset = 0.0
    output_per_min: float | Unset = 0.0
    total_per_min: float | Unset = 0.0
    unattributed_per_min: float | Unset = 0.0
    by_model: TokenMetricsByModel | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        input_per_min = self.input_per_min

        output_per_min = self.output_per_min

        total_per_min = self.total_per_min

        unattributed_per_min = self.unattributed_per_min

        by_model: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_model, Unset):
            by_model = self.by_model.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if input_per_min is not UNSET:
            field_dict["input_per_min"] = input_per_min
        if output_per_min is not UNSET:
            field_dict["output_per_min"] = output_per_min
        if total_per_min is not UNSET:
            field_dict["total_per_min"] = total_per_min
        if unattributed_per_min is not UNSET:
            field_dict["unattributed_per_min"] = unattributed_per_min
        if by_model is not UNSET:
            field_dict["by_model"] = by_model

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.token_metrics_by_model import TokenMetricsByModel

        d = dict(src_dict)
        input_per_min = d.pop("input_per_min", UNSET)

        output_per_min = d.pop("output_per_min", UNSET)

        total_per_min = d.pop("total_per_min", UNSET)

        unattributed_per_min = d.pop("unattributed_per_min", UNSET)

        _by_model = d.pop("by_model", UNSET)
        by_model: TokenMetricsByModel | Unset
        if isinstance(_by_model, Unset):
            by_model = UNSET
        else:
            by_model = TokenMetricsByModel.from_dict(_by_model)

        token_metrics = cls(
            input_per_min=input_per_min,
            output_per_min=output_per_min,
            total_per_min=total_per_min,
            unattributed_per_min=unattributed_per_min,
            by_model=by_model,
        )

        token_metrics.additional_properties = d
        return token_metrics

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
