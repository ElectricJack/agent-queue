from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_settings import AgentSettings
    from ..models.agent_waiting_question import AgentWaitingQuestion


T = TypeVar("T", bound="AgentSummary")


@_attrs_define
class AgentSummary:
    """
    Attributes:
        id (str):
        name (str):
        profile_id (str):
        settings (AgentSettings):
        role (str | Unset):  Default: 'worker'.
        enabled (bool | Unset):  Default: True.
        state (str | Unset):  Default: 'idle'.
        provider (None | str | Unset):
        harness (None | str | Unset):
        model (None | str | Unset):
        intelligence_class (None | str | Unset):
        current_task_id (None | str | Unset):
        current_task_title (None | str | Unset):
        current_project_id (None | str | Unset):
        project_id (None | str | Unset):
        workspace_id (None | str | Unset):
        session_id (None | str | Unset):
        session_state (None | str | Unset):
        session_provider (None | str | Unset):
        origin (str | Unset):  Default: 'manual'.
        session_lifecycle (None | str | Unset):
        waiting_question (AgentWaitingQuestion | None | Unset):
        active_subagent_count (int | None | Unset):
        subagent_count_complete (bool | Unset):  Default: False.
        aq_subagent_count (int | Unset):  Default: 0.
        native_subagent_count (int | None | Unset):
        subagents_spawned_total (int | Unset):  Default: 0.
        last_heartbeat (float | None | Unset):
        session_tokens_used (int | Unset):  Default: 0.
    """

    id: str
    name: str
    profile_id: str
    settings: AgentSettings
    role: str | Unset = "worker"
    enabled: bool | Unset = True
    state: str | Unset = "idle"
    provider: None | str | Unset = UNSET
    harness: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    current_task_id: None | str | Unset = UNSET
    current_task_title: None | str | Unset = UNSET
    current_project_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    workspace_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    session_state: None | str | Unset = UNSET
    session_provider: None | str | Unset = UNSET
    origin: str | Unset = "manual"
    session_lifecycle: None | str | Unset = UNSET
    waiting_question: AgentWaitingQuestion | None | Unset = UNSET
    active_subagent_count: int | None | Unset = UNSET
    subagent_count_complete: bool | Unset = False
    aq_subagent_count: int | Unset = 0
    native_subagent_count: int | None | Unset = UNSET
    subagents_spawned_total: int | Unset = 0
    last_heartbeat: float | None | Unset = UNSET
    session_tokens_used: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_waiting_question import AgentWaitingQuestion

        id = self.id

        name = self.name

        profile_id = self.profile_id

        settings = self.settings.to_dict()

        role = self.role

        enabled = self.enabled

        state = self.state

        provider: None | str | Unset
        if isinstance(self.provider, Unset):
            provider = UNSET
        else:
            provider = self.provider

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        current_task_id: None | str | Unset
        if isinstance(self.current_task_id, Unset):
            current_task_id = UNSET
        else:
            current_task_id = self.current_task_id

        current_task_title: None | str | Unset
        if isinstance(self.current_task_title, Unset):
            current_task_title = UNSET
        else:
            current_task_title = self.current_task_title

        current_project_id: None | str | Unset
        if isinstance(self.current_project_id, Unset):
            current_project_id = UNSET
        else:
            current_project_id = self.current_project_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        workspace_id: None | str | Unset
        if isinstance(self.workspace_id, Unset):
            workspace_id = UNSET
        else:
            workspace_id = self.workspace_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        session_state: None | str | Unset
        if isinstance(self.session_state, Unset):
            session_state = UNSET
        else:
            session_state = self.session_state

        session_provider: None | str | Unset
        if isinstance(self.session_provider, Unset):
            session_provider = UNSET
        else:
            session_provider = self.session_provider

        origin = self.origin

        session_lifecycle: None | str | Unset
        if isinstance(self.session_lifecycle, Unset):
            session_lifecycle = UNSET
        else:
            session_lifecycle = self.session_lifecycle

        waiting_question: dict[str, Any] | None | Unset
        if isinstance(self.waiting_question, Unset):
            waiting_question = UNSET
        elif isinstance(self.waiting_question, AgentWaitingQuestion):
            waiting_question = self.waiting_question.to_dict()
        else:
            waiting_question = self.waiting_question

        active_subagent_count: int | None | Unset
        if isinstance(self.active_subagent_count, Unset):
            active_subagent_count = UNSET
        else:
            active_subagent_count = self.active_subagent_count

        subagent_count_complete = self.subagent_count_complete

        aq_subagent_count = self.aq_subagent_count

        native_subagent_count: int | None | Unset
        if isinstance(self.native_subagent_count, Unset):
            native_subagent_count = UNSET
        else:
            native_subagent_count = self.native_subagent_count

        subagents_spawned_total = self.subagents_spawned_total

        last_heartbeat: float | None | Unset
        if isinstance(self.last_heartbeat, Unset):
            last_heartbeat = UNSET
        else:
            last_heartbeat = self.last_heartbeat

        session_tokens_used = self.session_tokens_used

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "name": name,
                "profile_id": profile_id,
                "settings": settings,
            }
        )
        if role is not UNSET:
            field_dict["role"] = role
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if state is not UNSET:
            field_dict["state"] = state
        if provider is not UNSET:
            field_dict["provider"] = provider
        if harness is not UNSET:
            field_dict["harness"] = harness
        if model is not UNSET:
            field_dict["model"] = model
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if current_task_id is not UNSET:
            field_dict["current_task_id"] = current_task_id
        if current_task_title is not UNSET:
            field_dict["current_task_title"] = current_task_title
        if current_project_id is not UNSET:
            field_dict["current_project_id"] = current_project_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if workspace_id is not UNSET:
            field_dict["workspace_id"] = workspace_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if session_state is not UNSET:
            field_dict["session_state"] = session_state
        if session_provider is not UNSET:
            field_dict["session_provider"] = session_provider
        if origin is not UNSET:
            field_dict["origin"] = origin
        if session_lifecycle is not UNSET:
            field_dict["session_lifecycle"] = session_lifecycle
        if waiting_question is not UNSET:
            field_dict["waiting_question"] = waiting_question
        if active_subagent_count is not UNSET:
            field_dict["active_subagent_count"] = active_subagent_count
        if subagent_count_complete is not UNSET:
            field_dict["subagent_count_complete"] = subagent_count_complete
        if aq_subagent_count is not UNSET:
            field_dict["aq_subagent_count"] = aq_subagent_count
        if native_subagent_count is not UNSET:
            field_dict["native_subagent_count"] = native_subagent_count
        if subagents_spawned_total is not UNSET:
            field_dict["subagents_spawned_total"] = subagents_spawned_total
        if last_heartbeat is not UNSET:
            field_dict["last_heartbeat"] = last_heartbeat
        if session_tokens_used is not UNSET:
            field_dict["session_tokens_used"] = session_tokens_used

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_settings import AgentSettings
        from ..models.agent_waiting_question import AgentWaitingQuestion

        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        profile_id = d.pop("profile_id")

        settings = AgentSettings.from_dict(d.pop("settings"))

        role = d.pop("role", UNSET)

        enabled = d.pop("enabled", UNSET)

        state = d.pop("state", UNSET)

        def _parse_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        provider = _parse_provider(d.pop("provider", UNSET))

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_current_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_id = _parse_current_task_id(d.pop("current_task_id", UNSET))

        def _parse_current_task_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_task_title = _parse_current_task_title(d.pop("current_task_title", UNSET))

        def _parse_current_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_project_id = _parse_current_project_id(d.pop("current_project_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_workspace_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace_id = _parse_workspace_id(d.pop("workspace_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        def _parse_session_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_state = _parse_session_state(d.pop("session_state", UNSET))

        def _parse_session_provider(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_provider = _parse_session_provider(d.pop("session_provider", UNSET))

        origin = d.pop("origin", UNSET)

        def _parse_session_lifecycle(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_lifecycle = _parse_session_lifecycle(d.pop("session_lifecycle", UNSET))

        def _parse_waiting_question(data: object) -> AgentWaitingQuestion | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                waiting_question_type_0 = AgentWaitingQuestion.from_dict(data)

                return waiting_question_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentWaitingQuestion | None | Unset, data)

        waiting_question = _parse_waiting_question(d.pop("waiting_question", UNSET))

        def _parse_active_subagent_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        active_subagent_count = _parse_active_subagent_count(d.pop("active_subagent_count", UNSET))

        subagent_count_complete = d.pop("subagent_count_complete", UNSET)

        aq_subagent_count = d.pop("aq_subagent_count", UNSET)

        def _parse_native_subagent_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        native_subagent_count = _parse_native_subagent_count(d.pop("native_subagent_count", UNSET))

        subagents_spawned_total = d.pop("subagents_spawned_total", UNSET)

        def _parse_last_heartbeat(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        last_heartbeat = _parse_last_heartbeat(d.pop("last_heartbeat", UNSET))

        session_tokens_used = d.pop("session_tokens_used", UNSET)

        agent_summary = cls(
            id=id,
            name=name,
            profile_id=profile_id,
            settings=settings,
            role=role,
            enabled=enabled,
            state=state,
            provider=provider,
            harness=harness,
            model=model,
            intelligence_class=intelligence_class,
            current_task_id=current_task_id,
            current_task_title=current_task_title,
            current_project_id=current_project_id,
            project_id=project_id,
            workspace_id=workspace_id,
            session_id=session_id,
            session_state=session_state,
            session_provider=session_provider,
            origin=origin,
            session_lifecycle=session_lifecycle,
            waiting_question=waiting_question,
            active_subagent_count=active_subagent_count,
            subagent_count_complete=subagent_count_complete,
            aq_subagent_count=aq_subagent_count,
            native_subagent_count=native_subagent_count,
            subagents_spawned_total=subagents_spawned_total,
            last_heartbeat=last_heartbeat,
            session_tokens_used=session_tokens_used,
        )

        agent_summary.additional_properties = d
        return agent_summary

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
