from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_pending_event_action_response_action import PlaybookPendingEventActionResponseAction
from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookPendingEventActionResponse")


@_attrs_define
class PlaybookPendingEventActionResponse:
    """
    Attributes:
        action (PlaybookPendingEventActionResponseAction):
        success (bool | Unset):  Default: True.
        requested (int | Unset):  Default: 0.
        dispatched_run_ids (list[str] | Unset):
        discarded_ids (list[str] | Unset):
        skipped (list[str] | Unset):
        errors (list[str] | Unset):
    """

    action: PlaybookPendingEventActionResponseAction
    success: bool | Unset = True
    requested: int | Unset = 0
    dispatched_run_ids: list[str] | Unset = UNSET
    discarded_ids: list[str] | Unset = UNSET
    skipped: list[str] | Unset = UNSET
    errors: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        action = self.action.value

        success = self.success

        requested = self.requested

        dispatched_run_ids: list[str] | Unset = UNSET
        if not isinstance(self.dispatched_run_ids, Unset):
            dispatched_run_ids = self.dispatched_run_ids

        discarded_ids: list[str] | Unset = UNSET
        if not isinstance(self.discarded_ids, Unset):
            discarded_ids = self.discarded_ids

        skipped: list[str] | Unset = UNSET
        if not isinstance(self.skipped, Unset):
            skipped = self.skipped

        errors: list[str] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "action": action,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if requested is not UNSET:
            field_dict["requested"] = requested
        if dispatched_run_ids is not UNSET:
            field_dict["dispatched_run_ids"] = dispatched_run_ids
        if discarded_ids is not UNSET:
            field_dict["discarded_ids"] = discarded_ids
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        action = PlaybookPendingEventActionResponseAction(d.pop("action"))

        success = d.pop("success", UNSET)

        requested = d.pop("requested", UNSET)

        dispatched_run_ids = cast(list[str], d.pop("dispatched_run_ids", UNSET))

        discarded_ids = cast(list[str], d.pop("discarded_ids", UNSET))

        skipped = cast(list[str], d.pop("skipped", UNSET))

        errors = cast(list[str], d.pop("errors", UNSET))

        playbook_pending_event_action_response = cls(
            action=action,
            success=success,
            requested=requested,
            dispatched_run_ids=dispatched_run_ids,
            discarded_ids=discarded_ids,
            skipped=skipped,
            errors=errors,
        )

        return playbook_pending_event_action_response
