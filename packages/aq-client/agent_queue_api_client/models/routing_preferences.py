from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.routing_preferences_policy_type_0 import RoutingPreferencesPolicyType0


T = TypeVar("T", bound="RoutingPreferences")


@_attrs_define
class RoutingPreferences:
    """
    Attributes:
        assignment_playbook_id (str):
        type_ (Literal['routing'] | Unset):  Default: 'routing'.
        policy (None | RoutingPreferencesPolicyType0 | Unset):
    """

    assignment_playbook_id: str
    type_: Literal["routing"] | Unset = "routing"
    policy: None | RoutingPreferencesPolicyType0 | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.routing_preferences_policy_type_0 import RoutingPreferencesPolicyType0

        assignment_playbook_id = self.assignment_playbook_id

        type_ = self.type_

        policy: dict[str, Any] | None | Unset
        if isinstance(self.policy, Unset):
            policy = UNSET
        elif isinstance(self.policy, RoutingPreferencesPolicyType0):
            policy = self.policy.to_dict()
        else:
            policy = self.policy

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "assignment_playbook_id": assignment_playbook_id,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_
        if policy is not UNSET:
            field_dict["policy"] = policy

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.routing_preferences_policy_type_0 import RoutingPreferencesPolicyType0

        d = dict(src_dict)
        assignment_playbook_id = d.pop("assignment_playbook_id")

        type_ = cast(Literal["routing"] | Unset, d.pop("type", UNSET))
        if type_ != "routing" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'routing', got '{type_}'")

        def _parse_policy(data: object) -> None | RoutingPreferencesPolicyType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                policy_type_0 = RoutingPreferencesPolicyType0.from_dict(data)

                return policy_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RoutingPreferencesPolicyType0 | Unset, data)

        policy = _parse_policy(d.pop("policy", UNSET))

        routing_preferences = cls(
            assignment_playbook_id=assignment_playbook_id,
            type_=type_,
            policy=policy,
        )

        return routing_preferences
