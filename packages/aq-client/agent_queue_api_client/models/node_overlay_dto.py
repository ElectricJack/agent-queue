from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.node_overlay_dto_state import NodeOverlayDTOState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.loop_iteration_overlay_dto import LoopIterationOverlayDTO


T = TypeVar("T", bound="NodeOverlayDTO")


@_attrs_define
class NodeOverlayDTO:
    """
    Attributes:
        step_id (str):
        state (NodeOverlayDTOState | Unset):  Default: NodeOverlayDTOState.NOT_VISITED.
        visit_count (int | Unset):  Default: 0.
        last_outcome (None | str | Unset):
        receipt_ids (list[str] | Unset):
        iterations (list[LoopIterationOverlayDTO] | Unset):
    """

    step_id: str
    state: NodeOverlayDTOState | Unset = NodeOverlayDTOState.NOT_VISITED
    visit_count: int | Unset = 0
    last_outcome: None | str | Unset = UNSET
    receipt_ids: list[str] | Unset = UNSET
    iterations: list[LoopIterationOverlayDTO] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        step_id = self.step_id

        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.value

        visit_count = self.visit_count

        last_outcome: None | str | Unset
        if isinstance(self.last_outcome, Unset):
            last_outcome = UNSET
        else:
            last_outcome = self.last_outcome

        receipt_ids: list[str] | Unset = UNSET
        if not isinstance(self.receipt_ids, Unset):
            receipt_ids = self.receipt_ids

        iterations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.iterations, Unset):
            iterations = []
            for iterations_item_data in self.iterations:
                iterations_item = iterations_item_data.to_dict()
                iterations.append(iterations_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "step_id": step_id,
            }
        )
        if state is not UNSET:
            field_dict["state"] = state
        if visit_count is not UNSET:
            field_dict["visit_count"] = visit_count
        if last_outcome is not UNSET:
            field_dict["last_outcome"] = last_outcome
        if receipt_ids is not UNSET:
            field_dict["receipt_ids"] = receipt_ids
        if iterations is not UNSET:
            field_dict["iterations"] = iterations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.loop_iteration_overlay_dto import LoopIterationOverlayDTO

        d = dict(src_dict)
        step_id = d.pop("step_id")

        _state = d.pop("state", UNSET)
        state: NodeOverlayDTOState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = NodeOverlayDTOState(_state)

        visit_count = d.pop("visit_count", UNSET)

        def _parse_last_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        last_outcome = _parse_last_outcome(d.pop("last_outcome", UNSET))

        receipt_ids = cast(list[str], d.pop("receipt_ids", UNSET))

        _iterations = d.pop("iterations", UNSET)
        iterations: list[LoopIterationOverlayDTO] | Unset = UNSET
        if _iterations is not UNSET:
            iterations = []
            for iterations_item_data in _iterations:
                iterations_item = LoopIterationOverlayDTO.from_dict(iterations_item_data)

                iterations.append(iterations_item)

        node_overlay_dto = cls(
            step_id=step_id,
            state=state,
            visit_count=visit_count,
            last_outcome=last_outcome,
            receipt_ids=receipt_ids,
            iterations=iterations,
        )

        return node_overlay_dto
