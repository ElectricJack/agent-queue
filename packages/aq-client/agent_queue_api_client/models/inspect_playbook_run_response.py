from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.inspect_playbook_run_response_conversation_history_item import (
        InspectPlaybookRunResponseConversationHistoryItem,
    )
    from ..models.inspect_playbook_run_response_graph_type_0 import InspectPlaybookRunResponseGraphType0
    from ..models.inspect_playbook_run_response_node_trace_item import InspectPlaybookRunResponseNodeTraceItem
    from ..models.inspect_playbook_run_response_trigger_event import InspectPlaybookRunResponseTriggerEvent


T = TypeVar("T", bound="InspectPlaybookRunResponse")


@_attrs_define
class InspectPlaybookRunResponse:
    """
    Attributes:
        run_id (str):
        playbook_id (str):
        playbook_version (int):
        status (str):
        current_node (None | str | Unset):
        started_at (float | None | Unset):
        completed_at (float | None | Unset):
        tokens_used (int | Unset):  Default: 0.
        node_trace (list[InspectPlaybookRunResponseNodeTraceItem] | Unset):
        node_count (int | Unset):  Default: 0.
        conversation_history (list[InspectPlaybookRunResponseConversationHistoryItem] | Unset):
        message_count (int | Unset):  Default: 0.
        trigger_event (InspectPlaybookRunResponseTriggerEvent | Unset):
        error (None | str | Unset):
        paused_at (float | None | Unset):
        waiting_for_event (None | str | Unset):
        total_duration_seconds (float | None | Unset):
        graph (InspectPlaybookRunResponseGraphType0 | None | Unset):
    """

    run_id: str
    playbook_id: str
    playbook_version: int
    status: str
    current_node: None | str | Unset = UNSET
    started_at: float | None | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    tokens_used: int | Unset = 0
    node_trace: list[InspectPlaybookRunResponseNodeTraceItem] | Unset = UNSET
    node_count: int | Unset = 0
    conversation_history: list[InspectPlaybookRunResponseConversationHistoryItem] | Unset = UNSET
    message_count: int | Unset = 0
    trigger_event: InspectPlaybookRunResponseTriggerEvent | Unset = UNSET
    error: None | str | Unset = UNSET
    paused_at: float | None | Unset = UNSET
    waiting_for_event: None | str | Unset = UNSET
    total_duration_seconds: float | None | Unset = UNSET
    graph: InspectPlaybookRunResponseGraphType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.inspect_playbook_run_response_graph_type_0 import InspectPlaybookRunResponseGraphType0

        run_id = self.run_id

        playbook_id = self.playbook_id

        playbook_version = self.playbook_version

        status = self.status

        current_node: None | str | Unset
        if isinstance(self.current_node, Unset):
            current_node = UNSET
        else:
            current_node = self.current_node

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        tokens_used = self.tokens_used

        node_trace: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.node_trace, Unset):
            node_trace = []
            for node_trace_item_data in self.node_trace:
                node_trace_item = node_trace_item_data.to_dict()
                node_trace.append(node_trace_item)

        node_count = self.node_count

        conversation_history: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.conversation_history, Unset):
            conversation_history = []
            for conversation_history_item_data in self.conversation_history:
                conversation_history_item = conversation_history_item_data.to_dict()
                conversation_history.append(conversation_history_item)

        message_count = self.message_count

        trigger_event: dict[str, Any] | Unset = UNSET
        if not isinstance(self.trigger_event, Unset):
            trigger_event = self.trigger_event.to_dict()

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        paused_at: float | None | Unset
        if isinstance(self.paused_at, Unset):
            paused_at = UNSET
        else:
            paused_at = self.paused_at

        waiting_for_event: None | str | Unset
        if isinstance(self.waiting_for_event, Unset):
            waiting_for_event = UNSET
        else:
            waiting_for_event = self.waiting_for_event

        total_duration_seconds: float | None | Unset
        if isinstance(self.total_duration_seconds, Unset):
            total_duration_seconds = UNSET
        else:
            total_duration_seconds = self.total_duration_seconds

        graph: dict[str, Any] | None | Unset
        if isinstance(self.graph, Unset):
            graph = UNSET
        elif isinstance(self.graph, InspectPlaybookRunResponseGraphType0):
            graph = self.graph.to_dict()
        else:
            graph = self.graph

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
                "playbook_id": playbook_id,
                "playbook_version": playbook_version,
                "status": status,
            }
        )
        if current_node is not UNSET:
            field_dict["current_node"] = current_node
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if tokens_used is not UNSET:
            field_dict["tokens_used"] = tokens_used
        if node_trace is not UNSET:
            field_dict["node_trace"] = node_trace
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if conversation_history is not UNSET:
            field_dict["conversation_history"] = conversation_history
        if message_count is not UNSET:
            field_dict["message_count"] = message_count
        if trigger_event is not UNSET:
            field_dict["trigger_event"] = trigger_event
        if error is not UNSET:
            field_dict["error"] = error
        if paused_at is not UNSET:
            field_dict["paused_at"] = paused_at
        if waiting_for_event is not UNSET:
            field_dict["waiting_for_event"] = waiting_for_event
        if total_duration_seconds is not UNSET:
            field_dict["total_duration_seconds"] = total_duration_seconds
        if graph is not UNSET:
            field_dict["graph"] = graph

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.inspect_playbook_run_response_conversation_history_item import (
            InspectPlaybookRunResponseConversationHistoryItem,
        )
        from ..models.inspect_playbook_run_response_graph_type_0 import InspectPlaybookRunResponseGraphType0
        from ..models.inspect_playbook_run_response_node_trace_item import InspectPlaybookRunResponseNodeTraceItem
        from ..models.inspect_playbook_run_response_trigger_event import InspectPlaybookRunResponseTriggerEvent

        d = dict(src_dict)
        run_id = d.pop("run_id")

        playbook_id = d.pop("playbook_id")

        playbook_version = d.pop("playbook_version")

        status = d.pop("status")

        def _parse_current_node(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_node = _parse_current_node(d.pop("current_node", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        tokens_used = d.pop("tokens_used", UNSET)

        _node_trace = d.pop("node_trace", UNSET)
        node_trace: list[InspectPlaybookRunResponseNodeTraceItem] | Unset = UNSET
        if _node_trace is not UNSET:
            node_trace = []
            for node_trace_item_data in _node_trace:
                node_trace_item = InspectPlaybookRunResponseNodeTraceItem.from_dict(node_trace_item_data)

                node_trace.append(node_trace_item)

        node_count = d.pop("node_count", UNSET)

        _conversation_history = d.pop("conversation_history", UNSET)
        conversation_history: list[InspectPlaybookRunResponseConversationHistoryItem] | Unset = UNSET
        if _conversation_history is not UNSET:
            conversation_history = []
            for conversation_history_item_data in _conversation_history:
                conversation_history_item = InspectPlaybookRunResponseConversationHistoryItem.from_dict(
                    conversation_history_item_data
                )

                conversation_history.append(conversation_history_item)

        message_count = d.pop("message_count", UNSET)

        _trigger_event = d.pop("trigger_event", UNSET)
        trigger_event: InspectPlaybookRunResponseTriggerEvent | Unset
        if isinstance(_trigger_event, Unset):
            trigger_event = UNSET
        else:
            trigger_event = InspectPlaybookRunResponseTriggerEvent.from_dict(_trigger_event)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_paused_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        paused_at = _parse_paused_at(d.pop("paused_at", UNSET))

        def _parse_waiting_for_event(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        waiting_for_event = _parse_waiting_for_event(d.pop("waiting_for_event", UNSET))

        def _parse_total_duration_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        total_duration_seconds = _parse_total_duration_seconds(d.pop("total_duration_seconds", UNSET))

        def _parse_graph(data: object) -> InspectPlaybookRunResponseGraphType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                graph_type_0 = InspectPlaybookRunResponseGraphType0.from_dict(data)

                return graph_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(InspectPlaybookRunResponseGraphType0 | None | Unset, data)

        graph = _parse_graph(d.pop("graph", UNSET))

        inspect_playbook_run_response = cls(
            run_id=run_id,
            playbook_id=playbook_id,
            playbook_version=playbook_version,
            status=status,
            current_node=current_node,
            started_at=started_at,
            completed_at=completed_at,
            tokens_used=tokens_used,
            node_trace=node_trace,
            node_count=node_count,
            conversation_history=conversation_history,
            message_count=message_count,
            trigger_event=trigger_event,
            error=error,
            paused_at=paused_at,
            waiting_for_event=waiting_for_event,
            total_duration_seconds=total_duration_seconds,
            graph=graph,
        )

        inspect_playbook_run_response.additional_properties = d
        return inspect_playbook_run_response

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
