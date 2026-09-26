from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.supervisor_inbox_history_request_states_type_0_item import SupervisorInboxHistoryRequestStatesType0Item
from ..types import UNSET, Unset

T = TypeVar("T", bound="SupervisorInboxHistoryRequest")


@_attrs_define
class SupervisorInboxHistoryRequest:
    """
    Attributes:
        conversation_id (None | str | Unset):
        states (list[SupervisorInboxHistoryRequestStatesType0Item] | None | Unset):
        limit (int | Unset):  Default: 50.
        before (float | None | Unset):
    """

    conversation_id: None | str | Unset = UNSET
    states: list[SupervisorInboxHistoryRequestStatesType0Item] | None | Unset = UNSET
    limit: int | Unset = 50
    before: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        conversation_id: None | str | Unset
        if isinstance(self.conversation_id, Unset):
            conversation_id = UNSET
        else:
            conversation_id = self.conversation_id

        states: list[str] | None | Unset
        if isinstance(self.states, Unset):
            states = UNSET
        elif isinstance(self.states, list):
            states = []
            for states_type_0_item_data in self.states:
                states_type_0_item = states_type_0_item_data.value
                states.append(states_type_0_item)

        else:
            states = self.states

        limit = self.limit

        before: float | None | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        else:
            before = self.before

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if conversation_id is not UNSET:
            field_dict["conversation_id"] = conversation_id
        if states is not UNSET:
            field_dict["states"] = states
        if limit is not UNSET:
            field_dict["limit"] = limit
        if before is not UNSET:
            field_dict["before"] = before

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_conversation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        conversation_id = _parse_conversation_id(d.pop("conversation_id", UNSET))

        def _parse_states(data: object) -> list[SupervisorInboxHistoryRequestStatesType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                states_type_0 = []
                _states_type_0 = data
                for states_type_0_item_data in _states_type_0:
                    states_type_0_item = SupervisorInboxHistoryRequestStatesType0Item(states_type_0_item_data)

                    states_type_0.append(states_type_0_item)

                return states_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[SupervisorInboxHistoryRequestStatesType0Item] | None | Unset, data)

        states = _parse_states(d.pop("states", UNSET))

        limit = d.pop("limit", UNSET)

        def _parse_before(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        supervisor_inbox_history_request = cls(
            conversation_id=conversation_id,
            states=states,
            limit=limit,
            before=before,
        )

        return supervisor_inbox_history_request
