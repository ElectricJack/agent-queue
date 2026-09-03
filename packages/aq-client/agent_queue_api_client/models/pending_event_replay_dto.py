from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.pending_event_replay_dto_policy import PendingEventReplayDTOPolicy
from ..types import UNSET, Unset

T = TypeVar("T", bound="PendingEventReplayDTO")


@_attrs_define
class PendingEventReplayDTO:
    """What ``playbooks.v2_pending_event_replay_on_activation`` did here.

    Always present on an activation response, including under the default
    ``manual`` policy, so an empty backlog and a policy that never looked at
    one are distinguishable without reading the daemon's config.
    ``refused_reason`` is the fail-closed path: the policy is ``automatic``
    but the activation was not a ready, enabled one — most importantly
    ``question_required``, where an unreviewed playbook may not auto-consume
    a backlog.

        Attributes:
            policy (PendingEventReplayDTOPolicy | Unset):  Default: PendingEventReplayDTOPolicy.MANUAL.
            replayed (bool | Unset):  Default: False.
            refused_reason (None | str | Unset):
            considered (int | Unset):  Default: 0.
            dispatched_run_ids (list[str] | Unset):
            skipped (list[str] | Unset):
            errors (list[str] | Unset):
    """

    policy: PendingEventReplayDTOPolicy | Unset = PendingEventReplayDTOPolicy.MANUAL
    replayed: bool | Unset = False
    refused_reason: None | str | Unset = UNSET
    considered: int | Unset = 0
    dispatched_run_ids: list[str] | Unset = UNSET
    skipped: list[str] | Unset = UNSET
    errors: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        policy: str | Unset = UNSET
        if not isinstance(self.policy, Unset):
            policy = self.policy.value

        replayed = self.replayed

        refused_reason: None | str | Unset
        if isinstance(self.refused_reason, Unset):
            refused_reason = UNSET
        else:
            refused_reason = self.refused_reason

        considered = self.considered

        dispatched_run_ids: list[str] | Unset = UNSET
        if not isinstance(self.dispatched_run_ids, Unset):
            dispatched_run_ids = self.dispatched_run_ids

        skipped: list[str] | Unset = UNSET
        if not isinstance(self.skipped, Unset):
            skipped = self.skipped

        errors: list[str] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if policy is not UNSET:
            field_dict["policy"] = policy
        if replayed is not UNSET:
            field_dict["replayed"] = replayed
        if refused_reason is not UNSET:
            field_dict["refused_reason"] = refused_reason
        if considered is not UNSET:
            field_dict["considered"] = considered
        if dispatched_run_ids is not UNSET:
            field_dict["dispatched_run_ids"] = dispatched_run_ids
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        _policy = d.pop("policy", UNSET)
        policy: PendingEventReplayDTOPolicy | Unset
        if isinstance(_policy, Unset):
            policy = UNSET
        else:
            policy = PendingEventReplayDTOPolicy(_policy)

        replayed = d.pop("replayed", UNSET)

        def _parse_refused_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        refused_reason = _parse_refused_reason(d.pop("refused_reason", UNSET))

        considered = d.pop("considered", UNSET)

        dispatched_run_ids = cast(list[str], d.pop("dispatched_run_ids", UNSET))

        skipped = cast(list[str], d.pop("skipped", UNSET))

        errors = cast(list[str], d.pop("errors", UNSET))

        pending_event_replay_dto = cls(
            policy=policy,
            replayed=replayed,
            refused_reason=refused_reason,
            considered=considered,
            dispatched_run_ids=dispatched_run_ids,
            skipped=skipped,
            errors=errors,
        )

        return pending_event_replay_dto
