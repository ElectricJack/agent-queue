from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_availability_status import ProviderAvailabilityStatus


T = TypeVar("T", bound="ProviderStatusResponse")


@_attrs_define
class ProviderStatusResponse:
    """``provider_status`` and ``GET /api/providers/availability``.

    Attributes:
        now (float):
        success (bool | Unset):  Default: True.
        mode (str | Unset):  Default: 'enforce'.
        providers (list[ProviderAvailabilityStatus] | Unset):
    """

    now: float
    success: bool | Unset = True
    mode: str | Unset = "enforce"
    providers: list[ProviderAvailabilityStatus] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        now = self.now

        success = self.success

        mode = self.mode

        providers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.providers, Unset):
            providers = []
            for providers_item_data in self.providers:
                providers_item = providers_item_data.to_dict()
                providers.append(providers_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "now": now,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if mode is not UNSET:
            field_dict["mode"] = mode
        if providers is not UNSET:
            field_dict["providers"] = providers

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_availability_status import ProviderAvailabilityStatus

        d = dict(src_dict)
        now = d.pop("now")

        success = d.pop("success", UNSET)

        mode = d.pop("mode", UNSET)

        _providers = d.pop("providers", UNSET)
        providers: list[ProviderAvailabilityStatus] | Unset = UNSET
        if _providers is not UNSET:
            providers = []
            for providers_item_data in _providers:
                providers_item = ProviderAvailabilityStatus.from_dict(providers_item_data)

                providers.append(providers_item)

        provider_status_response = cls(
            now=now,
            success=success,
            mode=mode,
            providers=providers,
        )

        provider_status_response.additional_properties = d
        return provider_status_response

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
