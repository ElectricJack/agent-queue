from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.activation_state_dto import ActivationStateDTO


T = TypeVar("T", bound="SetPlaybookActivationResponse")


@_attrs_define
class SetPlaybookActivationResponse:
    """
    Attributes:
        activation (ActivationStateDTO): ``enabled`` and ``health`` are independent (design spec).  A disabled
            activation still reports its computed health; ``health="disabled"`` is used
            only when there is no active artifact at all.
        success (bool | Unset):  Default: True.
        previous_artifact_sha256 (None | str | Unset):
        changed (bool | Unset):  Default: False.
        blocked (bool | Unset):  Default: False.
        blockers (list[str] | Unset):
    """

    activation: ActivationStateDTO
    success: bool | Unset = True
    previous_artifact_sha256: None | str | Unset = UNSET
    changed: bool | Unset = False
    blocked: bool | Unset = False
    blockers: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        activation = self.activation.to_dict()

        success = self.success

        previous_artifact_sha256: None | str | Unset
        if isinstance(self.previous_artifact_sha256, Unset):
            previous_artifact_sha256 = UNSET
        else:
            previous_artifact_sha256 = self.previous_artifact_sha256

        changed = self.changed

        blocked = self.blocked

        blockers: list[str] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = self.blockers

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "activation": activation,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if previous_artifact_sha256 is not UNSET:
            field_dict["previous_artifact_sha256"] = previous_artifact_sha256
        if changed is not UNSET:
            field_dict["changed"] = changed
        if blocked is not UNSET:
            field_dict["blocked"] = blocked
        if blockers is not UNSET:
            field_dict["blockers"] = blockers

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.activation_state_dto import ActivationStateDTO

        d = dict(src_dict)
        activation = ActivationStateDTO.from_dict(d.pop("activation"))

        success = d.pop("success", UNSET)

        def _parse_previous_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        previous_artifact_sha256 = _parse_previous_artifact_sha256(d.pop("previous_artifact_sha256", UNSET))

        changed = d.pop("changed", UNSET)

        blocked = d.pop("blocked", UNSET)

        blockers = cast(list[str], d.pop("blockers", UNSET))

        set_playbook_activation_response = cls(
            activation=activation,
            success=success,
            previous_artifact_sha256=previous_artifact_sha256,
            changed=changed,
            blocked=blocked,
            blockers=blockers,
        )

        return set_playbook_activation_response
