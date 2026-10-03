from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_generation_status_response_budgets_item import KnowledgeGenerationStatusResponseBudgetsItem
    from ..models.knowledge_generation_status_response_jobs_item import KnowledgeGenerationStatusResponseJobsItem


T = TypeVar("T", bound="KnowledgeGenerationStatusResponse")


@_attrs_define
class KnowledgeGenerationStatusResponse:
    """Content-free cost and ambiguity diagnostics; never inspects a provider.

    ``jobs`` groups by scope / state / error code, ``budgets`` is the daily
    per-feature allowance projection, and ``unknown_calls`` counts reservations
    whose paid provider operation has no known outcome.

        Attributes:
            success (bool | Unset):  Default: True.
            jobs (list[KnowledgeGenerationStatusResponseJobsItem] | Unset):
            budgets (list[KnowledgeGenerationStatusResponseBudgetsItem] | Unset):
            unknown_calls (int | None | Unset):
            page_limit (int | None | Unset):
    """

    success: bool | Unset = True
    jobs: list[KnowledgeGenerationStatusResponseJobsItem] | Unset = UNSET
    budgets: list[KnowledgeGenerationStatusResponseBudgetsItem] | Unset = UNSET
    unknown_calls: int | None | Unset = UNSET
    page_limit: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        jobs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.jobs, Unset):
            jobs = []
            for jobs_item_data in self.jobs:
                jobs_item = jobs_item_data.to_dict()
                jobs.append(jobs_item)

        budgets: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.budgets, Unset):
            budgets = []
            for budgets_item_data in self.budgets:
                budgets_item = budgets_item_data.to_dict()
                budgets.append(budgets_item)

        unknown_calls: int | None | Unset
        if isinstance(self.unknown_calls, Unset):
            unknown_calls = UNSET
        else:
            unknown_calls = self.unknown_calls

        page_limit: int | None | Unset
        if isinstance(self.page_limit, Unset):
            page_limit = UNSET
        else:
            page_limit = self.page_limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if jobs is not UNSET:
            field_dict["jobs"] = jobs
        if budgets is not UNSET:
            field_dict["budgets"] = budgets
        if unknown_calls is not UNSET:
            field_dict["unknown_calls"] = unknown_calls
        if page_limit is not UNSET:
            field_dict["page_limit"] = page_limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_generation_status_response_budgets_item import (
            KnowledgeGenerationStatusResponseBudgetsItem,
        )
        from ..models.knowledge_generation_status_response_jobs_item import KnowledgeGenerationStatusResponseJobsItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _jobs = d.pop("jobs", UNSET)
        jobs: list[KnowledgeGenerationStatusResponseJobsItem] | Unset = UNSET
        if _jobs is not UNSET:
            jobs = []
            for jobs_item_data in _jobs:
                jobs_item = KnowledgeGenerationStatusResponseJobsItem.from_dict(jobs_item_data)

                jobs.append(jobs_item)

        _budgets = d.pop("budgets", UNSET)
        budgets: list[KnowledgeGenerationStatusResponseBudgetsItem] | Unset = UNSET
        if _budgets is not UNSET:
            budgets = []
            for budgets_item_data in _budgets:
                budgets_item = KnowledgeGenerationStatusResponseBudgetsItem.from_dict(budgets_item_data)

                budgets.append(budgets_item)

        def _parse_unknown_calls(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        unknown_calls = _parse_unknown_calls(d.pop("unknown_calls", UNSET))

        def _parse_page_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        page_limit = _parse_page_limit(d.pop("page_limit", UNSET))

        knowledge_generation_status_response = cls(
            success=success,
            jobs=jobs,
            budgets=budgets,
            unknown_calls=unknown_calls,
            page_limit=page_limit,
        )

        knowledge_generation_status_response.additional_properties = d
        return knowledge_generation_status_response

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
