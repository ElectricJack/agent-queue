from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.held_batch_summary_state import HeldBatchSummaryState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.held_batch_summary_lineage import HeldBatchSummaryLineage


T = TypeVar("T", bound="HeldBatchSummary")


@_attrs_define
class HeldBatchSummary:
    """Ingestion membership survives release; ``state`` says whether it still holds work.

    Attributes:
        batch_id (str):
        state (HeldBatchSummaryState):
        structure_revision (int):
        source (str):
        lineage (HeldBatchSummaryLineage):
        reviewer_task_id (None | str | Unset):
    """

    batch_id: str
    state: HeldBatchSummaryState
    structure_revision: int
    source: str
    lineage: HeldBatchSummaryLineage
    reviewer_task_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        batch_id = self.batch_id

        state = self.state.value

        structure_revision = self.structure_revision

        source = self.source

        lineage = self.lineage.to_dict()

        reviewer_task_id: None | str | Unset
        if isinstance(self.reviewer_task_id, Unset):
            reviewer_task_id = UNSET
        else:
            reviewer_task_id = self.reviewer_task_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "batch_id": batch_id,
                "state": state,
                "structure_revision": structure_revision,
                "source": source,
                "lineage": lineage,
            }
        )
        if reviewer_task_id is not UNSET:
            field_dict["reviewer_task_id"] = reviewer_task_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.held_batch_summary_lineage import HeldBatchSummaryLineage

        d = dict(src_dict)
        batch_id = d.pop("batch_id")

        state = HeldBatchSummaryState(d.pop("state"))

        structure_revision = d.pop("structure_revision")

        source = d.pop("source")

        lineage = HeldBatchSummaryLineage.from_dict(d.pop("lineage"))

        def _parse_reviewer_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reviewer_task_id = _parse_reviewer_task_id(d.pop("reviewer_task_id", UNSET))

        held_batch_summary = cls(
            batch_id=batch_id,
            state=state,
            structure_revision=structure_revision,
            source=source,
            lineage=lineage,
            reviewer_task_id=reviewer_task_id,
        )

        held_batch_summary.additional_properties = d
        return held_batch_summary

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
