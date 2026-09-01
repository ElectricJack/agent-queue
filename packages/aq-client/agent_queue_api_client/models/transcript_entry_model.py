from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.transcript_entry_model_usage_type_0 import TranscriptEntryModelUsageType0


T = TypeVar("T", bound="TranscriptEntryModel")


@_attrs_define
class TranscriptEntryModel:
    """
    Attributes:
        uuid (str):
        type_ (str):
        parent_uuid (None | str | Unset):
        text (str | Unset):  Default: ''.
        model (None | str | Unset):
        usage (None | TranscriptEntryModelUsageType0 | Unset):
        ts (float | Unset):  Default: 0.0.
    """

    uuid: str
    type_: str
    parent_uuid: None | str | Unset = UNSET
    text: str | Unset = ""
    model: None | str | Unset = UNSET
    usage: None | TranscriptEntryModelUsageType0 | Unset = UNSET
    ts: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.transcript_entry_model_usage_type_0 import TranscriptEntryModelUsageType0  # noqa: PLC0415

        uuid = self.uuid

        type_ = self.type_

        parent_uuid: None | str | Unset
        if isinstance(self.parent_uuid, Unset):
            parent_uuid = UNSET
        else:
            parent_uuid = self.parent_uuid

        text = self.text

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        usage: dict[str, Any] | None | Unset
        if isinstance(self.usage, Unset):
            usage = UNSET
        elif isinstance(self.usage, TranscriptEntryModelUsageType0):
            usage = self.usage.to_dict()
        else:
            usage = self.usage

        ts = self.ts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "uuid": uuid,
                "type": type_,
            }
        )
        if parent_uuid is not UNSET:
            field_dict["parent_uuid"] = parent_uuid
        if text is not UNSET:
            field_dict["text"] = text
        if model is not UNSET:
            field_dict["model"] = model
        if usage is not UNSET:
            field_dict["usage"] = usage
        if ts is not UNSET:
            field_dict["ts"] = ts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.transcript_entry_model_usage_type_0 import TranscriptEntryModelUsageType0  # noqa: PLC0415

        d = dict(src_dict)
        uuid = d.pop("uuid")

        type_ = d.pop("type")

        def _parse_parent_uuid(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_uuid = _parse_parent_uuid(d.pop("parent_uuid", UNSET))

        text = d.pop("text", UNSET)

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_usage(data: object) -> None | TranscriptEntryModelUsageType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                usage_type_0 = TranscriptEntryModelUsageType0.from_dict(data)

                return usage_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TranscriptEntryModelUsageType0 | Unset, data)

        usage = _parse_usage(d.pop("usage", UNSET))

        ts = d.pop("ts", UNSET)

        transcript_entry_model = cls(
            uuid=uuid,
            type_=type_,
            parent_uuid=parent_uuid,
            text=text,
            model=model,
            usage=usage,
            ts=ts,
        )

        transcript_entry_model.additional_properties = d
        return transcript_entry_model

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
