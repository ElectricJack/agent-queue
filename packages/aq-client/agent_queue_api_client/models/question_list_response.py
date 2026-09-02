from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_question_detail import AgentQuestionDetail


T = TypeVar("T", bound="QuestionListResponse")


@_attrs_define
class QuestionListResponse:
    """
    Attributes:
        questions (list[AgentQuestionDetail] | Unset):
        count (int | Unset):  Default: 0.
    """

    questions: list[AgentQuestionDetail] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        questions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.questions, Unset):
            questions = []
            for questions_item_data in self.questions:
                questions_item = questions_item_data.to_dict()
                questions.append(questions_item)

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if questions is not UNSET:
            field_dict["questions"] = questions
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_question_detail import AgentQuestionDetail

        d = dict(src_dict)
        _questions = d.pop("questions", UNSET)
        questions: list[AgentQuestionDetail] | Unset = UNSET
        if _questions is not UNSET:
            questions = []
            for questions_item_data in _questions:
                questions_item = AgentQuestionDetail.from_dict(questions_item_data)

                questions.append(questions_item)

        count = d.pop("count", UNSET)

        question_list_response = cls(
            questions=questions,
            count=count,
        )

        question_list_response.additional_properties = d
        return question_list_response

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
