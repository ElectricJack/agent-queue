from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pr_merge_base_freshness import PrMergeBaseFreshness


T = TypeVar("T", bound="PrMergeCiVerdict")


@_attrs_define
class PrMergeCiVerdict:
    """What the PR head's status checks said, and what the policy did about it.

    Present on every ``pr_merge`` response unless
    ``integration.merge_ci_policy`` is ``off`` (in which case CI is never
    consulted).  Under ``warn`` the merge happens regardless and this block
    is the record of what landed; under ``required`` a ``blocked`` verdict
    is why ``success`` is false.

        Attributes:
            policy (str | Unset):  Default: ''.
            state (str | Unset):  Default: ''.
            summary (str | Unset):  Default: ''.
            failing (list[str] | Unset):
            pending (list[str] | Unset):
            missing (list[str] | Unset):
            base (None | PrMergeBaseFreshness | Unset):
            blocked (bool | Unset):  Default: False.
            forced (bool | Unset):  Default: False.
            message (str | Unset):  Default: ''.
    """

    policy: str | Unset = ""
    state: str | Unset = ""
    summary: str | Unset = ""
    failing: list[str] | Unset = UNSET
    pending: list[str] | Unset = UNSET
    missing: list[str] | Unset = UNSET
    base: None | PrMergeBaseFreshness | Unset = UNSET
    blocked: bool | Unset = False
    forced: bool | Unset = False
    message: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.pr_merge_base_freshness import PrMergeBaseFreshness

        policy = self.policy

        state = self.state

        summary = self.summary

        failing: list[str] | Unset = UNSET
        if not isinstance(self.failing, Unset):
            failing = self.failing

        pending: list[str] | Unset = UNSET
        if not isinstance(self.pending, Unset):
            pending = self.pending

        missing: list[str] | Unset = UNSET
        if not isinstance(self.missing, Unset):
            missing = self.missing

        base: dict[str, Any] | None | Unset
        if isinstance(self.base, Unset):
            base = UNSET
        elif isinstance(self.base, PrMergeBaseFreshness):
            base = self.base.to_dict()
        else:
            base = self.base

        blocked = self.blocked

        forced = self.forced

        message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if policy is not UNSET:
            field_dict["policy"] = policy
        if state is not UNSET:
            field_dict["state"] = state
        if summary is not UNSET:
            field_dict["summary"] = summary
        if failing is not UNSET:
            field_dict["failing"] = failing
        if pending is not UNSET:
            field_dict["pending"] = pending
        if missing is not UNSET:
            field_dict["missing"] = missing
        if base is not UNSET:
            field_dict["base"] = base
        if blocked is not UNSET:
            field_dict["blocked"] = blocked
        if forced is not UNSET:
            field_dict["forced"] = forced
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pr_merge_base_freshness import PrMergeBaseFreshness

        d = dict(src_dict)
        policy = d.pop("policy", UNSET)

        state = d.pop("state", UNSET)

        summary = d.pop("summary", UNSET)

        failing = cast(list[str], d.pop("failing", UNSET))

        pending = cast(list[str], d.pop("pending", UNSET))

        missing = cast(list[str], d.pop("missing", UNSET))

        def _parse_base(data: object) -> None | PrMergeBaseFreshness | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                base_type_0 = PrMergeBaseFreshness.from_dict(data)

                return base_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PrMergeBaseFreshness | Unset, data)

        base = _parse_base(d.pop("base", UNSET))

        blocked = d.pop("blocked", UNSET)

        forced = d.pop("forced", UNSET)

        message = d.pop("message", UNSET)

        pr_merge_ci_verdict = cls(
            policy=policy,
            state=state,
            summary=summary,
            failing=failing,
            pending=pending,
            missing=missing,
            base=base,
            blocked=blocked,
            forced=forced,
            message=message,
        )

        pr_merge_ci_verdict.additional_properties = d
        return pr_merge_ci_verdict

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
