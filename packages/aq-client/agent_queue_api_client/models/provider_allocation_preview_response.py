from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_busy_set import ProviderAllocationBusySet
    from ..models.provider_allocation_ceiling_change import ProviderAllocationCeilingChange
    from ..models.provider_allocation_pinned_task import ProviderAllocationPinnedTask
    from ..models.provider_allocation_preference import ProviderAllocationPreference
    from ..models.provider_allocation_preview_profile import ProviderAllocationPreviewProfile
    from ..models.provider_allocation_preview_session import ProviderAllocationPreviewSession
    from ..models.provider_allocation_project_limit import ProviderAllocationProjectLimit
    from ..models.provider_allocation_push_change import ProviderAllocationPushChange
    from ..models.provider_allocation_request import ProviderAllocationRequest
    from ..models.provider_allocation_warning import ProviderAllocationWarning


T = TypeVar("T", bound="ProviderAllocationPreviewResponse")


@_attrs_define
class ProviderAllocationPreviewResponse:
    """``provider_allocation_preview`` and ``POST /api/providers/allocation/preview``.

    Attributes:
        provider (str):
        request (ProviderAllocationRequest): The canonical request a preview token was issued for.

            ``bounds`` keeps only the keys the caller gave, so ``{"max": null}``
            (unbounded) stays distinct from an omitted ``max``.
        required_scope (str):
        ceiling (ProviderAllocationCeilingChange): The provider-wide configured ceiling before and after.
        busy (ProviderAllocationBusySet): The busy sessions (and their tasks) the request stops.
        preview_token (str):
        success (bool | Unset):  Default: True.
        now (float | None | Unset):
        vendor (str | Unset):  Default: ''.
        state (None | str | Unset):
        global_max_active (int | None | Unset):
        selected (list[str] | Unset):
        profiles (list[ProviderAllocationPreviewProfile] | Unset):
        project_limits (list[ProviderAllocationProjectLimit] | Unset):
        sessions (list[ProviderAllocationPreviewSession] | Unset):
        pinned (list[ProviderAllocationPinnedTask] | Unset):
        manual_agents (list[ProviderAllocationPushChange] | Unset):
        preference (None | ProviderAllocationPreference | Unset):
        warnings (list[ProviderAllocationWarning] | Unset):
        blocked (bool | Unset):  Default: False.
    """

    provider: str
    request: ProviderAllocationRequest
    required_scope: str
    ceiling: ProviderAllocationCeilingChange
    busy: ProviderAllocationBusySet
    preview_token: str
    success: bool | Unset = True
    now: float | None | Unset = UNSET
    vendor: str | Unset = ""
    state: None | str | Unset = UNSET
    global_max_active: int | None | Unset = UNSET
    selected: list[str] | Unset = UNSET
    profiles: list[ProviderAllocationPreviewProfile] | Unset = UNSET
    project_limits: list[ProviderAllocationProjectLimit] | Unset = UNSET
    sessions: list[ProviderAllocationPreviewSession] | Unset = UNSET
    pinned: list[ProviderAllocationPinnedTask] | Unset = UNSET
    manual_agents: list[ProviderAllocationPushChange] | Unset = UNSET
    preference: None | ProviderAllocationPreference | Unset = UNSET
    warnings: list[ProviderAllocationWarning] | Unset = UNSET
    blocked: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_allocation_preference import ProviderAllocationPreference

        provider = self.provider

        request = self.request.to_dict()

        required_scope = self.required_scope

        ceiling = self.ceiling.to_dict()

        busy = self.busy.to_dict()

        preview_token = self.preview_token

        success = self.success

        now: float | None | Unset
        if isinstance(self.now, Unset):
            now = UNSET
        else:
            now = self.now

        vendor = self.vendor

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        global_max_active: int | None | Unset
        if isinstance(self.global_max_active, Unset):
            global_max_active = UNSET
        else:
            global_max_active = self.global_max_active

        selected: list[str] | Unset = UNSET
        if not isinstance(self.selected, Unset):
            selected = self.selected

        profiles: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.profiles, Unset):
            profiles = []
            for profiles_item_data in self.profiles:
                profiles_item = profiles_item_data.to_dict()
                profiles.append(profiles_item)

        project_limits: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.project_limits, Unset):
            project_limits = []
            for project_limits_item_data in self.project_limits:
                project_limits_item = project_limits_item_data.to_dict()
                project_limits.append(project_limits_item)

        sessions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.sessions, Unset):
            sessions = []
            for sessions_item_data in self.sessions:
                sessions_item = sessions_item_data.to_dict()
                sessions.append(sessions_item)

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

        preference: dict[str, Any] | None | Unset
        if isinstance(self.preference, Unset):
            preference = UNSET
        elif isinstance(self.preference, ProviderAllocationPreference):
            preference = self.preference.to_dict()
        else:
            preference = self.preference

        warnings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)

        blocked = self.blocked

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
                "request": request,
                "required_scope": required_scope,
                "ceiling": ceiling,
                "busy": busy,
                "preview_token": preview_token,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if now is not UNSET:
            field_dict["now"] = now
        if vendor is not UNSET:
            field_dict["vendor"] = vendor
        if state is not UNSET:
            field_dict["state"] = state
        if global_max_active is not UNSET:
            field_dict["global_max_active"] = global_max_active
        if selected is not UNSET:
            field_dict["selected"] = selected
        if profiles is not UNSET:
            field_dict["profiles"] = profiles
        if project_limits is not UNSET:
            field_dict["project_limits"] = project_limits
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if pinned is not UNSET:
            field_dict["pinned"] = pinned
        if manual_agents is not UNSET:
            field_dict["manual_agents"] = manual_agents
        if preference is not UNSET:
            field_dict["preference"] = preference
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if blocked is not UNSET:
            field_dict["blocked"] = blocked

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_busy_set import ProviderAllocationBusySet
        from ..models.provider_allocation_ceiling_change import ProviderAllocationCeilingChange
        from ..models.provider_allocation_pinned_task import ProviderAllocationPinnedTask
        from ..models.provider_allocation_preference import ProviderAllocationPreference
        from ..models.provider_allocation_preview_profile import ProviderAllocationPreviewProfile
        from ..models.provider_allocation_preview_session import ProviderAllocationPreviewSession
        from ..models.provider_allocation_project_limit import ProviderAllocationProjectLimit
        from ..models.provider_allocation_push_change import ProviderAllocationPushChange
        from ..models.provider_allocation_request import ProviderAllocationRequest
        from ..models.provider_allocation_warning import ProviderAllocationWarning

        d = dict(src_dict)
        provider = d.pop("provider")

        request = ProviderAllocationRequest.from_dict(d.pop("request"))

        required_scope = d.pop("required_scope")

        ceiling = ProviderAllocationCeilingChange.from_dict(d.pop("ceiling"))

        busy = ProviderAllocationBusySet.from_dict(d.pop("busy"))

        preview_token = d.pop("preview_token")

        success = d.pop("success", UNSET)

        def _parse_now(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        now = _parse_now(d.pop("now", UNSET))

        vendor = d.pop("vendor", UNSET)

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_global_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        global_max_active = _parse_global_max_active(d.pop("global_max_active", UNSET))

        selected = cast(list[str], d.pop("selected", UNSET))

        _profiles = d.pop("profiles", UNSET)
        profiles: list[ProviderAllocationPreviewProfile] | Unset = UNSET
        if _profiles is not UNSET:
            profiles = []
            for profiles_item_data in _profiles:
                profiles_item = ProviderAllocationPreviewProfile.from_dict(profiles_item_data)

                profiles.append(profiles_item)

        _project_limits = d.pop("project_limits", UNSET)
        project_limits: list[ProviderAllocationProjectLimit] | Unset = UNSET
        if _project_limits is not UNSET:
            project_limits = []
            for project_limits_item_data in _project_limits:
                project_limits_item = ProviderAllocationProjectLimit.from_dict(project_limits_item_data)

                project_limits.append(project_limits_item)

        _sessions = d.pop("sessions", UNSET)
        sessions: list[ProviderAllocationPreviewSession] | Unset = UNSET
        if _sessions is not UNSET:
            sessions = []
            for sessions_item_data in _sessions:
                sessions_item = ProviderAllocationPreviewSession.from_dict(sessions_item_data)

                sessions.append(sessions_item)

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

        def _parse_preference(data: object) -> None | ProviderAllocationPreference | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                preference_type_0 = ProviderAllocationPreference.from_dict(data)

                return preference_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationPreference | Unset, data)

        preference = _parse_preference(d.pop("preference", UNSET))

        _warnings = d.pop("warnings", UNSET)
        warnings: list[ProviderAllocationWarning] | Unset = UNSET
        if _warnings is not UNSET:
            warnings = []
            for warnings_item_data in _warnings:
                warnings_item = ProviderAllocationWarning.from_dict(warnings_item_data)

                warnings.append(warnings_item)

        blocked = d.pop("blocked", UNSET)

        provider_allocation_preview_response = cls(
            provider=provider,
            request=request,
            required_scope=required_scope,
            ceiling=ceiling,
            busy=busy,
            preview_token=preview_token,
            success=success,
            now=now,
            vendor=vendor,
            state=state,
            global_max_active=global_max_active,
            selected=selected,
            profiles=profiles,
            project_limits=project_limits,
            sessions=sessions,
            pinned=pinned,
            manual_agents=manual_agents,
            preference=preference,
            warnings=warnings,
            blocked=blocked,
        )

        provider_allocation_preview_response.additional_properties = d
        return provider_allocation_preview_response

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
