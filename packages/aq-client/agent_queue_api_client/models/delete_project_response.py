from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.delete_project_response_channel_ids_type_0 import DeleteProjectResponseChannelIdsType0


T = TypeVar("T", bound="DeleteProjectResponse")


@_attrs_define
class DeleteProjectResponse:
    """
    Attributes:
        deleted (str):
        name (str):
        channel_ids (DeleteProjectResponseChannelIdsType0 | None | Unset):
        archive_channels (bool | None | Unset):
    """

    deleted: str
    name: str
    channel_ids: DeleteProjectResponseChannelIdsType0 | None | Unset = UNSET
    archive_channels: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.delete_project_response_channel_ids_type_0 import DeleteProjectResponseChannelIdsType0

        deleted = self.deleted

        name = self.name

        channel_ids: dict[str, Any] | None | Unset
        if isinstance(self.channel_ids, Unset):
            channel_ids = UNSET
        elif isinstance(self.channel_ids, DeleteProjectResponseChannelIdsType0):
            channel_ids = self.channel_ids.to_dict()
        else:
            channel_ids = self.channel_ids

        archive_channels: bool | None | Unset
        if isinstance(self.archive_channels, Unset):
            archive_channels = UNSET
        else:
            archive_channels = self.archive_channels

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
                "name": name,
            }
        )
        if channel_ids is not UNSET:
            field_dict["channel_ids"] = channel_ids
        if archive_channels is not UNSET:
            field_dict["archive_channels"] = archive_channels

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.delete_project_response_channel_ids_type_0 import DeleteProjectResponseChannelIdsType0

        d = dict(src_dict)
        deleted = d.pop("deleted")

        name = d.pop("name")

        def _parse_channel_ids(data: object) -> DeleteProjectResponseChannelIdsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                channel_ids_type_0 = DeleteProjectResponseChannelIdsType0.from_dict(data)

                return channel_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DeleteProjectResponseChannelIdsType0 | None | Unset, data)

        channel_ids = _parse_channel_ids(d.pop("channel_ids", UNSET))

        def _parse_archive_channels(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        archive_channels = _parse_archive_channels(d.pop("archive_channels", UNSET))

        delete_project_response = cls(
            deleted=deleted,
            name=name,
            channel_ids=channel_ids,
            archive_channels=archive_channels,
        )

        delete_project_response.additional_properties = d
        return delete_project_response

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
