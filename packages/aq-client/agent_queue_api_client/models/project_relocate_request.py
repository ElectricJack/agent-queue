from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.project_relocate_request_owner_inventory import ProjectRelocateRequestOwnerInventory


T = TypeVar("T", bound="ProjectRelocateRequest")


@_attrs_define
class ProjectRelocateRequest:
    """
    Attributes:
        project_id (str):
        destination (str):
        owner_inventory (ProjectRelocateRequestOwnerInventory):
        expected_digest (None | str | Unset):
        apply (bool | Unset):  Default: False.
    """

    project_id: str
    destination: str
    owner_inventory: ProjectRelocateRequestOwnerInventory
    expected_digest: None | str | Unset = UNSET
    apply: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        destination = self.destination

        owner_inventory = self.owner_inventory.to_dict()

        expected_digest: None | str | Unset
        if isinstance(self.expected_digest, Unset):
            expected_digest = UNSET
        else:
            expected_digest = self.expected_digest

        apply = self.apply

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "destination": destination,
                "owner_inventory": owner_inventory,
            }
        )
        if expected_digest is not UNSET:
            field_dict["expected_digest"] = expected_digest
        if apply is not UNSET:
            field_dict["apply"] = apply

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.project_relocate_request_owner_inventory import ProjectRelocateRequestOwnerInventory

        d = dict(src_dict)
        project_id = d.pop("project_id")

        destination = d.pop("destination")

        owner_inventory = ProjectRelocateRequestOwnerInventory.from_dict(d.pop("owner_inventory"))

        def _parse_expected_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_digest = _parse_expected_digest(d.pop("expected_digest", UNSET))

        apply = d.pop("apply", UNSET)

        project_relocate_request = cls(
            project_id=project_id,
            destination=destination,
            owner_inventory=owner_inventory,
            expected_digest=expected_digest,
            apply=apply,
        )

        project_relocate_request.additional_properties = d
        return project_relocate_request

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
