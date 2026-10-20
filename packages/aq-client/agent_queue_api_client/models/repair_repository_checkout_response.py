from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.repair_repository_checkout_response_after import RepairRepositoryCheckoutResponseAfter
    from ..models.repair_repository_checkout_response_before import RepairRepositoryCheckoutResponseBefore


T = TypeVar("T", bound="RepairRepositoryCheckoutResponse")


@_attrs_define
class RepairRepositoryCheckoutResponse:
    """
    Attributes:
        project_id (str):
        repository_id (str):
        before (RepairRepositoryCheckoutResponseBefore):
        after (RepairRepositoryCheckoutResponseAfter):
        success (bool | Unset):  Default: True.
        changed (bool | Unset):  Default: False.
        preview (bool | Unset):  Default: True.
        event_id (int | None | Unset):
    """

    project_id: str
    repository_id: str
    before: RepairRepositoryCheckoutResponseBefore
    after: RepairRepositoryCheckoutResponseAfter
    success: bool | Unset = True
    changed: bool | Unset = False
    preview: bool | Unset = True
    event_id: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        repository_id = self.repository_id

        before = self.before.to_dict()

        after = self.after.to_dict()

        success = self.success

        changed = self.changed

        preview = self.preview

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
                "repository_id": repository_id,
                "before": before,
                "after": after,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if changed is not UNSET:
            field_dict["changed"] = changed
        if preview is not UNSET:
            field_dict["preview"] = preview
        if event_id is not UNSET:
            field_dict["event_id"] = event_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.repair_repository_checkout_response_after import RepairRepositoryCheckoutResponseAfter
        from ..models.repair_repository_checkout_response_before import RepairRepositoryCheckoutResponseBefore

        d = dict(src_dict)
        project_id = d.pop("project_id")

        repository_id = d.pop("repository_id")

        before = RepairRepositoryCheckoutResponseBefore.from_dict(d.pop("before"))

        after = RepairRepositoryCheckoutResponseAfter.from_dict(d.pop("after"))

        success = d.pop("success", UNSET)

        changed = d.pop("changed", UNSET)

        preview = d.pop("preview", UNSET)

        def _parse_event_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        repair_repository_checkout_response = cls(
            project_id=project_id,
            repository_id=repository_id,
            before=before,
            after=after,
            success=success,
            changed=changed,
            preview=preview,
            event_id=event_id,
        )

        repair_repository_checkout_response.additional_properties = d
        return repair_repository_checkout_response

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
