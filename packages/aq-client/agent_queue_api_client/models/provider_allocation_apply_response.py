from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_applied_preference import ProviderAllocationAppliedPreference
    from ..models.provider_allocation_applied_profile import ProviderAllocationAppliedProfile
    from ..models.provider_allocation_ceiling_change import ProviderAllocationCeilingChange
    from ..models.provider_allocation_pinned_task import ProviderAllocationPinnedTask
    from ..models.provider_allocation_preview_response import ProviderAllocationPreviewResponse
    from ..models.provider_allocation_push_change import ProviderAllocationPushChange
    from ..models.provider_allocation_request import ProviderAllocationRequest
    from ..models.provider_allocation_session_action import ProviderAllocationSessionAction
    from ..models.provider_allocation_warning import ProviderAllocationWarning


T = TypeVar("T", bound="ProviderAllocationApplyResponse")


@_attrs_define
class ProviderAllocationApplyResponse:
    """``provider_allocation_apply`` and ``POST /api/providers/allocation/apply``.

    ``success`` only when ``status`` is ``applied``.  A refusal before any
    change carries ``error_code`` (``preview_unknown``, ``preview_stale``,
    ``pinned_wait_unacknowledged``, ``busy_authorization_required`` /
    ``_mismatch`` / ``_unexpected``) and the current ``preview`` when there
    is one; an apply that failed part-way carries ``status`` ``rolled_back``
    or ``partial`` with every row.

        Attributes:
            success (bool | Unset):  Default: True.
            status (None | str | Unset):
            error (None | str | Unset):
            error_code (None | str | Unset):
            preview (None | ProviderAllocationPreviewResponse | Unset):
            request_id (None | str | Unset):
            event_id (int | None | Unset):
            provider (None | str | Unset):
            vendor (str | Unset):  Default: ''.
            actor (None | str | Unset):
            preview_token (None | str | Unset):
            request (None | ProviderAllocationRequest | Unset):
            profiles (list[ProviderAllocationAppliedProfile] | Unset):
            ceiling (None | ProviderAllocationCeilingChange | Unset):
            preference (None | ProviderAllocationAppliedPreference | Unset):
            session_actions (list[ProviderAllocationSessionAction] | Unset):
            pinned (list[ProviderAllocationPinnedTask] | Unset):
            manual_agents (list[ProviderAllocationPushChange] | Unset):
            warnings (list[ProviderAllocationWarning] | Unset):
    """

    success: bool | Unset = True
    status: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    preview: None | ProviderAllocationPreviewResponse | Unset = UNSET
    request_id: None | str | Unset = UNSET
    event_id: int | None | Unset = UNSET
    provider: None | str | Unset = UNSET
    vendor: str | Unset = ""
    actor: None | str | Unset = UNSET
    preview_token: None | str | Unset = UNSET
    request: None | ProviderAllocationRequest | Unset = UNSET
    profiles: list[ProviderAllocationAppliedProfile] | Unset = UNSET
    ceiling: None | ProviderAllocationCeilingChange | Unset = UNSET
    preference: None | ProviderAllocationAppliedPreference | Unset = UNSET
    session_actions: list[ProviderAllocationSessionAction] | Unset = UNSET
    pinned: list[ProviderAllocationPinnedTask] | Unset = UNSET
    manual_agents: list[ProviderAllocationPushChange] | Unset = UNSET
    warnings: list[ProviderAllocationWarning] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_allocation_applied_preference import ProviderAllocationAppliedPreference
        from ..models.provider_allocation_ceiling_change import ProviderAllocationCeilingChange
        from ..models.provider_allocation_preview_response import ProviderAllocationPreviewResponse
        from ..models.provider_allocation_request import ProviderAllocationRequest

        success = self.success

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        preview: dict[str, Any] | None | Unset
        if isinstance(self.preview, Unset):
            preview = UNSET
        elif isinstance(self.preview, ProviderAllocationPreviewResponse):
            preview = self.preview.to_dict()
        else:
            preview = self.preview

        request_id: None | str | Unset
        if isinstance(self.request_id, Unset):
            request_id = UNSET
        else:
            request_id = self.request_id

        event_id: int | None | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        vendor = self.vendor

        actor: None | str | Unset
        if isinstance(self.actor, Unset):
            actor = UNSET
        else:
            actor = self.actor

        preview_token: None | str | Unset
        if isinstance(self.preview_token, Unset):
            preview_token = UNSET
        else:
            preview_token = self.preview_token

        request: dict[str, Any] | None | Unset
        if isinstance(self.request, Unset):
            request = UNSET
        elif isinstance(self.request, ProviderAllocationRequest):
            request = self.request.to_dict()
        else:
            request = self.request

        profiles: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.profiles, Unset):
            profiles = []
            for profiles_item_data in self.profiles:
                profiles_item = profiles_item_data.to_dict()
                profiles.append(profiles_item)

        ceiling: dict[str, Any] | None | Unset
        if isinstance(self.ceiling, Unset):
            ceiling = UNSET
        elif isinstance(self.ceiling, ProviderAllocationCeilingChange):
            ceiling = self.ceiling.to_dict()
        else:
            ceiling = self.ceiling

        preference: dict[str, Any] | None | Unset
        if isinstance(self.preference, Unset):
            preference = UNSET
        elif isinstance(self.preference, ProviderAllocationAppliedPreference):
            preference = self.preference.to_dict()
        else:
            preference = self.preference

        session_actions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.session_actions, Unset):
            session_actions = []
            for session_actions_item_data in self.session_actions:
                session_actions_item = session_actions_item_data.to_dict()
                session_actions.append(session_actions_item)

        pinned: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.pinned, Unset):
            pinned = []
            for pinned_item_data in self.pinned:
                pinned_item = pinned_item_data.to_dict()
                pinned.append(pinned_item)

        manual_agents: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.manual_agents, Unset):
            manual_agents = []
            for manual_agents_item_data in self.manual_agents:
                manual_agents_item = manual_agents_item_data.to_dict()
                manual_agents.append(manual_agents_item)

        warnings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if status is not UNSET:
            field_dict["status"] = status
        if error is not UNSET:
            field_dict["error"] = error
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if preview is not UNSET:
            field_dict["preview"] = preview
        if request_id is not UNSET:
            field_dict["request_id"] = request_id
        if event_id is not UNSET:
            field_dict["event_id"] = event_id
        if provider is not UNSET:
            field_dict["provider"] = provider
        if vendor is not UNSET:
            field_dict["vendor"] = vendor
        if actor is not UNSET:
            field_dict["actor"] = actor
        if preview_token is not UNSET:
            field_dict["preview_token"] = preview_token
        if request is not UNSET:
            field_dict["request"] = request
        if profiles is not UNSET:
            field_dict["profiles"] = profiles
        if ceiling is not UNSET:
            field_dict["ceiling"] = ceiling
        if preference is not UNSET:
            field_dict["preference"] = preference
        if session_actions is not UNSET:
            field_dict["session_actions"] = session_actions
        if pinned is not UNSET:
            field_dict["pinned"] = pinned
        if manual_agents is not UNSET:
            field_dict["manual_agents"] = manual_agents
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_applied_preference import ProviderAllocationAppliedPreference
        from ..models.provider_allocation_applied_profile import ProviderAllocationAppliedProfile
        from ..models.provider_allocation_ceiling_change import ProviderAllocationCeilingChange
        from ..models.provider_allocation_pinned_task import ProviderAllocationPinnedTask
        from ..models.provider_allocation_preview_response import ProviderAllocationPreviewResponse
        from ..models.provider_allocation_push_change import ProviderAllocationPushChange
        from ..models.provider_allocation_request import ProviderAllocationRequest
        from ..models.provider_allocation_session_action import ProviderAllocationSessionAction
        from ..models.provider_allocation_warning import ProviderAllocationWarning

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        def _parse_preview(data: object) -> None | ProviderAllocationPreviewResponse | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preview_type_0 = ProviderAllocationPreviewResponse.from_dict(data)

                return preview_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationPreviewResponse | Unset, data)

        preview = _parse_preview(d.pop("preview", UNSET))

        def _parse_request_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_id = _parse_request_id(d.pop("request_id", UNSET))

        def _parse_event_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        vendor = d.pop("vendor", UNSET)

        def _parse_actor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        actor = _parse_actor(d.pop("actor", UNSET))

        def _parse_preview_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        preview_token = _parse_preview_token(d.pop("preview_token", UNSET))

        def _parse_request(data: object) -> None | ProviderAllocationRequest | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                request_type_0 = ProviderAllocationRequest.from_dict(data)

                return request_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationRequest | Unset, data)

        request = _parse_request(d.pop("request", UNSET))

        _profiles = d.pop("profiles", UNSET)
        profiles: list[ProviderAllocationAppliedProfile] | Unset = UNSET
        if _profiles is not UNSET:
            profiles = []
            for profiles_item_data in _profiles:
                profiles_item = ProviderAllocationAppliedProfile.from_dict(profiles_item_data)

                profiles.append(profiles_item)

        def _parse_ceiling(data: object) -> None | ProviderAllocationCeilingChange | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                ceiling_type_0 = ProviderAllocationCeilingChange.from_dict(data)

                return ceiling_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationCeilingChange | Unset, data)

        ceiling = _parse_ceiling(d.pop("ceiling", UNSET))

        def _parse_preference(data: object) -> None | ProviderAllocationAppliedPreference | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preference_type_0 = ProviderAllocationAppliedPreference.from_dict(data)

                return preference_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationAppliedPreference | Unset, data)

        preference = _parse_preference(d.pop("preference", UNSET))

        _session_actions = d.pop("session_actions", UNSET)
        session_actions: list[ProviderAllocationSessionAction] | Unset = UNSET
        if _session_actions is not UNSET:
            session_actions = []
            for session_actions_item_data in _session_actions:
                session_actions_item = ProviderAllocationSessionAction.from_dict(session_actions_item_data)

                session_actions.append(session_actions_item)

        _pinned = d.pop("pinned", UNSET)
        pinned: list[ProviderAllocationPinnedTask] | Unset = UNSET
        if _pinned is not UNSET:
            pinned = []
            for pinned_item_data in _pinned:
                pinned_item = ProviderAllocationPinnedTask.from_dict(pinned_item_data)

                pinned.append(pinned_item)

        _manual_agents = d.pop("manual_agents", UNSET)
        manual_agents: list[ProviderAllocationPushChange] | Unset = UNSET
        if _manual_agents is not UNSET:
            manual_agents = []
            for manual_agents_item_data in _manual_agents:
                manual_agents_item = ProviderAllocationPushChange.from_dict(manual_agents_item_data)

                manual_agents.append(manual_agents_item)

        _warnings = d.pop("warnings", UNSET)
        warnings: list[ProviderAllocationWarning] | Unset = UNSET
        if _warnings is not UNSET:
            warnings = []
            for warnings_item_data in _warnings:
                warnings_item = ProviderAllocationWarning.from_dict(warnings_item_data)

                warnings.append(warnings_item)

        provider_allocation_apply_response = cls(
            success=success,
            status=status,
            error=error,
            error_code=error_code,
            preview=preview,
            request_id=request_id,
            event_id=event_id,
            provider=provider,
            vendor=vendor,
            actor=actor,
            preview_token=preview_token,
            request=request,
            profiles=profiles,
            ceiling=ceiling,
            preference=preference,
            session_actions=session_actions,
            pinned=pinned,
            manual_agents=manual_agents,
            warnings=warnings,
        )

        provider_allocation_apply_response.additional_properties = d
        return provider_allocation_apply_response

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
