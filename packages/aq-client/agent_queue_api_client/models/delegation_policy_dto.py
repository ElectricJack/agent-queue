from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="DelegationPolicyDTO")


@_attrs_define
class DelegationPolicyDTO:
    """AgentTaskStep only.

    Attributes:
        child_profile_id (str):
        wait_for_completion (bool | Unset):  Default: True.
        cancel_child (bool | Unset):  Default: False.
        narrowed_from (None | str | Unset):
    """

    child_profile_id: str
    wait_for_completion: bool | Unset = True
    cancel_child: bool | Unset = False
    narrowed_from: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        child_profile_id = self.child_profile_id

        wait_for_completion = self.wait_for_completion

        cancel_child = self.cancel_child

        narrowed_from: None | str | Unset
        if isinstance(self.narrowed_from, Unset):
            narrowed_from = UNSET
        else:
            narrowed_from = self.narrowed_from

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "child_profile_id": child_profile_id,
            }
        )
        if wait_for_completion is not UNSET:
            field_dict["wait_for_completion"] = wait_for_completion
        if cancel_child is not UNSET:
            field_dict["cancel_child"] = cancel_child
        if narrowed_from is not UNSET:
            field_dict["narrowed_from"] = narrowed_from

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        child_profile_id = d.pop("child_profile_id")

        wait_for_completion = d.pop("wait_for_completion", UNSET)

        cancel_child = d.pop("cancel_child", UNSET)

        def _parse_narrowed_from(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        narrowed_from = _parse_narrowed_from(d.pop("narrowed_from", UNSET))

        delegation_policy_dto = cls(
            child_profile_id=child_profile_id,
            wait_for_completion=wait_for_completion,
            cancel_child=cancel_child,
            narrowed_from=narrowed_from,
        )

        return delegation_policy_dto
