from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProvenanceRef")


@_attrs_define
class ProvenanceRef:
    """The task at the far end of a non-blocking edge *out of* this one.

    Provenance edges are **outgoing**, pointing from the task toward its
    origin — the same direction as ``depends_on``, which is why they share
    a query.  Today the only kind is ``discovered-from``, so this is the
    task a worker was holding when it filed the one being inspected.

        Attributes:
            id (str):
            title (str | Unset):  Default: ''.
            status (str | Unset):  Default: ''.
            dep_type (str | Unset):  Default: ''.
            reason (None | str | Unset):
    """

    id: str
    title: str | Unset = ""
    status: str | Unset = ""
    dep_type: str | Unset = ""
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        title = self.title

        status = self.status

        dep_type = self.dep_type

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if title is not UNSET:
            field_dict["title"] = title
        if status is not UNSET:
            field_dict["status"] = status
        if dep_type is not UNSET:
            field_dict["dep_type"] = dep_type
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title", UNSET)

        status = d.pop("status", UNSET)

        dep_type = d.pop("dep_type", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        provenance_ref = cls(
            id=id,
            title=title,
            status=status,
            dep_type=dep_type,
            reason=reason,
        )

        provenance_ref.additional_properties = d
        return provenance_ref

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
