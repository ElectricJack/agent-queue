from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.transcript_entry_model import TranscriptEntryModel


T = TypeVar("T", bound="SessionLogsResponse")


@_attrs_define
class SessionLogsResponse:
    """Union: transcript entries OR peek-fallback string output.

    ``source`` discriminates; extra keys allowed so a peek-fallback row
    that echoes ``note`` from ``_cmd_session_peek`` still validates.

        Attributes:
            session_id (str):
            success (bool | Unset):  Default: True.
            source (str | Unset):  Default: 'transcript'.
            entries (list[TranscriptEntryModel] | None | Unset):
            output (None | str | Unset):
    """

    session_id: str
    success: bool | Unset = True
    source: str | Unset = "transcript"
    entries: list[TranscriptEntryModel] | None | Unset = UNSET
    output: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        success = self.success

        source = self.source

        entries: list[dict[str, Any]] | None | Unset
        if isinstance(self.entries, Unset):
            entries = UNSET
        elif isinstance(self.entries, list):
            entries = []
            for entries_type_0_item_data in self.entries:
                entries_type_0_item = entries_type_0_item_data.to_dict()
                entries.append(entries_type_0_item)

        else:
            entries = self.entries

        output: None | str | Unset
        if isinstance(self.output, Unset):
            output = UNSET
        else:
            output = self.output

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if source is not UNSET:
            field_dict["source"] = source
        if entries is not UNSET:
            field_dict["entries"] = entries
        if output is not UNSET:
            field_dict["output"] = output

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.transcript_entry_model import TranscriptEntryModel  # noqa: PLC0415

        d = dict(src_dict)
        session_id = d.pop("session_id")

        success = d.pop("success", UNSET)

        source = d.pop("source", UNSET)

        def _parse_entries(data: object) -> list[TranscriptEntryModel] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                entries_type_0 = []
                _entries_type_0 = data
                for entries_type_0_item_data in _entries_type_0:
                    entries_type_0_item = TranscriptEntryModel.from_dict(entries_type_0_item_data)

                    entries_type_0.append(entries_type_0_item)

                return entries_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[TranscriptEntryModel] | None | Unset, data)

        entries = _parse_entries(d.pop("entries", UNSET))

        def _parse_output(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        output = _parse_output(d.pop("output", UNSET))

        session_logs_response = cls(
            session_id=session_id,
            success=success,
            source=source,
            entries=entries,
            output=output,
        )

        session_logs_response.additional_properties = d
        return session_logs_response

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
