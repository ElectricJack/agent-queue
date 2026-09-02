from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AgentMessageRequest")


@_attrs_define
class AgentMessageRequest:
    """
    Attributes:
        body (str): Message body
        target (None | str | Unset): Task, agent, or session id
        all_running (bool | Unset):  Default: False.
        profile (None | str | Unset): Optional profile filter for broadcast
        wait (int | None | Unset): Wait up to 60 seconds for delivery
    """

    body: str
    target: None | str | Unset = UNSET
    all_running: bool | Unset = False
    profile: None | str | Unset = UNSET
    wait: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        body = self.body

        target: None | str | Unset
        if isinstance(self.target, Unset):
            target = UNSET
        else:
            target = self.target

        all_running = self.all_running

        profile: None | str | Unset
        if isinstance(self.profile, Unset):
            profile = UNSET
        else:
            profile = self.profile

        wait: int | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        else:
            wait = self.wait

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "body": body,
            }
        )
        if target is not UNSET:
            field_dict["target"] = target
        if all_running is not UNSET:
            field_dict["all_running"] = all_running
        if profile is not UNSET:
            field_dict["profile"] = profile
        if wait is not UNSET:
            field_dict["wait"] = wait

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        body = d.pop("body")

        def _parse_target(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target = _parse_target(d.pop("target", UNSET))

        all_running = d.pop("all_running", UNSET)

        def _parse_profile(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile = _parse_profile(d.pop("profile", UNSET))

        def _parse_wait(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        agent_message_request = cls(
            body=body,
            target=target,
            all_running=all_running,
            profile=profile,
            wait=wait,
        )

        agent_message_request.additional_properties = d
        return agent_message_request

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
