from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CiRepairAdoptRequest")


@_attrs_define
class CiRepairAdoptRequest:
    """
    Attributes:
        project_id (str): Project the task belongs to.
        task_id (str): The live task that repairs the failure.
        ref (None | str | Unset): Branch the failure is on. Default: the project's default branch.
        head_sha (None | str | Unset): Commit the failure was read at, when the caller read it.
        failing_tests (list[Any] | None | Unset): Pytest node ids the repair owns. Omit this and failing_checks to read
            the branch's CI now.
        failing_checks (list[Any] | None | Unset): Failing check names.
    """

    project_id: str
    task_id: str
    ref: None | str | Unset = UNSET
    head_sha: None | str | Unset = UNSET
    failing_tests: list[Any] | None | Unset = UNSET
    failing_checks: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        task_id = self.task_id

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

        failing_tests: list[Any] | None | Unset
        if isinstance(self.failing_tests, Unset):
            failing_tests = UNSET
        elif isinstance(self.failing_tests, list):
            failing_tests = self.failing_tests

        else:
            failing_tests = self.failing_tests

        failing_checks: list[Any] | None | Unset
        if isinstance(self.failing_checks, Unset):
            failing_checks = UNSET
        elif isinstance(self.failing_checks, list):
            failing_checks = self.failing_checks

        else:
            failing_checks = self.failing_checks

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "task_id": task_id,
            }
        )
        if ref is not UNSET:
            field_dict["ref"] = ref
        if head_sha is not UNSET:
            field_dict["head_sha"] = head_sha
        if failing_tests is not UNSET:
            field_dict["failing_tests"] = failing_tests
        if failing_checks is not UNSET:
            field_dict["failing_checks"] = failing_checks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        task_id = d.pop("task_id")

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

        def _parse_failing_tests(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                failing_tests_type_0 = cast(list[Any], data)

                return failing_tests_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        failing_tests = _parse_failing_tests(d.pop("failing_tests", UNSET))

        def _parse_failing_checks(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                failing_checks_type_0 = cast(list[Any], data)

                return failing_checks_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        failing_checks = _parse_failing_checks(d.pop("failing_checks", UNSET))

        ci_repair_adopt_request = cls(
            project_id=project_id,
            task_id=task_id,
            ref=ref,
            head_sha=head_sha,
            failing_tests=failing_tests,
            failing_checks=failing_checks,
        )

        ci_repair_adopt_request.additional_properties = d
        return ci_repair_adopt_request

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
