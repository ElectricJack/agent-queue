from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CiRepairAdoptResponse")


@_attrs_define
class CiRepairAdoptResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        outcome (str | Unset):  Default: ''.
        task_id (str | Unset):  Default: ''.
        dedup_key (str | Unset):  Default: ''.
        ref (None | str | Unset):
        head_sha (None | str | Unset):
        signature (None | str | Unset):
        failing_tests (list[str] | Unset):
        failing_checks (list[str] | Unset):
        in_flight (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool | Unset = True
    outcome: str | Unset = ""
    task_id: str | Unset = ""
    dedup_key: str | Unset = ""
    ref: None | str | Unset = UNSET
    head_sha: None | str | Unset = UNSET
    signature: None | str | Unset = UNSET
    failing_tests: list[str] | Unset = UNSET
    failing_checks: list[str] | Unset = UNSET
    in_flight: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        outcome = self.outcome

        task_id = self.task_id

        dedup_key = self.dedup_key

        ref: None | str | Unset
        if isinstance(self.ref, Unset):
            ref = UNSET
        else:
            ref = self.ref

        head_sha: None | str | Unset
        if isinstance(self.head_sha, Unset):
            head_sha = UNSET
        else:
            head_sha = self.head_sha

        signature: None | str | Unset
        if isinstance(self.signature, Unset):
            signature = UNSET
        else:
            signature = self.signature

        failing_tests: list[str] | Unset = UNSET
        if not isinstance(self.failing_tests, Unset):
            failing_tests = self.failing_tests

        failing_checks: list[str] | Unset = UNSET
        if not isinstance(self.failing_checks, Unset):
            failing_checks = self.failing_checks

        in_flight: list[str] | Unset = UNSET
        if not isinstance(self.in_flight, Unset):
            in_flight = self.in_flight

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
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if dedup_key is not UNSET:
            field_dict["dedup_key"] = dedup_key
        if ref is not UNSET:
            field_dict["ref"] = ref
        if head_sha is not UNSET:
            field_dict["head_sha"] = head_sha
        if signature is not UNSET:
            field_dict["signature"] = signature
        if failing_tests is not UNSET:
            field_dict["failing_tests"] = failing_tests
        if failing_checks is not UNSET:
            field_dict["failing_checks"] = failing_checks
        if in_flight is not UNSET:
            field_dict["in_flight"] = in_flight
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        outcome = d.pop("outcome", UNSET)

        task_id = d.pop("task_id", UNSET)

        dedup_key = d.pop("dedup_key", UNSET)

        def _parse_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ref = _parse_ref(d.pop("ref", UNSET))

        def _parse_head_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        head_sha = _parse_head_sha(d.pop("head_sha", UNSET))

        def _parse_signature(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        signature = _parse_signature(d.pop("signature", UNSET))

        failing_tests = cast(list[str], d.pop("failing_tests", UNSET))

        failing_checks = cast(list[str], d.pop("failing_checks", UNSET))

        in_flight = cast(list[str], d.pop("in_flight", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        ci_repair_adopt_response = cls(
            success=success,
            outcome=outcome,
            task_id=task_id,
            dedup_key=dedup_key,
            ref=ref,
            head_sha=head_sha,
            signature=signature,
            failing_tests=failing_tests,
            failing_checks=failing_checks,
            in_flight=in_flight,
            error=error,
        )

        ci_repair_adopt_response.additional_properties = d
        return ci_repair_adopt_response

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
