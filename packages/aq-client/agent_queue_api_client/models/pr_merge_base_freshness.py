from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PrMergeBaseFreshness")


@_attrs_define
class PrMergeBaseFreshness:
    """Whether the PR head contains its base branch's tip.

    GitHub's "Require branches to be up to date before merging", asked on
    the fleet's own merge path.  A green rollup only says the head passed
    against the base *as it was when the run started*; ``stale`` means the
    base has moved since and the merged result is a combination nothing
    has tested (PRs #390 + #391, 2026-09-03).

        Attributes:
            ref (str | Unset):  Default: ''.
            behind_by (int | None | Unset):
            state (str | Unset):  Default: ''.
    """

    ref: str | Unset = ""
    behind_by: int | None | Unset = UNSET
    state: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ref = self.ref

        behind_by: int | None | Unset
        if isinstance(self.behind_by, Unset):
            behind_by = UNSET
        else:
            behind_by = self.behind_by

        state = self.state

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if ref is not UNSET:
            field_dict["ref"] = ref
        if behind_by is not UNSET:
            field_dict["behind_by"] = behind_by
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ref = d.pop("ref", UNSET)

        def _parse_behind_by(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        behind_by = _parse_behind_by(d.pop("behind_by", UNSET))

        state = d.pop("state", UNSET)

        pr_merge_base_freshness = cls(
            ref=ref,
            behind_by=behind_by,
            state=state,
        )

        pr_merge_base_freshness.additional_properties = d
        return pr_merge_base_freshness

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
