from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ci_policy import CIPolicy
    from ..models.selection_areas import SelectionAreas
    from ..models.selection_policy import SelectionPolicy
    from ..models.selection_rules import SelectionRules


T = TypeVar("T", bound="CITestPolicy")


@_attrs_define
class CITestPolicy:
    """
    Attributes:
        type_ (Literal['ci_test'] | Unset):  Default: 'ci_test'.
        ci (CIPolicy | None | Unset):
        selection_rules (None | SelectionRules | Unset):
        selection_areas (None | SelectionAreas | Unset):
        selection_policy (None | SelectionPolicy | Unset):
    """

    type_: Literal["ci_test"] | Unset = "ci_test"
    ci: CIPolicy | None | Unset = UNSET
    selection_rules: None | SelectionRules | Unset = UNSET
    selection_areas: None | SelectionAreas | Unset = UNSET
    selection_policy: None | SelectionPolicy | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.ci_policy import CIPolicy
        from ..models.selection_areas import SelectionAreas
        from ..models.selection_policy import SelectionPolicy
        from ..models.selection_rules import SelectionRules

        type_ = self.type_

        ci: dict[str, Any] | None | Unset
        if isinstance(self.ci, Unset):
            ci = UNSET
        elif isinstance(self.ci, CIPolicy):
            ci = self.ci.to_dict()
        else:
            ci = self.ci

        selection_rules: dict[str, Any] | None | Unset
        if isinstance(self.selection_rules, Unset):
            selection_rules = UNSET
        elif isinstance(self.selection_rules, SelectionRules):
            selection_rules = self.selection_rules.to_dict()
        else:
            selection_rules = self.selection_rules

        selection_areas: dict[str, Any] | None | Unset
        if isinstance(self.selection_areas, Unset):
            selection_areas = UNSET
        elif isinstance(self.selection_areas, SelectionAreas):
            selection_areas = self.selection_areas.to_dict()
        else:
            selection_areas = self.selection_areas

        selection_policy: dict[str, Any] | None | Unset
        if isinstance(self.selection_policy, Unset):
            selection_policy = UNSET
        elif isinstance(self.selection_policy, SelectionPolicy):
            selection_policy = self.selection_policy.to_dict()
        else:
            selection_policy = self.selection_policy

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if type_ is not UNSET:
            field_dict["type"] = type_
        if ci is not UNSET:
            field_dict["ci"] = ci
        if selection_rules is not UNSET:
            field_dict["selection_rules"] = selection_rules
        if selection_areas is not UNSET:
            field_dict["selection_areas"] = selection_areas
        if selection_policy is not UNSET:
            field_dict["selection_policy"] = selection_policy

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ci_policy import CIPolicy
        from ..models.selection_areas import SelectionAreas
        from ..models.selection_policy import SelectionPolicy
        from ..models.selection_rules import SelectionRules

        d = dict(src_dict)
        type_ = cast(Literal["ci_test"] | Unset, d.pop("type", UNSET))
        if type_ != "ci_test" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'ci_test', got '{type_}'")

        def _parse_ci(data: object) -> CIPolicy | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                ci_type_0 = CIPolicy.from_dict(data)

                return ci_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CIPolicy | None | Unset, data)

        ci = _parse_ci(d.pop("ci", UNSET))

        def _parse_selection_rules(data: object) -> None | SelectionRules | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                selection_rules_type_0 = SelectionRules.from_dict(data)

                return selection_rules_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SelectionRules | Unset, data)

        selection_rules = _parse_selection_rules(d.pop("selection_rules", UNSET))

        def _parse_selection_areas(data: object) -> None | SelectionAreas | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                selection_areas_type_0 = SelectionAreas.from_dict(data)

                return selection_areas_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SelectionAreas | Unset, data)

        selection_areas = _parse_selection_areas(d.pop("selection_areas", UNSET))

        def _parse_selection_policy(data: object) -> None | SelectionPolicy | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                selection_policy_type_0 = SelectionPolicy.from_dict(data)

                return selection_policy_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SelectionPolicy | Unset, data)

        selection_policy = _parse_selection_policy(d.pop("selection_policy", UNSET))

        ci_test_policy = cls(
            type_=type_,
            ci=ci,
            selection_rules=selection_rules,
            selection_areas=selection_areas,
            selection_policy=selection_policy,
        )

        return ci_test_policy
