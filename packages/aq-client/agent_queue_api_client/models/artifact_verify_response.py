from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ArtifactVerifyResponse")


@_attrs_define
class ArtifactVerifyResponse:
    """
    Attributes:
        uri (str):
        sha256 (str):
        bytes_ (int):
        verified (bool):
        path (str):
        success (bool | Unset):  Default: True.
        kind (None | str | Unset):
    """

    uri: str
    sha256: str
    bytes_: int
    verified: bool
    path: str
    success: bool | Unset = True
    kind: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        uri = self.uri

        sha256 = self.sha256

        bytes_ = self.bytes_

        verified = self.verified

        path = self.path

        success = self.success

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        else:
            kind = self.kind

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "uri": uri,
                "sha256": sha256,
                "bytes": bytes_,
                "verified": verified,
                "path": path,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if kind is not UNSET:
            field_dict["kind"] = kind

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        uri = d.pop("uri")

        sha256 = d.pop("sha256")

        bytes_ = d.pop("bytes")

        verified = d.pop("verified")

        path = d.pop("path")

        success = d.pop("success", UNSET)

        def _parse_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        artifact_verify_response = cls(
            uri=uri,
            sha256=sha256,
            bytes_=bytes_,
            verified=verified,
            path=path,
            success=success,
            kind=kind,
        )

        artifact_verify_response.additional_properties = d
        return artifact_verify_response

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
