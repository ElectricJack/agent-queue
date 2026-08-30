from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PlaybookInstallRequest")


@_attrs_define
class PlaybookInstallRequest:
    """
    Attributes:
        playbook_id (str): The playbook id being installed. Must equal the artifact's own id.
        compiled_path (str): Path to the compiled .json artifact, inside the vault.
    """

    playbook_id: str
    compiled_path: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        compiled_path = self.compiled_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "compiled_path": compiled_path,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        compiled_path = d.pop("compiled_path")

        playbook_install_request = cls(
            playbook_id=playbook_id,
            compiled_path=compiled_path,
        )

        playbook_install_request.additional_properties = d
        return playbook_install_request

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
