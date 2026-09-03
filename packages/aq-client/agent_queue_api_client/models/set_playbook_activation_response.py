from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.activation_state_dto import ActivationStateDTO
    from ..models.pending_event_replay_dto import PendingEventReplayDTO


T = TypeVar("T", bound="SetPlaybookActivationResponse")


@_attrs_define
class SetPlaybookActivationResponse:
    """
    Attributes:
        activation (ActivationStateDTO): ``enabled`` and ``health`` are independent (design spec).  A disabled
            activation still reports its computed health; ``health="disabled"`` is used
            only when there is no active artifact at all.
        success (bool | Unset):  Default: True.
        previous_artifact_sha256 (None | str | Unset):
        changed (bool | Unset):  Default: False.
        blocked (bool | Unset):  Default: False.
        blockers (list[str] | Unset):
        pending_event_replay (PendingEventReplayDTO | Unset): What ``playbooks.v2_pending_event_replay_on_activation``
            did here.

            Always present on an activation response, including under the default
            ``manual`` policy, so an empty backlog and a policy that never looked at
            one are distinguishable without reading the daemon's config.
            ``refused_reason`` is the fail-closed path: the policy is ``automatic``
            but the activation was not a ready, enabled one — most importantly
            ``question_required``, where an unreviewed playbook may not auto-consume
            a backlog.
    """

    activation: ActivationStateDTO
    success: bool | Unset = True
    previous_artifact_sha256: None | str | Unset = UNSET
    changed: bool | Unset = False
    blocked: bool | Unset = False
    blockers: list[str] | Unset = UNSET
    pending_event_replay: PendingEventReplayDTO | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        activation = self.activation.to_dict()

        success = self.success

        previous_artifact_sha256: None | str | Unset
        if isinstance(self.previous_artifact_sha256, Unset):
            previous_artifact_sha256 = UNSET
        else:
            previous_artifact_sha256 = self.previous_artifact_sha256

        changed = self.changed

        blocked = self.blocked

        blockers: list[str] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = self.blockers

        pending_event_replay: dict[str, Any] | Unset = UNSET
        if not isinstance(self.pending_event_replay, Unset):
            pending_event_replay = self.pending_event_replay.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "activation": activation,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if previous_artifact_sha256 is not UNSET:
            field_dict["previous_artifact_sha256"] = previous_artifact_sha256
        if changed is not UNSET:
            field_dict["changed"] = changed
        if blocked is not UNSET:
            field_dict["blocked"] = blocked
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if pending_event_replay is not UNSET:
            field_dict["pending_event_replay"] = pending_event_replay

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.activation_state_dto import ActivationStateDTO
        from ..models.pending_event_replay_dto import PendingEventReplayDTO

        d = dict(src_dict)
        activation = ActivationStateDTO.from_dict(d.pop("activation"))

        success = d.pop("success", UNSET)

        def _parse_previous_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        previous_artifact_sha256 = _parse_previous_artifact_sha256(d.pop("previous_artifact_sha256", UNSET))

        changed = d.pop("changed", UNSET)

        blocked = d.pop("blocked", UNSET)

        blockers = cast(list[str], d.pop("blockers", UNSET))

        _pending_event_replay = d.pop("pending_event_replay", UNSET)
        pending_event_replay: PendingEventReplayDTO | Unset
        if isinstance(_pending_event_replay, Unset):
            pending_event_replay = UNSET
        else:
            pending_event_replay = PendingEventReplayDTO.from_dict(_pending_event_replay)

        set_playbook_activation_response = cls(
            activation=activation,
            success=success,
            previous_artifact_sha256=previous_artifact_sha256,
            changed=changed,
            blocked=blocked,
            blockers=blockers,
            pending_event_replay=pending_event_replay,
        )

        return set_playbook_activation_response
