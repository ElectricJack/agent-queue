from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.integration_continue_close_response_outcome import IntegrationContinueCloseResponseOutcome
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.integration_continue_close_response_close_type_0 import IntegrationContinueCloseResponseCloseType0


T = TypeVar("T", bound="IntegrationContinueCloseResponse")


@_attrs_define
class IntegrationContinueCloseResponse:
    """
    Attributes:
        success (bool):
        outcome (IntegrationContinueCloseResponseOutcome):
        operation_id (None | str | Unset):
        close (IntegrationContinueCloseResponseCloseType0 | None | Unset):
        error (None | str | Unset):
    """

    success: bool
    outcome: IntegrationContinueCloseResponseOutcome
    operation_id: None | str | Unset = UNSET
    close: IntegrationContinueCloseResponseCloseType0 | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.integration_continue_close_response_close_type_0 import IntegrationContinueCloseResponseCloseType0

        success = self.success

        outcome = self.outcome.value

        operation_id: None | str | Unset
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        close: dict[str, Any] | None | Unset
        if isinstance(self.close, Unset):
            close = UNSET
        elif isinstance(self.close, IntegrationContinueCloseResponseCloseType0):
            close = self.close.to_dict()
        else:
            close = self.close

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "outcome": outcome,
            }
        )
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if close is not UNSET:
            field_dict["close"] = close
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.integration_continue_close_response_close_type_0 import IntegrationContinueCloseResponseCloseType0

        d = dict(src_dict)
        success = d.pop("success")

        outcome = IntegrationContinueCloseResponseOutcome(d.pop("outcome"))

        def _parse_operation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))

        def _parse_close(data: object) -> IntegrationContinueCloseResponseCloseType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                close_type_0 = IntegrationContinueCloseResponseCloseType0.from_dict(data)

                return close_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IntegrationContinueCloseResponseCloseType0 | None | Unset, data)

        close = _parse_close(d.pop("close", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        integration_continue_close_response = cls(
            success=success,
            outcome=outcome,
            operation_id=operation_id,
            close=close,
            error=error,
        )

        integration_continue_close_response.additional_properties = d
        return integration_continue_close_response

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
