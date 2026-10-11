from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_show_response_actions_item import AskShowResponseActionsItem
    from ..models.ask_show_response_document_type_0 import AskShowResponseDocumentType0
    from ..models.ask_show_response_inputs_item import AskShowResponseInputsItem
    from ..models.ask_source_record import AskSourceRecord
    from ..models.ask_version import AskVersion


T = TypeVar("T", bound="AskShowResponse")


@_attrs_define
class AskShowResponse:
    """
    Attributes:
        ask (AskSourceRecord):
        expected (AskVersion):
        inputs (list[AskShowResponseInputsItem]):
        actions (list[AskShowResponseActionsItem]):
        success (bool | Unset):  Default: True.
        document (AskShowResponseDocumentType0 | None | Unset):
    """

    ask: AskSourceRecord
    expected: AskVersion
    inputs: list[AskShowResponseInputsItem]
    actions: list[AskShowResponseActionsItem]
    success: bool | Unset = True
    document: AskShowResponseDocumentType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.ask_show_response_document_type_0 import AskShowResponseDocumentType0

        ask = self.ask.to_dict()

        expected = self.expected.to_dict()

        inputs = []
        for inputs_item_data in self.inputs:
            inputs_item = inputs_item_data.to_dict()
            inputs.append(inputs_item)

        actions = []
        for actions_item_data in self.actions:
            actions_item = actions_item_data.to_dict()
            actions.append(actions_item)

        success = self.success

        document: dict[str, Any] | None | Unset
        if isinstance(self.document, Unset):
            document = UNSET
        elif isinstance(self.document, AskShowResponseDocumentType0):
            document = self.document.to_dict()
        else:
            document = self.document

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ask": ask,
                "expected": expected,
                "inputs": inputs,
                "actions": actions,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if document is not UNSET:
            field_dict["document"] = document

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_show_response_actions_item import AskShowResponseActionsItem
        from ..models.ask_show_response_document_type_0 import AskShowResponseDocumentType0
        from ..models.ask_show_response_inputs_item import AskShowResponseInputsItem
        from ..models.ask_source_record import AskSourceRecord
        from ..models.ask_version import AskVersion

        d = dict(src_dict)
        ask = AskSourceRecord.from_dict(d.pop("ask"))

        expected = AskVersion.from_dict(d.pop("expected"))

        inputs = []
        _inputs = d.pop("inputs")
        for inputs_item_data in _inputs:
            inputs_item = AskShowResponseInputsItem.from_dict(inputs_item_data)

            inputs.append(inputs_item)

        actions = []
        _actions = d.pop("actions")
        for actions_item_data in _actions:
            actions_item = AskShowResponseActionsItem.from_dict(actions_item_data)

            actions.append(actions_item)

        success = d.pop("success", UNSET)

        def _parse_document(data: object) -> AskShowResponseDocumentType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                document_type_0 = AskShowResponseDocumentType0.from_dict(data)

                return document_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AskShowResponseDocumentType0 | None | Unset, data)

        document = _parse_document(d.pop("document", UNSET))

        ask_show_response = cls(
            ask=ask,
            expected=expected,
            inputs=inputs,
            actions=actions,
            success=success,
            document=document,
        )

        ask_show_response.additional_properties = d
        return ask_show_response

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
