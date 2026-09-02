from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_row_dto import ExplanationRowDTO
    from ..models.idempotency_dto import IdempotencyDTO
    from ..models.node_advanced_dto_result_schema_type_0 import NodeAdvancedDTOResultSchemaType0
    from ..models.node_advanced_dto_typed_step import NodeAdvancedDTOTypedStep
    from ..models.redaction_row_dto import RedactionRowDTO
    from ..models.retry_policy_dto import RetryPolicyDTO


T = TypeVar("T", bound="NodeAdvancedDTO")


@_attrs_define
class NodeAdvancedDTO:
    """Advanced view.  Canonical data, never the default explanation.

    Attributes:
        typed_step (NodeAdvancedDTOTypedStep):
        resolved_inputs (list[ExplanationRowDTO] | Unset):
        result_schema (NodeAdvancedDTOResultSchemaType0 | None | Unset):
        retry (None | RetryPolicyDTO | Unset):
        idempotency (IdempotencyDTO | None | Unset):
        redaction (list[RedactionRowDTO] | Unset):
        execution_fingerprint (None | str | Unset):
    """

    typed_step: NodeAdvancedDTOTypedStep
    resolved_inputs: list[ExplanationRowDTO] | Unset = UNSET
    result_schema: NodeAdvancedDTOResultSchemaType0 | None | Unset = UNSET
    retry: None | RetryPolicyDTO | Unset = UNSET
    idempotency: IdempotencyDTO | None | Unset = UNSET
    redaction: list[RedactionRowDTO] | Unset = UNSET
    execution_fingerprint: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.idempotency_dto import IdempotencyDTO
        from ..models.node_advanced_dto_result_schema_type_0 import NodeAdvancedDTOResultSchemaType0
        from ..models.retry_policy_dto import RetryPolicyDTO

        typed_step = self.typed_step.to_dict()

        resolved_inputs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.resolved_inputs, Unset):
            resolved_inputs = []
            for resolved_inputs_item_data in self.resolved_inputs:
                resolved_inputs_item = resolved_inputs_item_data.to_dict()
                resolved_inputs.append(resolved_inputs_item)

        result_schema: dict[str, Any] | None | Unset
        if isinstance(self.result_schema, Unset):
            result_schema = UNSET
        elif isinstance(self.result_schema, NodeAdvancedDTOResultSchemaType0):
            result_schema = self.result_schema.to_dict()
        else:
            result_schema = self.result_schema

        retry: dict[str, Any] | None | Unset
        if isinstance(self.retry, Unset):
            retry = UNSET
        elif isinstance(self.retry, RetryPolicyDTO):
            retry = self.retry.to_dict()
        else:
            retry = self.retry

        idempotency: dict[str, Any] | None | Unset
        if isinstance(self.idempotency, Unset):
            idempotency = UNSET
        elif isinstance(self.idempotency, IdempotencyDTO):
            idempotency = self.idempotency.to_dict()
        else:
            idempotency = self.idempotency

        redaction: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.redaction, Unset):
            redaction = []
            for redaction_item_data in self.redaction:
                redaction_item = redaction_item_data.to_dict()
                redaction.append(redaction_item)

        execution_fingerprint: None | str | Unset
        if isinstance(self.execution_fingerprint, Unset):
            execution_fingerprint = UNSET
        else:
            execution_fingerprint = self.execution_fingerprint

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "typed_step": typed_step,
            }
        )
        if resolved_inputs is not UNSET:
            field_dict["resolved_inputs"] = resolved_inputs
        if result_schema is not UNSET:
            field_dict["result_schema"] = result_schema
        if retry is not UNSET:
            field_dict["retry"] = retry
        if idempotency is not UNSET:
            field_dict["idempotency"] = idempotency
        if redaction is not UNSET:
            field_dict["redaction"] = redaction
        if execution_fingerprint is not UNSET:
            field_dict["execution_fingerprint"] = execution_fingerprint

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_row_dto import ExplanationRowDTO
        from ..models.idempotency_dto import IdempotencyDTO
        from ..models.node_advanced_dto_result_schema_type_0 import NodeAdvancedDTOResultSchemaType0
        from ..models.node_advanced_dto_typed_step import NodeAdvancedDTOTypedStep
        from ..models.redaction_row_dto import RedactionRowDTO
        from ..models.retry_policy_dto import RetryPolicyDTO

        d = dict(src_dict)
        typed_step = NodeAdvancedDTOTypedStep.from_dict(d.pop("typed_step"))

        _resolved_inputs = d.pop("resolved_inputs", UNSET)
        resolved_inputs: list[ExplanationRowDTO] | Unset = UNSET
        if _resolved_inputs is not UNSET:
            resolved_inputs = []
            for resolved_inputs_item_data in _resolved_inputs:
                resolved_inputs_item = ExplanationRowDTO.from_dict(resolved_inputs_item_data)

                resolved_inputs.append(resolved_inputs_item)

        def _parse_result_schema(data: object) -> NodeAdvancedDTOResultSchemaType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_schema_type_0 = NodeAdvancedDTOResultSchemaType0.from_dict(data)

                return result_schema_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(NodeAdvancedDTOResultSchemaType0 | None | Unset, data)

        result_schema = _parse_result_schema(d.pop("result_schema", UNSET))

        def _parse_retry(data: object) -> None | RetryPolicyDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                retry_type_0 = RetryPolicyDTO.from_dict(data)

                return retry_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RetryPolicyDTO | Unset, data)

        retry = _parse_retry(d.pop("retry", UNSET))

        def _parse_idempotency(data: object) -> IdempotencyDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                idempotency_type_0 = IdempotencyDTO.from_dict(data)

                return idempotency_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IdempotencyDTO | None | Unset, data)

        idempotency = _parse_idempotency(d.pop("idempotency", UNSET))

        _redaction = d.pop("redaction", UNSET)
        redaction: list[RedactionRowDTO] | Unset = UNSET
        if _redaction is not UNSET:
            redaction = []
            for redaction_item_data in _redaction:
                redaction_item = RedactionRowDTO.from_dict(redaction_item_data)

                redaction.append(redaction_item)

        def _parse_execution_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        execution_fingerprint = _parse_execution_fingerprint(d.pop("execution_fingerprint", UNSET))

        node_advanced_dto = cls(
            typed_step=typed_step,
            resolved_inputs=resolved_inputs,
            result_schema=result_schema,
            retry=retry,
            idempotency=idempotency,
            redaction=redaction,
            execution_fingerprint=execution_fingerprint,
        )

        return node_advanced_dto
