from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ci_policy_check_sets import CIPolicyCheckSets
    from ..models.required_checks import RequiredChecks


T = TypeVar("T", bound="CIPolicy")


@_attrs_define
class CIPolicy:
    """
    Attributes:
        required_checks (None | RequiredChecks | Unset):
        check_sets (CIPolicyCheckSets | Unset):
        promotion_attestation_names (list[str] | Unset):
    """

    required_checks: None | RequiredChecks | Unset = UNSET
    check_sets: CIPolicyCheckSets | Unset = UNSET
    promotion_attestation_names: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.required_checks import RequiredChecks

        required_checks: dict[str, Any] | None | Unset
        if isinstance(self.required_checks, Unset):
            required_checks = UNSET
        elif isinstance(self.required_checks, RequiredChecks):
            required_checks = self.required_checks.to_dict()
        else:
            required_checks = self.required_checks

        check_sets: dict[str, Any] | Unset = UNSET
        if not isinstance(self.check_sets, Unset):
            check_sets = self.check_sets.to_dict()

        promotion_attestation_names: list[str] | Unset = UNSET
        if not isinstance(self.promotion_attestation_names, Unset):
            promotion_attestation_names = self.promotion_attestation_names

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if required_checks is not UNSET:
            field_dict["required_checks"] = required_checks
        if check_sets is not UNSET:
            field_dict["check_sets"] = check_sets
        if promotion_attestation_names is not UNSET:
            field_dict["promotion_attestation_names"] = promotion_attestation_names

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ci_policy_check_sets import CIPolicyCheckSets
        from ..models.required_checks import RequiredChecks

        d = dict(src_dict)

        def _parse_required_checks(data: object) -> None | RequiredChecks | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                required_checks_type_0 = RequiredChecks.from_dict(data)

                return required_checks_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RequiredChecks | Unset, data)

        required_checks = _parse_required_checks(d.pop("required_checks", UNSET))

        _check_sets = d.pop("check_sets", UNSET)
        check_sets: CIPolicyCheckSets | Unset
        if isinstance(_check_sets, Unset):
            check_sets = UNSET
        else:
            check_sets = CIPolicyCheckSets.from_dict(_check_sets)

        promotion_attestation_names = cast(list[str], d.pop("promotion_attestation_names", UNSET))

        ci_policy = cls(
            required_checks=required_checks,
            check_sets=check_sets,
            promotion_attestation_names=promotion_attestation_names,
        )

        return ci_policy
