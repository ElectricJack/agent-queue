from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operator_preflight_action import OperatorPreflightAction
    from ..models.operator_preflight_inputs import OperatorPreflightInputs
    from ..models.operator_preflight_response_probes import OperatorPreflightResponseProbes


T = TypeVar("T", bound="OperatorPreflightResponse")


@_attrs_define
class OperatorPreflightResponse:
    """
    Attributes:
        preflight_id (int):
        ready (bool):
        inputs (OperatorPreflightInputs):
        success (bool | Unset):  Default: True.
        actions (list[OperatorPreflightAction] | Unset):
        blockers (list[str] | Unset):
        probes (OperatorPreflightResponseProbes | Unset):
        notification_error (None | str | Unset):
    """

    preflight_id: int
    ready: bool
    inputs: OperatorPreflightInputs
    success: bool | Unset = True
    actions: list[OperatorPreflightAction] | Unset = UNSET
    blockers: list[str] | Unset = UNSET
    probes: OperatorPreflightResponseProbes | Unset = UNSET
    notification_error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        preflight_id = self.preflight_id

        ready = self.ready

        inputs = self.inputs.to_dict()

        success = self.success

        actions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.actions, Unset):
            actions = []
            for actions_item_data in self.actions:
                actions_item = actions_item_data.to_dict()
                actions.append(actions_item)

        blockers: list[str] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = self.blockers

        probes: dict[str, Any] | Unset = UNSET
        if not isinstance(self.probes, Unset):
            probes = self.probes.to_dict()

        notification_error: None | str | Unset
        if isinstance(self.notification_error, Unset):
            notification_error = UNSET
        else:
            notification_error = self.notification_error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "preflight_id": preflight_id,
                "ready": ready,
                "inputs": inputs,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if actions is not UNSET:
            field_dict["actions"] = actions
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if probes is not UNSET:
            field_dict["probes"] = probes
        if notification_error is not UNSET:
            field_dict["notification_error"] = notification_error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operator_preflight_action import OperatorPreflightAction
        from ..models.operator_preflight_inputs import OperatorPreflightInputs
        from ..models.operator_preflight_response_probes import OperatorPreflightResponseProbes

        d = dict(src_dict)
        preflight_id = d.pop("preflight_id")

        ready = d.pop("ready")

        inputs = OperatorPreflightInputs.from_dict(d.pop("inputs"))

        success = d.pop("success", UNSET)

        _actions = d.pop("actions", UNSET)
        actions: list[OperatorPreflightAction] | Unset = UNSET
        if _actions is not UNSET:
            actions = []
            for actions_item_data in _actions:
                actions_item = OperatorPreflightAction.from_dict(actions_item_data)

                actions.append(actions_item)

        blockers = cast(list[str], d.pop("blockers", UNSET))

        _probes = d.pop("probes", UNSET)
        probes: OperatorPreflightResponseProbes | Unset
        if isinstance(_probes, Unset):
            probes = UNSET
        else:
            probes = OperatorPreflightResponseProbes.from_dict(_probes)

        def _parse_notification_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        notification_error = _parse_notification_error(d.pop("notification_error", UNSET))

        operator_preflight_response = cls(
            preflight_id=preflight_id,
            ready=ready,
            inputs=inputs,
            success=success,
            actions=actions,
            blockers=blockers,
            probes=probes,
            notification_error=notification_error,
        )

        operator_preflight_response.additional_properties = d
        return operator_preflight_response

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
