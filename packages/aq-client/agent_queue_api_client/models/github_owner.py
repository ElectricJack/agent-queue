from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.github_owner_kind import GithubOwnerKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="GithubOwner")


@_attrs_define
class GithubOwner:
    """
    Attributes:
        login (str):
        kind (GithubOwnerKind | Unset):  Default: GithubOwnerKind.USER.
        name (None | str | Unset):
    """

    login: str
    kind: GithubOwnerKind | Unset = GithubOwnerKind.USER
    name: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        login = self.login

        kind: str | Unset = UNSET
        if not isinstance(self.kind, Unset):
            kind = self.kind.value

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "login": login,
            }
        )
        if kind is not UNSET:
            field_dict["kind"] = kind
        if name is not UNSET:
            field_dict["name"] = name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        login = d.pop("login")

        _kind = d.pop("kind", UNSET)
        kind: GithubOwnerKind | Unset
        if isinstance(_kind, Unset):
            kind = UNSET
        else:
            kind = GithubOwnerKind(_kind)

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        github_owner = cls(
            login=login,
            kind=kind,
            name=name,
        )

        github_owner.additional_properties = d
        return github_owner

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
