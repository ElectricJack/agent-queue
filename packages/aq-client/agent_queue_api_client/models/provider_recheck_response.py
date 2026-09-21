from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_availability_status import ProviderAvailabilityStatus
    from ..models.provider_recheck_response_probe_detail import ProviderRecheckResponseProbeDetail
    from ..models.provider_recheck_response_transition_type_0 import ProviderRecheckResponseTransitionType0


T = TypeVar("T", bound="ProviderRecheckResponse")


@_attrs_define
class ProviderRecheckResponse:
    """``provider_recheck``: the probe's answer and the resulting state.

    ``probe`` is ``authenticated``, ``not_authenticated``, ``cannot_tell`` or
    ``not_probeable`` (the provider has no login probe).

        Attributes:
            provider (str):
            probe (str):
            state (str):
            success (bool | Unset):  Default: True.
            probe_detail (ProviderRecheckResponseProbeDetail | Unset):
            transition (None | ProviderRecheckResponseTransitionType0 | Unset):
            status (None | ProviderAvailabilityStatus | Unset):
    """

    provider: str
    probe: str
    state: str
    success: bool | Unset = True
    probe_detail: ProviderRecheckResponseProbeDetail | Unset = UNSET
    transition: None | ProviderRecheckResponseTransitionType0 | Unset = UNSET
    status: None | ProviderAvailabilityStatus | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_availability_status import ProviderAvailabilityStatus
        from ..models.provider_recheck_response_transition_type_0 import ProviderRecheckResponseTransitionType0

        provider = self.provider

        probe = self.probe

        state = self.state

        success = self.success

        probe_detail: dict[str, Any] | Unset = UNSET
        if not isinstance(self.probe_detail, Unset):
            probe_detail = self.probe_detail.to_dict()

        transition: dict[str, Any] | None | Unset
        if isinstance(self.transition, Unset):
            transition = UNSET
        elif isinstance(self.transition, ProviderRecheckResponseTransitionType0):
            transition = self.transition.to_dict()
        else:
            transition = self.transition

        status: dict[str, Any] | None | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, ProviderAvailabilityStatus):
            status = self.status.to_dict()
        else:
            status = self.status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "probe": probe,
                "state": state,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if probe_detail is not UNSET:
            field_dict["probe_detail"] = probe_detail
        if transition is not UNSET:
            field_dict["transition"] = transition
        if status is not UNSET:
            field_dict["status"] = status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_availability_status import ProviderAvailabilityStatus
        from ..models.provider_recheck_response_probe_detail import ProviderRecheckResponseProbeDetail
        from ..models.provider_recheck_response_transition_type_0 import ProviderRecheckResponseTransitionType0

        d = dict(src_dict)
        provider = d.pop("provider")

        probe = d.pop("probe")

        state = d.pop("state")

        success = d.pop("success", UNSET)

        _probe_detail = d.pop("probe_detail", UNSET)
        probe_detail: ProviderRecheckResponseProbeDetail | Unset
        if isinstance(_probe_detail, Unset):
            probe_detail = UNSET
        else:
            probe_detail = ProviderRecheckResponseProbeDetail.from_dict(_probe_detail)

        def _parse_transition(data: object) -> None | ProviderRecheckResponseTransitionType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                transition_type_0 = ProviderRecheckResponseTransitionType0.from_dict(data)

                return transition_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderRecheckResponseTransitionType0 | Unset, data)

        transition = _parse_transition(d.pop("transition", UNSET))

        def _parse_status(data: object) -> None | ProviderAvailabilityStatus | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                status_type_0 = ProviderAvailabilityStatus.from_dict(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAvailabilityStatus | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        provider_recheck_response = cls(
            provider=provider,
            probe=probe,
            state=state,
            success=success,
            probe_detail=probe_detail,
            transition=transition,
            status=status,
        )

        provider_recheck_response.additional_properties = d
        return provider_recheck_response

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
