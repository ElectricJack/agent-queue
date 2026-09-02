from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.loop_node_detail_dto_failure_policy import LoopNodeDetailDTOFailurePolicy
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_value_dto import ExplanationValueDTO


T = TypeVar("T", bound="LoopNodeDetailDTO")


@_attrs_define
class LoopNodeDetailDTO:
    """ForEachStep only.

    Attributes:
        collection (ExplanationValueDTO): One typed value, in both its human and canonical forms.

            ``display`` is always present and always safe to render.  ``canonical`` is
            the Advanced-view payload and is ``None`` whenever ``redacted`` is true.
        item_binding (str):
        failure_policy (LoopNodeDetailDTOFailurePolicy):
        body_entry_step_id (str):
        continuation_step_id (None | str | Unset):
    """

    collection: ExplanationValueDTO
    item_binding: str
    failure_policy: LoopNodeDetailDTOFailurePolicy
    body_entry_step_id: str
    continuation_step_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        collection = self.collection.to_dict()

        item_binding = self.item_binding

        failure_policy = self.failure_policy.value

        body_entry_step_id = self.body_entry_step_id

        continuation_step_id: None | str | Unset
        if isinstance(self.continuation_step_id, Unset):
            continuation_step_id = UNSET
        else:
            continuation_step_id = self.continuation_step_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "collection": collection,
                "item_binding": item_binding,
                "failure_policy": failure_policy,
                "body_entry_step_id": body_entry_step_id,
            }
        )
        if continuation_step_id is not UNSET:
            field_dict["continuation_step_id"] = continuation_step_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_value_dto import ExplanationValueDTO

        d = dict(src_dict)
        collection = ExplanationValueDTO.from_dict(d.pop("collection"))

        item_binding = d.pop("item_binding")

        failure_policy = LoopNodeDetailDTOFailurePolicy(d.pop("failure_policy"))

        body_entry_step_id = d.pop("body_entry_step_id")

        def _parse_continuation_step_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        continuation_step_id = _parse_continuation_step_id(d.pop("continuation_step_id", UNSET))

        loop_node_detail_dto = cls(
            collection=collection,
            item_binding=item_binding,
            failure_policy=failure_policy,
            body_entry_step_id=body_entry_step_id,
            continuation_step_id=continuation_step_id,
        )

        return loop_node_detail_dto
