from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pool_status_row import PoolStatusRow


T = TypeVar("T", bound="PoolStatusResponse")


@_attrs_define
class PoolStatusResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        pools (list[PoolStatusRow] | Unset):
    """

    success: bool | Unset = True
    pools: list[PoolStatusRow] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        pools: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.pools, Unset):
            pools = []
            for pools_item_data in self.pools:
                pools_item = pools_item_data.to_dict()
                pools.append(pools_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if pools is not UNSET:
            field_dict["pools"] = pools

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pool_status_row import PoolStatusRow

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _pools = d.pop("pools", UNSET)
        pools: list[PoolStatusRow] | Unset = UNSET
        if _pools is not UNSET:
            pools = []
            for pools_item_data in _pools:
                pools_item = PoolStatusRow.from_dict(pools_item_data)

                pools.append(pools_item)

        pool_status_response = cls(
            success=success,
            pools=pools,
        )

        pool_status_response.additional_properties = d
        return pool_status_response

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
