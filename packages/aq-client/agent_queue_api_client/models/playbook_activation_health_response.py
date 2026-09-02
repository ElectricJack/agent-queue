from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.activation_state_dto import ActivationStateDTO
    from ..models.playbook_activation_health_response_by_health import PlaybookActivationHealthResponseByHealth


T = TypeVar("T", bound="PlaybookActivationHealthResponse")


@_attrs_define
class PlaybookActivationHealthResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        activations (list[ActivationStateDTO] | Unset):
        count (int | Unset):  Default: 0.
        by_health (PlaybookActivationHealthResponseByHealth | Unset):
    """

    success: bool | Unset = True
    activations: list[ActivationStateDTO] | Unset = UNSET
    count: int | Unset = 0
    by_health: PlaybookActivationHealthResponseByHealth | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        activations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.activations, Unset):
            activations = []
            for activations_item_data in self.activations:
                activations_item = activations_item_data.to_dict()
                activations.append(activations_item)

        count = self.count

        by_health: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_health, Unset):
            by_health = self.by_health.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if activations is not UNSET:
            field_dict["activations"] = activations
        if count is not UNSET:
            field_dict["count"] = count
        if by_health is not UNSET:
            field_dict["by_health"] = by_health

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.activation_state_dto import ActivationStateDTO
        from ..models.playbook_activation_health_response_by_health import PlaybookActivationHealthResponseByHealth

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _activations = d.pop("activations", UNSET)
        activations: list[ActivationStateDTO] | Unset = UNSET
        if _activations is not UNSET:
            activations = []
            for activations_item_data in _activations:
                activations_item = ActivationStateDTO.from_dict(activations_item_data)

                activations.append(activations_item)

        count = d.pop("count", UNSET)

        _by_health = d.pop("by_health", UNSET)
        by_health: PlaybookActivationHealthResponseByHealth | Unset
        if isinstance(_by_health, Unset):
            by_health = UNSET
        else:
            by_health = PlaybookActivationHealthResponseByHealth.from_dict(_by_health)

        playbook_activation_health_response = cls(
            success=success,
            activations=activations,
            count=count,
            by_health=by_health,
        )

        return playbook_activation_health_response
