from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.receipt_dto_step_kind import ReceiptDTOStepKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cancellation_facts_dto import CancellationFactsDTO
    from ..models.explanation_row_dto import ExplanationRowDTO
    from ..models.explanation_value_dto import ExplanationValueDTO
    from ..models.token_usage_dto import TokenUsageDTO
    from ..models.wait_facts_dto import WaitFactsDTO


T = TypeVar("T", bound="ReceiptDTO")


@_attrs_define
class ReceiptDTO:
    """
    Attributes:
        receipt_id (str):
        step_id (str):
        rule_id (str):
        step_kind (ReceiptDTOStepKind):
        outcome (str):
        started_at (float):
        attempt (int | Unset):  Default: 1.
        iteration_index (int | None | Unset):
        selected_edge_id (None | str | Unset):
        completed_at (float | None | Unset):
        duration_seconds (float | None | Unset):
        inputs (list[ExplanationRowDTO] | Unset):
        result (ExplanationValueDTO | None | Unset):
        token_usage (None | TokenUsageDTO | Unset):
        idempotency_key (None | str | Unset):
        principal_fingerprint (None | str | Unset):
        profile_id (None | str | Unset):
        contract_fingerprint (None | str | Unset):
        error (None | str | Unset):
        wait (None | Unset | WaitFactsDTO):
        cancellation (CancellationFactsDTO | None | Unset):
    """

    receipt_id: str
    step_id: str
    rule_id: str
    step_kind: ReceiptDTOStepKind
    outcome: str
    started_at: float
    attempt: int | Unset = 1
    iteration_index: int | None | Unset = UNSET
    selected_edge_id: None | str | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    duration_seconds: float | None | Unset = UNSET
    inputs: list[ExplanationRowDTO] | Unset = UNSET
    result: ExplanationValueDTO | None | Unset = UNSET
    token_usage: None | TokenUsageDTO | Unset = UNSET
    idempotency_key: None | str | Unset = UNSET
    principal_fingerprint: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    contract_fingerprint: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    wait: None | Unset | WaitFactsDTO = UNSET
    cancellation: CancellationFactsDTO | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.cancellation_facts_dto import CancellationFactsDTO
        from ..models.explanation_value_dto import ExplanationValueDTO
        from ..models.token_usage_dto import TokenUsageDTO
        from ..models.wait_facts_dto import WaitFactsDTO

        receipt_id = self.receipt_id

        step_id = self.step_id

        rule_id = self.rule_id

        step_kind = self.step_kind.value

        outcome = self.outcome

        started_at = self.started_at

        attempt = self.attempt

        iteration_index: int | None | Unset
        if isinstance(self.iteration_index, Unset):
            iteration_index = UNSET
        else:
            iteration_index = self.iteration_index

        selected_edge_id: None | str | Unset
        if isinstance(self.selected_edge_id, Unset):
            selected_edge_id = UNSET
        else:
            selected_edge_id = self.selected_edge_id

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        duration_seconds: float | None | Unset
        if isinstance(self.duration_seconds, Unset):
            duration_seconds = UNSET
        else:
            duration_seconds = self.duration_seconds

        inputs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.inputs, Unset):
            inputs = []
            for inputs_item_data in self.inputs:
                inputs_item = inputs_item_data.to_dict()
                inputs.append(inputs_item)

        result: dict[str, Any] | None | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, ExplanationValueDTO):
            result = self.result.to_dict()
        else:
            result = self.result

        token_usage: dict[str, Any] | None | Unset
        if isinstance(self.token_usage, Unset):
            token_usage = UNSET
        elif isinstance(self.token_usage, TokenUsageDTO):
            token_usage = self.token_usage.to_dict()
        else:
            token_usage = self.token_usage

        idempotency_key: None | str | Unset
        if isinstance(self.idempotency_key, Unset):
            idempotency_key = UNSET
        else:
            idempotency_key = self.idempotency_key

        principal_fingerprint: None | str | Unset
        if isinstance(self.principal_fingerprint, Unset):
            principal_fingerprint = UNSET
        else:
            principal_fingerprint = self.principal_fingerprint

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        contract_fingerprint: None | str | Unset
        if isinstance(self.contract_fingerprint, Unset):
            contract_fingerprint = UNSET
        else:
            contract_fingerprint = self.contract_fingerprint

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        wait: dict[str, Any] | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        elif isinstance(self.wait, WaitFactsDTO):
            wait = self.wait.to_dict()
        else:
            wait = self.wait

        cancellation: dict[str, Any] | None | Unset
        if isinstance(self.cancellation, Unset):
            cancellation = UNSET
        elif isinstance(self.cancellation, CancellationFactsDTO):
            cancellation = self.cancellation.to_dict()
        else:
            cancellation = self.cancellation

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "receipt_id": receipt_id,
                "step_id": step_id,
                "rule_id": rule_id,
                "step_kind": step_kind,
                "outcome": outcome,
                "started_at": started_at,
            }
        )
        if attempt is not UNSET:
            field_dict["attempt"] = attempt
        if iteration_index is not UNSET:
            field_dict["iteration_index"] = iteration_index
        if selected_edge_id is not UNSET:
            field_dict["selected_edge_id"] = selected_edge_id
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if duration_seconds is not UNSET:
            field_dict["duration_seconds"] = duration_seconds
        if inputs is not UNSET:
            field_dict["inputs"] = inputs
        if result is not UNSET:
            field_dict["result"] = result
        if token_usage is not UNSET:
            field_dict["token_usage"] = token_usage
        if idempotency_key is not UNSET:
            field_dict["idempotency_key"] = idempotency_key
        if principal_fingerprint is not UNSET:
            field_dict["principal_fingerprint"] = principal_fingerprint
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if contract_fingerprint is not UNSET:
            field_dict["contract_fingerprint"] = contract_fingerprint
        if error is not UNSET:
            field_dict["error"] = error
        if wait is not UNSET:
            field_dict["wait"] = wait
        if cancellation is not UNSET:
            field_dict["cancellation"] = cancellation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cancellation_facts_dto import CancellationFactsDTO
        from ..models.explanation_row_dto import ExplanationRowDTO
        from ..models.explanation_value_dto import ExplanationValueDTO
        from ..models.token_usage_dto import TokenUsageDTO
        from ..models.wait_facts_dto import WaitFactsDTO

        d = dict(src_dict)
        receipt_id = d.pop("receipt_id")

        step_id = d.pop("step_id")

        rule_id = d.pop("rule_id")

        step_kind = ReceiptDTOStepKind(d.pop("step_kind"))

        outcome = d.pop("outcome")

        started_at = d.pop("started_at")

        attempt = d.pop("attempt", UNSET)

        def _parse_iteration_index(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        iteration_index = _parse_iteration_index(d.pop("iteration_index", UNSET))

        def _parse_selected_edge_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        selected_edge_id = _parse_selected_edge_id(d.pop("selected_edge_id", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        def _parse_duration_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        duration_seconds = _parse_duration_seconds(d.pop("duration_seconds", UNSET))

        _inputs = d.pop("inputs", UNSET)
        inputs: list[ExplanationRowDTO] | Unset = UNSET
        if _inputs is not UNSET:
            inputs = []
            for inputs_item_data in _inputs:
                inputs_item = ExplanationRowDTO.from_dict(inputs_item_data)

                inputs.append(inputs_item)

        def _parse_result(data: object) -> ExplanationValueDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = ExplanationValueDTO.from_dict(data)

                return result_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ExplanationValueDTO | None | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        def _parse_token_usage(data: object) -> None | TokenUsageDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                token_usage_type_0 = TokenUsageDTO.from_dict(data)

                return token_usage_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TokenUsageDTO | Unset, data)

        token_usage = _parse_token_usage(d.pop("token_usage", UNSET))

        def _parse_idempotency_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_key = _parse_idempotency_key(d.pop("idempotency_key", UNSET))

        def _parse_principal_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        principal_fingerprint = _parse_principal_fingerprint(d.pop("principal_fingerprint", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_contract_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        contract_fingerprint = _parse_contract_fingerprint(d.pop("contract_fingerprint", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_wait(data: object) -> None | Unset | WaitFactsDTO:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_0 = WaitFactsDTO.from_dict(data)

                return wait_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitFactsDTO, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        def _parse_cancellation(data: object) -> CancellationFactsDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cancellation_type_0 = CancellationFactsDTO.from_dict(data)

                return cancellation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CancellationFactsDTO | None | Unset, data)

        cancellation = _parse_cancellation(d.pop("cancellation", UNSET))

        receipt_dto = cls(
            receipt_id=receipt_id,
            step_id=step_id,
            rule_id=rule_id,
            step_kind=step_kind,
            outcome=outcome,
            started_at=started_at,
            attempt=attempt,
            iteration_index=iteration_index,
            selected_edge_id=selected_edge_id,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            inputs=inputs,
            result=result,
            token_usage=token_usage,
            idempotency_key=idempotency_key,
            principal_fingerprint=principal_fingerprint,
            profile_id=profile_id,
            contract_fingerprint=contract_fingerprint,
            error=error,
            wait=wait,
            cancellation=cancellation,
        )

        return receipt_dto
