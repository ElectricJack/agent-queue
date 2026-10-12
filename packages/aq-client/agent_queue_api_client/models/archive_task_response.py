from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.archive_task_response_delivery_type_0 import ArchiveTaskResponseDeliveryType0
    from ..models.archive_task_response_undelivered_item import ArchiveTaskResponseUndeliveredItem


T = TypeVar("T", bound="ArchiveTaskResponse")


@_attrs_define
class ArchiveTaskResponse:
    """
    Attributes:
        archived (str):
        title (str):
        status (str | Unset):  Default: ''.
        disposition (str | Unset):  Default: ''.
        kept (str | Unset):  Default: ''.
        undelivered (list[ArchiveTaskResponseUndeliveredItem] | Unset):
        delivery (ArchiveTaskResponseDeliveryType0 | None | Unset):
    """

    archived: str
    title: str
    status: str | Unset = ""
    disposition: str | Unset = ""
    kept: str | Unset = ""
    undelivered: list[ArchiveTaskResponseUndeliveredItem] | Unset = UNSET
    delivery: ArchiveTaskResponseDeliveryType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.archive_task_response_delivery_type_0 import ArchiveTaskResponseDeliveryType0

        archived = self.archived

        title = self.title

        status = self.status

        disposition = self.disposition

        kept = self.kept

        undelivered: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.undelivered, Unset):
            undelivered = []
            for undelivered_item_data in self.undelivered:
                undelivered_item = undelivered_item_data.to_dict()
                undelivered.append(undelivered_item)

        delivery: dict[str, Any] | None | Unset
        if isinstance(self.delivery, Unset):
            delivery = UNSET
        elif isinstance(self.delivery, ArchiveTaskResponseDeliveryType0):
            delivery = self.delivery.to_dict()
        else:
            delivery = self.delivery

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "archived": archived,
                "title": title,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if disposition is not UNSET:
            field_dict["disposition"] = disposition
        if kept is not UNSET:
            field_dict["kept"] = kept
        if undelivered is not UNSET:
            field_dict["undelivered"] = undelivered
        if delivery is not UNSET:
            field_dict["delivery"] = delivery

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.archive_task_response_delivery_type_0 import ArchiveTaskResponseDeliveryType0
        from ..models.archive_task_response_undelivered_item import ArchiveTaskResponseUndeliveredItem

        d = dict(src_dict)
        archived = d.pop("archived")

        title = d.pop("title")

        status = d.pop("status", UNSET)

        disposition = d.pop("disposition", UNSET)

        kept = d.pop("kept", UNSET)

        _undelivered = d.pop("undelivered", UNSET)
        undelivered: list[ArchiveTaskResponseUndeliveredItem] | Unset = UNSET
        if _undelivered is not UNSET:
            undelivered = []
            for undelivered_item_data in _undelivered:
                undelivered_item = ArchiveTaskResponseUndeliveredItem.from_dict(undelivered_item_data)

                undelivered.append(undelivered_item)

        def _parse_delivery(data: object) -> ArchiveTaskResponseDeliveryType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                delivery_type_0 = ArchiveTaskResponseDeliveryType0.from_dict(data)

                return delivery_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ArchiveTaskResponseDeliveryType0 | None | Unset, data)

        delivery = _parse_delivery(d.pop("delivery", UNSET))

        archive_task_response = cls(
            archived=archived,
            title=title,
            status=status,
            disposition=disposition,
            kept=kept,
            undelivered=undelivered,
            delivery=delivery,
        )

        archive_task_response.additional_properties = d
        return archive_task_response

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
