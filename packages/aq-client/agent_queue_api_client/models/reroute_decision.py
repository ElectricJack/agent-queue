from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RerouteDecision")


@_attrs_define
class RerouteDecision:
    """What one re-route sweep did (or would do) with one task (D12-D15).

    ``action`` is ``move``, ``hold`` or ``skip``; ``kind`` names why a held
    task is not moving (``provider_pinned``, ``no_equivalent_rung``,
    ``awaiting_failover_capacity`` with ``ahead``, ...).

        Attributes:
            task_id (str):
            project_id (str):
            from_profile_id (str):
            action (str):
            from_provider (str | Unset):  Default: ''.
            provider_state (str | Unset):  Default: ''.
            kind (None | str | Unset):
            to_profile_id (None | str | Unset):
            to_provider (None | str | Unset):
            to_class (None | str | Unset):
            ahead (int | None | Unset):
            detail (str | Unset):  Default: ''.
            resume (bool | Unset):  Default: False.
            intelligence_class (None | str | Unset):
            intent (str | Unset):  Default: 'class_only'.
            priority (int | Unset):  Default: 100.
            title (str | Unset):  Default: ''.
            status (str | Unset):  Default: ''.
            provider_generation (int | None | Unset):
    """

    task_id: str
    project_id: str
    from_profile_id: str
    action: str
    from_provider: str | Unset = ""
    provider_state: str | Unset = ""
    kind: None | str | Unset = UNSET
    to_profile_id: None | str | Unset = UNSET
    to_provider: None | str | Unset = UNSET
    to_class: None | str | Unset = UNSET
    ahead: int | None | Unset = UNSET
    detail: str | Unset = ""
    resume: bool | Unset = False
    intelligence_class: None | str | Unset = UNSET
    intent: str | Unset = "class_only"
    priority: int | Unset = 100
    title: str | Unset = ""
    status: str | Unset = ""
    provider_generation: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        project_id = self.project_id

        from_profile_id = self.from_profile_id

        action = self.action

        from_provider = self.from_provider

        provider_state = self.provider_state

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        else:
            kind = self.kind

        to_profile_id: None | str | Unset
        if isinstance(self.to_profile_id, Unset):
            to_profile_id = UNSET
        else:
            to_profile_id = self.to_profile_id

        to_provider: None | str | Unset
        if isinstance(self.to_provider, Unset):
            to_provider = UNSET
        else:
            to_provider = self.to_provider

        to_class: None | str | Unset
        if isinstance(self.to_class, Unset):
            to_class = UNSET
        else:
            to_class = self.to_class

        ahead: int | None | Unset
        if isinstance(self.ahead, Unset):
            ahead = UNSET
        else:
            ahead = self.ahead

        detail = self.detail

        resume = self.resume

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        intent = self.intent

        priority = self.priority

        title = self.title

        status = self.status

        provider_generation: int | None | Unset
        if isinstance(self.provider_generation, Unset):
            provider_generation = UNSET
        else:
            provider_generation = self.provider_generation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "project_id": project_id,
                "from_profile_id": from_profile_id,
                "action": action,
            }
        )
        if from_provider is not UNSET:
            field_dict["from_provider"] = from_provider
        if provider_state is not UNSET:
            field_dict["provider_state"] = provider_state
        if kind is not UNSET:
            field_dict["kind"] = kind
        if to_profile_id is not UNSET:
            field_dict["to_profile_id"] = to_profile_id
        if to_provider is not UNSET:
            field_dict["to_provider"] = to_provider
        if to_class is not UNSET:
            field_dict["to_class"] = to_class
        if ahead is not UNSET:
            field_dict["ahead"] = ahead
        if detail is not UNSET:
            field_dict["detail"] = detail
        if resume is not UNSET:
            field_dict["resume"] = resume
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if intent is not UNSET:
            field_dict["intent"] = intent
        if priority is not UNSET:
            field_dict["priority"] = priority
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status
        if provider_generation is not UNSET:
            field_dict["provider_generation"] = provider_generation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        project_id = d.pop("project_id")

        from_profile_id = d.pop("from_profile_id")

        action = d.pop("action")

        from_provider = d.pop("from_provider", UNSET)

        provider_state = d.pop("provider_state", UNSET)

        def _parse_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        def _parse_to_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_profile_id = _parse_to_profile_id(d.pop("to_profile_id", UNSET))

        def _parse_to_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_provider = _parse_to_provider(d.pop("to_provider", UNSET))

        def _parse_to_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_class = _parse_to_class(d.pop("to_class", UNSET))

        def _parse_ahead(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        ahead = _parse_ahead(d.pop("ahead", UNSET))

        detail = d.pop("detail", UNSET)

        resume = d.pop("resume", UNSET)

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        intent = d.pop("intent", UNSET)

        priority = d.pop("priority", UNSET)

        title = d.pop("title", UNSET)

        status = d.pop("status", UNSET)

        def _parse_provider_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        provider_generation = _parse_provider_generation(d.pop("provider_generation", UNSET))

        reroute_decision = cls(
            task_id=task_id,
            project_id=project_id,
            from_profile_id=from_profile_id,
            action=action,
            from_provider=from_provider,
            provider_state=provider_state,
            kind=kind,
            to_profile_id=to_profile_id,
            to_provider=to_provider,
            to_class=to_class,
            ahead=ahead,
            detail=detail,
            resume=resume,
            intelligence_class=intelligence_class,
            intent=intent,
            priority=priority,
            title=title,
            status=status,
            provider_generation=provider_generation,
        )

        reroute_decision.additional_properties = d
        return reroute_decision

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
