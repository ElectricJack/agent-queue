from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_show_response_actions_item import AskShowResponseActionsItem
    from ..models.ask_show_response_decision_type_0 import AskShowResponseDecisionType0
    from ..models.ask_show_response_document_type_0 import AskShowResponseDocumentType0
    from ..models.ask_show_response_history_item import AskShowResponseHistoryItem
    from ..models.ask_show_response_inputs_item import AskShowResponseInputsItem
    from ..models.ask_show_response_obligations_item import AskShowResponseObligationsItem
    from ..models.ask_show_response_operations_item import AskShowResponseOperationsItem
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
        history (list[AskShowResponseHistoryItem] | Unset):
        obligations (list[AskShowResponseObligationsItem] | Unset):
        operations (list[AskShowResponseOperationsItem] | Unset):
        decision (AskShowResponseDecisionType0 | None | Unset):
        source_health (str | Unset):  Default: 'healthy'.
    """

    ask: AskSourceRecord
    expected: AskVersion
    inputs: list[AskShowResponseInputsItem]
    actions: list[AskShowResponseActionsItem]
    success: bool | Unset = True
    document: AskShowResponseDocumentType0 | None | Unset = UNSET
    history: list[AskShowResponseHistoryItem] | Unset = UNSET
    obligations: list[AskShowResponseObligationsItem] | Unset = UNSET
    operations: list[AskShowResponseOperationsItem] | Unset = UNSET
    decision: AskShowResponseDecisionType0 | None | Unset = UNSET
    source_health: str | Unset = "healthy"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.ask_show_response_decision_type_0 import AskShowResponseDecisionType0
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

        history: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.history, Unset):
            history = []
            for history_item_data in self.history:
                history_item = history_item_data.to_dict()
                history.append(history_item)

        obligations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.obligations, Unset):
            obligations = []
            for obligations_item_data in self.obligations:
                obligations_item = obligations_item_data.to_dict()
                obligations.append(obligations_item)

        operations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.operations, Unset):
            operations = []
            for operations_item_data in self.operations:
                operations_item = operations_item_data.to_dict()
                operations.append(operations_item)

        decision: dict[str, Any] | None | Unset
        if isinstance(self.decision, Unset):
            decision = UNSET
        elif isinstance(self.decision, AskShowResponseDecisionType0):
            decision = self.decision.to_dict()
        else:
            decision = self.decision

        source_health = self.source_health

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
        if history is not UNSET:
            field_dict["history"] = history
        if obligations is not UNSET:
            field_dict["obligations"] = obligations
        if operations is not UNSET:
            field_dict["operations"] = operations
        if decision is not UNSET:
            field_dict["decision"] = decision
        if source_health is not UNSET:
            field_dict["source_health"] = source_health

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_show_response_actions_item import AskShowResponseActionsItem
        from ..models.ask_show_response_decision_type_0 import AskShowResponseDecisionType0
        from ..models.ask_show_response_document_type_0 import AskShowResponseDocumentType0
        from ..models.ask_show_response_history_item import AskShowResponseHistoryItem
        from ..models.ask_show_response_inputs_item import AskShowResponseInputsItem
        from ..models.ask_show_response_obligations_item import AskShowResponseObligationsItem
        from ..models.ask_show_response_operations_item import AskShowResponseOperationsItem
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

        _history = d.pop("history", UNSET)
        history: list[AskShowResponseHistoryItem] | Unset = UNSET
        if _history is not UNSET:
            history = []
            for history_item_data in _history:
                history_item = AskShowResponseHistoryItem.from_dict(history_item_data)

                history.append(history_item)

        _obligations = d.pop("obligations", UNSET)
        obligations: list[AskShowResponseObligationsItem] | Unset = UNSET
        if _obligations is not UNSET:
            obligations = []
            for obligations_item_data in _obligations:
                obligations_item = AskShowResponseObligationsItem.from_dict(obligations_item_data)

                obligations.append(obligations_item)

        _operations = d.pop("operations", UNSET)
        operations: list[AskShowResponseOperationsItem] | Unset = UNSET
        if _operations is not UNSET:
            operations = []
            for operations_item_data in _operations:
                operations_item = AskShowResponseOperationsItem.from_dict(operations_item_data)

                operations.append(operations_item)

        def _parse_decision(data: object) -> AskShowResponseDecisionType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                decision_type_0 = AskShowResponseDecisionType0.from_dict(data)

                return decision_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AskShowResponseDecisionType0 | None | Unset, data)

        decision = _parse_decision(d.pop("decision", UNSET))

        source_health = d.pop("source_health", UNSET)

        ask_show_response = cls(
            ask=ask,
            expected=expected,
            inputs=inputs,
            actions=actions,
            success=success,
            document=document,
            history=history,
            obligations=obligations,
            operations=operations,
            decision=decision,
            source_health=source_health,
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
