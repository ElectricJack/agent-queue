from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.operational_retention_response_counts_type_0 import OperationalRetentionResponseCountsType0


T = TypeVar("T", bound="OperationalRetentionResponse")


@_attrs_define
class OperationalRetentionResponse:
    """A completed bounded retention pass, overlap acknowledgement, or refusal.

    Attributes:
        success (bool):
        counts (None | OperationalRetentionResponseCountsType0 | Unset):
        dry_run (bool | None | Unset):
        outcome (Literal['already_running'] | None | Unset):
        error (None | str | Unset):
    """

    success: bool
    counts: None | OperationalRetentionResponseCountsType0 | Unset = UNSET
    dry_run: bool | None | Unset = UNSET
    outcome: Literal["already_running"] | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.operational_retention_response_counts_type_0 import OperationalRetentionResponseCountsType0

        success = self.success

        counts: dict[str, Any] | None | Unset
        if isinstance(self.counts, Unset):
            counts = UNSET
        elif isinstance(self.counts, OperationalRetentionResponseCountsType0):
            counts = self.counts.to_dict()
        else:
            counts = self.counts

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        outcome: Literal["already_running"] | None | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if counts is not UNSET:
            field_dict["counts"] = counts
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operational_retention_response_counts_type_0 import OperationalRetentionResponseCountsType0

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_counts(data: object) -> None | OperationalRetentionResponseCountsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                counts_type_0 = OperationalRetentionResponseCountsType0.from_dict(data)

                return counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationalRetentionResponseCountsType0 | Unset, data)

        counts = _parse_counts(d.pop("counts", UNSET))

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        def _parse_outcome(data: object) -> Literal["already_running"] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            outcome_type_0 = cast(Literal["already_running"], data)
            if outcome_type_0 != "already_running":
                raise ValueError(f"outcome_type_0 must match const 'already_running', got '{outcome_type_0}'")
            return outcome_type_0
            return cast(Literal["already_running"] | None | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        operational_retention_response = cls(
            success=success,
            counts=counts,
            dry_run=dry_run,
            outcome=outcome,
            error=error,
        )

        operational_retention_response.additional_properties = d
        return operational_retention_response

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
