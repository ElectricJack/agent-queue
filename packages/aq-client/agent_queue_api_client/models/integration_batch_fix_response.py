from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.integration_batch_fix_response_fixer_type_0 import IntegrationBatchFixResponseFixerType0


T = TypeVar("T", bound="IntegrationBatchFixResponse")


@_attrs_define
class IntegrationBatchFixResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        fixer (IntegrationBatchFixResponseFixerType0 | None | Unset):
        head_sha (None | str | Unset):
        branch (None | str | Unset):
        error_code (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    fixer: IntegrationBatchFixResponseFixerType0 | None | Unset = UNSET
    head_sha: None | str | Unset = UNSET
    branch: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.integration_batch_fix_response_fixer_type_0 import IntegrationBatchFixResponseFixerType0

        success = self.success

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        fixer: dict[str, Any] | None | Unset
        if isinstance(self.fixer, Unset):
            fixer = UNSET
        elif isinstance(self.fixer, IntegrationBatchFixResponseFixerType0):
            fixer = self.fixer.to_dict()
        else:
            fixer = self.fixer

        head_sha: None | str | Unset
        if isinstance(self.head_sha, Unset):
            head_sha = UNSET
        else:
            head_sha = self.head_sha

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if fixer is not UNSET:
            field_dict["fixer"] = fixer
        if head_sha is not UNSET:
            field_dict["head_sha"] = head_sha
        if branch is not UNSET:
            field_dict["branch"] = branch
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.integration_batch_fix_response_fixer_type_0 import IntegrationBatchFixResponseFixerType0

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_fixer(data: object) -> IntegrationBatchFixResponseFixerType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                fixer_type_0 = IntegrationBatchFixResponseFixerType0.from_dict(data)

                return fixer_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IntegrationBatchFixResponseFixerType0 | None | Unset, data)

        fixer = _parse_fixer(d.pop("fixer", UNSET))

        def _parse_head_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        head_sha = _parse_head_sha(d.pop("head_sha", UNSET))

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        integration_batch_fix_response = cls(
            success=success,
            outcome=outcome,
            fixer=fixer,
            head_sha=head_sha,
            branch=branch,
            error_code=error_code,
            error=error,
        )

        integration_batch_fix_response.additional_properties = d
        return integration_batch_fix_response

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
