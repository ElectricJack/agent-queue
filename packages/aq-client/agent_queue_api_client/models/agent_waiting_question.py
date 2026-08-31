from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AgentWaitingQuestion")


@_attrs_define
class AgentWaitingQuestion:
    """
    Attributes:
        id (str):
        question (str):
        state (str):
        requires_human (bool):
        created_at (float):
    """

    id: str
    question: str
    state: str
    requires_human: bool
    created_at: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        question = self.question

        state = self.state

        requires_human = self.requires_human

        created_at = self.created_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "question": question,
                "state": state,
                "requires_human": requires_human,
                "created_at": created_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        question = d.pop("question")

        state = d.pop("state")

        requires_human = d.pop("requires_human")

        created_at = d.pop("created_at")

        agent_waiting_question = cls(
            id=id,
            question=question,
            state=state,
            requires_human=requires_human,
            created_at=created_at,
        )

        agent_waiting_question.additional_properties = d
        return agent_waiting_question

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
