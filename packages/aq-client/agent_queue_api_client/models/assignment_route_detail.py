from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.assignment_route_detail_override_type_0 import AssignmentRouteDetailOverrideType0


T = TypeVar("T", bound="AssignmentRouteDetail")


@_attrs_define
class AssignmentRouteDetail:
    """A routed task's route, as ``aq task explain`` prints it (mandatory routing §10).

    Attributes:
        source (str):
        intelligence_class (str):
        freshness (str):
        provider (None | str | Unset):
        reason (None | str | Unset):
        playbook_id (None | str | Unset):
        playbook_version (int | None | Unset):
        playbook_run_id (None | str | Unset):
        profile_id (None | str | Unset):
        provider_intent (None | str | Unset):
        lane (None | str | Unset):
        rule (None | str | Unset):
        override (AssignmentRouteDetailOverrideType0 | None | Unset):
    """

    source: str
    intelligence_class: str
    freshness: str
    provider: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    playbook_id: None | str | Unset = UNSET
    playbook_version: int | None | Unset = UNSET
    playbook_run_id: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    provider_intent: None | str | Unset = UNSET
    lane: None | str | Unset = UNSET
    rule: None | str | Unset = UNSET
    override: AssignmentRouteDetailOverrideType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.assignment_route_detail_override_type_0 import AssignmentRouteDetailOverrideType0

        source = self.source

        intelligence_class = self.intelligence_class

        freshness = self.freshness

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        playbook_version: int | None | Unset
        if isinstance(self.playbook_version, Unset):
            playbook_version = UNSET
        else:
            playbook_version = self.playbook_version

        playbook_run_id: None | str | Unset
        if isinstance(self.playbook_run_id, Unset):
            playbook_run_id = UNSET
        else:
            playbook_run_id = self.playbook_run_id

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        provider_intent: None | str | Unset
        if isinstance(self.provider_intent, Unset):
            provider_intent = UNSET
        else:
            provider_intent = self.provider_intent

        lane: None | str | Unset
        if isinstance(self.lane, Unset):
            lane = UNSET
        else:
            lane = self.lane

        rule: None | str | Unset
        if isinstance(self.rule, Unset):
            rule = UNSET
        else:
            rule = self.rule

        override: dict[str, Any] | None | Unset
        if isinstance(self.override, Unset):
            override = UNSET
        elif isinstance(self.override, AssignmentRouteDetailOverrideType0):
            override = self.override.to_dict()
        else:
            override = self.override

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source": source,
                "intelligence_class": intelligence_class,
                "freshness": freshness,
            }
        )
        if provider is not UNSET:
            field_dict["provider"] = provider
        if reason is not UNSET:
            field_dict["reason"] = reason
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if playbook_version is not UNSET:
            field_dict["playbook_version"] = playbook_version
        if playbook_run_id is not UNSET:
            field_dict["playbook_run_id"] = playbook_run_id
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if provider_intent is not UNSET:
            field_dict["provider_intent"] = provider_intent
        if lane is not UNSET:
            field_dict["lane"] = lane
        if rule is not UNSET:
            field_dict["rule"] = rule
        if override is not UNSET:
            field_dict["override"] = override

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.assignment_route_detail_override_type_0 import AssignmentRouteDetailOverrideType0

        d = dict(src_dict)
        source = d.pop("source")

        intelligence_class = d.pop("intelligence_class")

        freshness = d.pop("freshness")

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_playbook_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        playbook_version = _parse_playbook_version(d.pop("playbook_version", UNSET))

        def _parse_playbook_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_run_id = _parse_playbook_run_id(d.pop("playbook_run_id", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_provider_intent(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider_intent = _parse_provider_intent(d.pop("provider_intent", UNSET))

        def _parse_lane(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lane = _parse_lane(d.pop("lane", UNSET))

        def _parse_rule(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rule = _parse_rule(d.pop("rule", UNSET))

        def _parse_override(data: object) -> AssignmentRouteDetailOverrideType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                override_type_0 = AssignmentRouteDetailOverrideType0.from_dict(data)

                return override_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AssignmentRouteDetailOverrideType0 | None | Unset, data)

        override = _parse_override(d.pop("override", UNSET))

        assignment_route_detail = cls(
            source=source,
            intelligence_class=intelligence_class,
            freshness=freshness,
            provider=provider,
            reason=reason,
            playbook_id=playbook_id,
            playbook_version=playbook_version,
            playbook_run_id=playbook_run_id,
            profile_id=profile_id,
            provider_intent=provider_intent,
            lane=lane,
            rule=rule,
            override=override,
        )

        assignment_route_detail.additional_properties = d
        return assignment_route_detail

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
