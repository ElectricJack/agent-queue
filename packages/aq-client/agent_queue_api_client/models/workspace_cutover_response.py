from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_cutover_snapshot import WorkspaceCutoverSnapshot


T = TypeVar("T", bound="WorkspaceCutoverResponse")


@_attrs_define
class WorkspaceCutoverResponse:
    """
    Attributes:
        success (bool):
        error (None | str | Unset):
        applied (bool | None | Unset):
        proof (None | str | Unset):
        snapshot (None | Unset | WorkspaceCutoverSnapshot):
        retained (list[str] | Unset):
        retirement (None | str | Unset):
    """

    success: bool
    error: None | str | Unset = UNSET
    applied: bool | None | Unset = UNSET
    proof: None | str | Unset = UNSET
    snapshot: None | Unset | WorkspaceCutoverSnapshot = UNSET
    retained: list[str] | Unset = UNSET
    retirement: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.workspace_cutover_snapshot import WorkspaceCutoverSnapshot

        success = self.success

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        applied: bool | None | Unset
        if isinstance(self.applied, Unset):
            applied = UNSET
        else:
            applied = self.applied

        proof: None | str | Unset
        if isinstance(self.proof, Unset):
            proof = UNSET
        else:
            proof = self.proof

        snapshot: dict[str, Any] | None | Unset
        if isinstance(self.snapshot, Unset):
            snapshot = UNSET
        elif isinstance(self.snapshot, WorkspaceCutoverSnapshot):
            snapshot = self.snapshot.to_dict()
        else:
            snapshot = self.snapshot

        retained: list[str] | Unset = UNSET
        if not isinstance(self.retained, Unset):
            retained = self.retained

        retirement: None | str | Unset
        if isinstance(self.retirement, Unset):
            retirement = UNSET
        else:
            retirement = self.retirement

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if error is not UNSET:
            field_dict["error"] = error
        if applied is not UNSET:
            field_dict["applied"] = applied
        if proof is not UNSET:
            field_dict["proof"] = proof
        if snapshot is not UNSET:
            field_dict["snapshot"] = snapshot
        if retained is not UNSET:
            field_dict["retained"] = retained
        if retirement is not UNSET:
            field_dict["retirement"] = retirement

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_cutover_snapshot import WorkspaceCutoverSnapshot

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_applied(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        applied = _parse_applied(d.pop("applied", UNSET))

        def _parse_proof(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        proof = _parse_proof(d.pop("proof", UNSET))

        def _parse_snapshot(data: object) -> None | Unset | WorkspaceCutoverSnapshot:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                snapshot_type_0 = WorkspaceCutoverSnapshot.from_dict(data)

                return snapshot_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WorkspaceCutoverSnapshot, data)

        snapshot = _parse_snapshot(d.pop("snapshot", UNSET))

        retained = cast(list[str], d.pop("retained", UNSET))

        def _parse_retirement(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retirement = _parse_retirement(d.pop("retirement", UNSET))

        workspace_cutover_response = cls(
            success=success,
            error=error,
            applied=applied,
            proof=proof,
            snapshot=snapshot,
            retained=retained,
            retirement=retirement,
        )

        workspace_cutover_response.additional_properties = d
        return workspace_cutover_response

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
