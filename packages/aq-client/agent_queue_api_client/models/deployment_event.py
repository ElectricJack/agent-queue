from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeploymentEvent")


@_attrs_define
class DeploymentEvent:
    """
    Attributes:
        timestamp (float):
        outcome (str):
        branch (str | Unset):  Default: ''.
        previous (str | Unset):  Default: ''.
        target (str | Unset):  Default: ''.
        message (str | Unset):  Default: ''.
        automatic (bool | Unset):  Default: False.
    """

    timestamp: float
    outcome: str
    branch: str | Unset = ""
    previous: str | Unset = ""
    target: str | Unset = ""
    message: str | Unset = ""
    automatic: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        timestamp = self.timestamp

        outcome = self.outcome

        branch = self.branch

        previous = self.previous

        target = self.target

        message = self.message

        automatic = self.automatic

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "timestamp": timestamp,
                "outcome": outcome,
            }
        )
        if branch is not UNSET:
            field_dict["branch"] = branch
        if previous is not UNSET:
            field_dict["previous"] = previous
        if target is not UNSET:
            field_dict["target"] = target
        if message is not UNSET:
            field_dict["message"] = message
        if automatic is not UNSET:
            field_dict["automatic"] = automatic

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        timestamp = d.pop("timestamp")

        outcome = d.pop("outcome")

        branch = d.pop("branch", UNSET)

        previous = d.pop("previous", UNSET)

        target = d.pop("target", UNSET)

        message = d.pop("message", UNSET)

        automatic = d.pop("automatic", UNSET)

        deployment_event = cls(
            timestamp=timestamp,
            outcome=outcome,
            branch=branch,
            previous=previous,
            target=target,
            message=message,
            automatic=automatic,
        )

        deployment_event.additional_properties = d
        return deployment_event

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
