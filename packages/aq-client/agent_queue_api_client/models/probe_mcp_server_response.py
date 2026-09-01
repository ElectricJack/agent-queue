from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.catalog_entry_model import CatalogEntryModel


T = TypeVar("T", bound="ProbeMcpServerResponse")


@_attrs_define
class ProbeMcpServerResponse:
    """
    Attributes:
        probed (CatalogEntryModel):
    """

    probed: CatalogEntryModel
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        probed = self.probed.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "probed": probed,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.catalog_entry_model import CatalogEntryModel  # noqa: PLC0415

        d = dict(src_dict)
        probed = CatalogEntryModel.from_dict(d.pop("probed"))

        probe_mcp_server_response = cls(
            probed=probed,
        )

        probe_mcp_server_response.additional_properties = d
        return probe_mcp_server_response

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
