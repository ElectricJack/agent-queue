from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_v1_run_cancel_response_ownership_type_0 import PlaybookV1RunCancelResponseOwnershipType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookV1RunCancelResponse")


@_attrs_define
class PlaybookV1RunCancelResponse:
    """A cancel is only reported successful once the row is terminal *and* the
    coroutine that could have overwritten it is gone.

        Attributes:
            success (bool):
            run_id (None | str | Unset):
            ownership (None | PlaybookV1RunCancelResponseOwnershipType0 | Unset):
            status (None | str | Unset):
            completed_at (float | None | Unset):
            error (None | str | Unset):
    """

    success: bool
    run_id: None | str | Unset = UNSET
    ownership: None | PlaybookV1RunCancelResponseOwnershipType0 | Unset = UNSET
    status: None | str | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        ownership: None | str | Unset
        if isinstance(self.ownership, Unset):
            ownership = UNSET
        elif isinstance(self.ownership, PlaybookV1RunCancelResponseOwnershipType0):
            ownership = self.ownership.value
        else:
            ownership = self.ownership

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
            }
        )
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if ownership is not UNSET:
            field_dict["ownership"] = ownership
        if status is not UNSET:
            field_dict["status"] = status
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))

        def _parse_ownership(data: object) -> None | PlaybookV1RunCancelResponseOwnershipType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                ownership_type_0 = PlaybookV1RunCancelResponseOwnershipType0(data)

                return ownership_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookV1RunCancelResponseOwnershipType0 | Unset, data)

        ownership = _parse_ownership(d.pop("ownership", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_v1_run_cancel_response = cls(
            success=success,
            run_id=run_id,
            ownership=ownership,
            status=status,
            completed_at=completed_at,
            error=error,
        )

        return playbook_v1_run_cancel_response
