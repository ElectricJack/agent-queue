from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.repair_workspace_checkout_response_after import RepairWorkspaceCheckoutResponseAfter
    from ..models.repair_workspace_checkout_response_before import RepairWorkspaceCheckoutResponseBefore
    from ..models.repair_workspace_checkout_response_identity import RepairWorkspaceCheckoutResponseIdentity
    from ..models.repair_workspace_checkout_response_slots_item import RepairWorkspaceCheckoutResponseSlotsItem


T = TypeVar("T", bound="RepairWorkspaceCheckoutResponse")


@_attrs_define
class RepairWorkspaceCheckoutResponse:
    """
    Attributes:
        project_id (str):
        workspace_id (str):
        proof (str):
        before (RepairWorkspaceCheckoutResponseBefore):
        after (RepairWorkspaceCheckoutResponseAfter):
        slots (list[RepairWorkspaceCheckoutResponseSlotsItem]):
        identity (RepairWorkspaceCheckoutResponseIdentity):
        reason (str):
        success (bool | Unset):  Default: True.
        changed (bool | Unset):  Default: False.
        preview (bool | Unset):  Default: True.
        authorized_origin (None | str | Unset):
        event_id (int | None | Unset):
    """

    project_id: str
    workspace_id: str
    proof: str
    before: RepairWorkspaceCheckoutResponseBefore
    after: RepairWorkspaceCheckoutResponseAfter
    slots: list[RepairWorkspaceCheckoutResponseSlotsItem]
    identity: RepairWorkspaceCheckoutResponseIdentity
    reason: str
    success: bool | Unset = True
    changed: bool | Unset = False
    preview: bool | Unset = True
    authorized_origin: None | str | Unset = UNSET
    event_id: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        workspace_id = self.workspace_id

        proof = self.proof

        before = self.before.to_dict()

        after = self.after.to_dict()

        slots = []
        for slots_item_data in self.slots:
            slots_item = slots_item_data.to_dict()
            slots.append(slots_item)

        identity = self.identity.to_dict()

        reason = self.reason

        success = self.success

        changed = self.changed

        preview = self.preview

        authorized_origin: None | str | Unset
        if isinstance(self.authorized_origin, Unset):
            authorized_origin = UNSET
        else:
            authorized_origin = self.authorized_origin

        event_id: int | None | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "workspace_id": workspace_id,
                "proof": proof,
                "before": before,
                "after": after,
                "slots": slots,
                "identity": identity,
                "reason": reason,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if changed is not UNSET:
            field_dict["changed"] = changed
        if preview is not UNSET:
            field_dict["preview"] = preview
        if authorized_origin is not UNSET:
            field_dict["authorized_origin"] = authorized_origin
        if event_id is not UNSET:
            field_dict["event_id"] = event_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.repair_workspace_checkout_response_after import RepairWorkspaceCheckoutResponseAfter
        from ..models.repair_workspace_checkout_response_before import RepairWorkspaceCheckoutResponseBefore
        from ..models.repair_workspace_checkout_response_identity import RepairWorkspaceCheckoutResponseIdentity
        from ..models.repair_workspace_checkout_response_slots_item import RepairWorkspaceCheckoutResponseSlotsItem

        d = dict(src_dict)
        project_id = d.pop("project_id")

        workspace_id = d.pop("workspace_id")

        proof = d.pop("proof")

        before = RepairWorkspaceCheckoutResponseBefore.from_dict(d.pop("before"))

        after = RepairWorkspaceCheckoutResponseAfter.from_dict(d.pop("after"))

        slots = []
        _slots = d.pop("slots")
        for slots_item_data in _slots:
            slots_item = RepairWorkspaceCheckoutResponseSlotsItem.from_dict(slots_item_data)

            slots.append(slots_item)

        identity = RepairWorkspaceCheckoutResponseIdentity.from_dict(d.pop("identity"))

        reason = d.pop("reason")

        success = d.pop("success", UNSET)

        changed = d.pop("changed", UNSET)

        preview = d.pop("preview", UNSET)

        def _parse_authorized_origin(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        authorized_origin = _parse_authorized_origin(d.pop("authorized_origin", UNSET))

        def _parse_event_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        repair_workspace_checkout_response = cls(
            project_id=project_id,
            workspace_id=workspace_id,
            proof=proof,
            before=before,
            after=after,
            slots=slots,
            identity=identity,
            reason=reason,
            success=success,
            changed=changed,
            preview=preview,
            authorized_origin=authorized_origin,
            event_id=event_id,
        )

        repair_workspace_checkout_response.additional_properties = d
        return repair_workspace_checkout_response

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
