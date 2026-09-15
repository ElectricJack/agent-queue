from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_command_catalog_entry_dto_parameters_schema import (
        PlaybookCommandCatalogEntryDTOParametersSchema,
    )


T = TypeVar("T", bound="PlaybookCommandCatalogEntryDTO")


@_attrs_define
class PlaybookCommandCatalogEntryDTO:
    """One contract's dashboard- and CLI-facing discovery metadata.

    Attributes:
        name (str):
        title (str):
        summary (str):
        docs_url (str):
        parameters_schema (PlaybookCommandCatalogEntryDTOParametersSchema | Unset):
    """

    name: str
    title: str
    summary: str
    docs_url: str
    parameters_schema: PlaybookCommandCatalogEntryDTOParametersSchema | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        title = self.title

        summary = self.summary

        docs_url = self.docs_url

        parameters_schema: dict[str, Any] | Unset = UNSET
        if not isinstance(self.parameters_schema, Unset):
            parameters_schema = self.parameters_schema.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "name": name,
                "title": title,
                "summary": summary,
                "docs_url": docs_url,
            }
        )
        if parameters_schema is not UNSET:
            field_dict["parameters_schema"] = parameters_schema

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_command_catalog_entry_dto_parameters_schema import (
            PlaybookCommandCatalogEntryDTOParametersSchema,
        )

        d = dict(src_dict)
        name = d.pop("name")

        title = d.pop("title")

        summary = d.pop("summary")

        docs_url = d.pop("docs_url")

        _parameters_schema = d.pop("parameters_schema", UNSET)
        parameters_schema: PlaybookCommandCatalogEntryDTOParametersSchema | Unset
        if isinstance(_parameters_schema, Unset):
            parameters_schema = UNSET
        else:
            parameters_schema = PlaybookCommandCatalogEntryDTOParametersSchema.from_dict(_parameters_schema)

        playbook_command_catalog_entry_dto = cls(
            name=name,
            title=title,
            summary=summary,
            docs_url=docs_url,
            parameters_schema=parameters_schema,
        )

        return playbook_command_catalog_entry_dto
