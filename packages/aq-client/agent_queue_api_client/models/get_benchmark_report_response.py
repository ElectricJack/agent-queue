from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.get_benchmark_report_response_pairs_item import GetBenchmarkReportResponsePairsItem


T = TypeVar("T", bound="GetBenchmarkReportResponse")


@_attrs_define
class GetBenchmarkReportResponse:
    """
    Attributes:
        success (bool):
        project_id (str):
        policy_sha256 (str):
        rate_card_version (str):
        rate_card_sha256 (str):
        pairs (list[GetBenchmarkReportResponsePairsItem]):
        attempts_total (int):
        attempts_failed (int):
        cost_complete (bool):
        estimated_api_cost_usd (float):
        unpriced_tokens (int):
        tokens_total (int):
    """

    success: bool
    project_id: str
    policy_sha256: str
    rate_card_version: str
    rate_card_sha256: str
    pairs: list[GetBenchmarkReportResponsePairsItem]
    attempts_total: int
    attempts_failed: int
    cost_complete: bool
    estimated_api_cost_usd: float
    unpriced_tokens: int
    tokens_total: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        project_id = self.project_id

        policy_sha256 = self.policy_sha256

        rate_card_version = self.rate_card_version

        rate_card_sha256 = self.rate_card_sha256

        pairs = []
        for pairs_item_data in self.pairs:
            pairs_item = pairs_item_data.to_dict()
            pairs.append(pairs_item)

        attempts_total = self.attempts_total

        attempts_failed = self.attempts_failed

        cost_complete = self.cost_complete

        estimated_api_cost_usd = self.estimated_api_cost_usd

        unpriced_tokens = self.unpriced_tokens

        tokens_total = self.tokens_total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "project_id": project_id,
                "policy_sha256": policy_sha256,
                "rate_card_version": rate_card_version,
                "rate_card_sha256": rate_card_sha256,
                "pairs": pairs,
                "attempts_total": attempts_total,
                "attempts_failed": attempts_failed,
                "cost_complete": cost_complete,
                "estimated_api_cost_usd": estimated_api_cost_usd,
                "unpriced_tokens": unpriced_tokens,
                "tokens_total": tokens_total,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_benchmark_report_response_pairs_item import GetBenchmarkReportResponsePairsItem

        d = dict(src_dict)
        success = d.pop("success")

        project_id = d.pop("project_id")

        policy_sha256 = d.pop("policy_sha256")

        rate_card_version = d.pop("rate_card_version")

        rate_card_sha256 = d.pop("rate_card_sha256")

        pairs = []
        _pairs = d.pop("pairs")
        for pairs_item_data in _pairs:
            pairs_item = GetBenchmarkReportResponsePairsItem.from_dict(pairs_item_data)

            pairs.append(pairs_item)

        attempts_total = d.pop("attempts_total")

        attempts_failed = d.pop("attempts_failed")

        cost_complete = d.pop("cost_complete")

        estimated_api_cost_usd = d.pop("estimated_api_cost_usd")

        unpriced_tokens = d.pop("unpriced_tokens")

        tokens_total = d.pop("tokens_total")

        get_benchmark_report_response = cls(
            success=success,
            project_id=project_id,
            policy_sha256=policy_sha256,
            rate_card_version=rate_card_version,
            rate_card_sha256=rate_card_sha256,
            pairs=pairs,
            attempts_total=attempts_total,
            attempts_failed=attempts_failed,
            cost_complete=cost_complete,
            estimated_api_cost_usd=estimated_api_cost_usd,
            unpriced_tokens=unpriced_tokens,
            tokens_total=tokens_total,
        )

        get_benchmark_report_response.additional_properties = d
        return get_benchmark_report_response

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
