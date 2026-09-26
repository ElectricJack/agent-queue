from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.conversation_backfill import ConversationBackfill
    from ..models.conversation_counts import ConversationCounts
    from ..models.conversation_diagnostics import ConversationDiagnostics
    from ..models.conversation_intake import ConversationIntake
    from ..models.conversation_limits import ConversationLimits
    from ..models.conversation_preconditions import ConversationPreconditions


T = TypeVar("T", bound="SupervisorInboxStatusResponse")


@_attrs_define
class SupervisorInboxStatusResponse:
    """
    Attributes:
        enabled (bool):
        preconditions (ConversationPreconditions):
        diagnostics (ConversationDiagnostics):
        limits (ConversationLimits):
        counts (ConversationCounts):
        backfill (ConversationBackfill):
        intake (ConversationIntake):
        success (bool | Unset):  Default: True.
    """

    enabled: bool
    preconditions: ConversationPreconditions
    diagnostics: ConversationDiagnostics
    limits: ConversationLimits
    counts: ConversationCounts
    backfill: ConversationBackfill
    intake: ConversationIntake
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        enabled = self.enabled

        preconditions = self.preconditions.to_dict()

        diagnostics = self.diagnostics.to_dict()

        limits = self.limits.to_dict()

        counts = self.counts.to_dict()

        backfill = self.backfill.to_dict()

        intake = self.intake.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "enabled": enabled,
                "preconditions": preconditions,
                "diagnostics": diagnostics,
                "limits": limits,
                "counts": counts,
                "backfill": backfill,
                "intake": intake,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.conversation_backfill import ConversationBackfill
        from ..models.conversation_counts import ConversationCounts
        from ..models.conversation_diagnostics import ConversationDiagnostics
        from ..models.conversation_intake import ConversationIntake
        from ..models.conversation_limits import ConversationLimits
        from ..models.conversation_preconditions import ConversationPreconditions

        d = dict(src_dict)
        enabled = d.pop("enabled")

        preconditions = ConversationPreconditions.from_dict(d.pop("preconditions"))

        diagnostics = ConversationDiagnostics.from_dict(d.pop("diagnostics"))

        limits = ConversationLimits.from_dict(d.pop("limits"))

        counts = ConversationCounts.from_dict(d.pop("counts"))

        backfill = ConversationBackfill.from_dict(d.pop("backfill"))

        intake = ConversationIntake.from_dict(d.pop("intake"))

        success = d.pop("success", UNSET)

        supervisor_inbox_status_response = cls(
            enabled=enabled,
            preconditions=preconditions,
            diagnostics=diagnostics,
            limits=limits,
            counts=counts,
            backfill=backfill,
            intake=intake,
            success=success,
        )

        supervisor_inbox_status_response.additional_properties = d
        return supervisor_inbox_status_response

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
