from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_select_response_pending_obligations_item import TestSelectResponsePendingObligationsItem
    from ..models.test_select_response_reasons import TestSelectResponseReasons
    from ..models.test_select_response_record import TestSelectResponseRecord


T = TypeVar("T", bound="TestSelectResponse")


@_attrs_define
class TestSelectResponse:
    """
    Attributes:
        selection_id (str):
        mode (str):
        recorded (bool):
        full_required (bool):
        full_suite_authorized (bool):
        final_modules (list[str]):
        ordered (list[str]):
        fallback_modules (list[str]):
        mandatory_modules (list[str]):
        static_modules (list[str]):
        jev_modules (list[str] | None):
        jev_status (str):
        fallback_reason (None | str):
        jev_used_for_omission (bool):
        reasons (TestSelectResponseReasons):
        argv (list[list[str]]):
        pending_obligations (list[TestSelectResponsePendingObligationsItem]):
        record (TestSelectResponseRecord):
        success (bool | Unset):  Default: True.
    """

    selection_id: str
    mode: str
    recorded: bool
    full_required: bool
    full_suite_authorized: bool
    final_modules: list[str]
    ordered: list[str]
    fallback_modules: list[str]
    mandatory_modules: list[str]
    static_modules: list[str]
    jev_modules: list[str] | None
    jev_status: str
    fallback_reason: None | str
    jev_used_for_omission: bool
    reasons: TestSelectResponseReasons
    argv: list[list[str]]
    pending_obligations: list[TestSelectResponsePendingObligationsItem]
    record: TestSelectResponseRecord
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        selection_id = self.selection_id

        mode = self.mode

        recorded = self.recorded

        full_required = self.full_required

        full_suite_authorized = self.full_suite_authorized

        final_modules = self.final_modules

        ordered = self.ordered

        fallback_modules = self.fallback_modules

        mandatory_modules = self.mandatory_modules

        static_modules = self.static_modules

        jev_modules: list[str] | None
        if isinstance(self.jev_modules, list):
            jev_modules = self.jev_modules

        else:
            jev_modules = self.jev_modules

        jev_status = self.jev_status

        fallback_reason: None | str
        fallback_reason = self.fallback_reason

        jev_used_for_omission = self.jev_used_for_omission

        reasons = self.reasons.to_dict()

        argv = []
        for argv_item_data in self.argv:
            argv_item = argv_item_data

            argv.append(argv_item)

        pending_obligations = []
        for pending_obligations_item_data in self.pending_obligations:
            pending_obligations_item = pending_obligations_item_data.to_dict()
            pending_obligations.append(pending_obligations_item)

        record = self.record.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "selection_id": selection_id,
                "mode": mode,
                "recorded": recorded,
                "full_required": full_required,
                "full_suite_authorized": full_suite_authorized,
                "final_modules": final_modules,
                "ordered": ordered,
                "fallback_modules": fallback_modules,
                "mandatory_modules": mandatory_modules,
                "static_modules": static_modules,
                "jev_modules": jev_modules,
                "jev_status": jev_status,
                "fallback_reason": fallback_reason,
                "jev_used_for_omission": jev_used_for_omission,
                "reasons": reasons,
                "argv": argv,
                "pending_obligations": pending_obligations,
                "record": record,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_select_response_pending_obligations_item import TestSelectResponsePendingObligationsItem
        from ..models.test_select_response_reasons import TestSelectResponseReasons
        from ..models.test_select_response_record import TestSelectResponseRecord

        d = dict(src_dict)
        selection_id = d.pop("selection_id")

        mode = d.pop("mode")

        recorded = d.pop("recorded")

        full_required = d.pop("full_required")

        full_suite_authorized = d.pop("full_suite_authorized")

        final_modules = cast(list[str], d.pop("final_modules"))

        ordered = cast(list[str], d.pop("ordered"))

        fallback_modules = cast(list[str], d.pop("fallback_modules"))

        mandatory_modules = cast(list[str], d.pop("mandatory_modules"))

        static_modules = cast(list[str], d.pop("static_modules"))

        def _parse_jev_modules(data: object) -> list[str] | None:
            if data is None:
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                jev_modules_type_0 = cast(list[str], data)

                return jev_modules_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None, data)

        jev_modules = _parse_jev_modules(d.pop("jev_modules"))

        jev_status = d.pop("jev_status")

        def _parse_fallback_reason(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        fallback_reason = _parse_fallback_reason(d.pop("fallback_reason"))

        jev_used_for_omission = d.pop("jev_used_for_omission")

        reasons = TestSelectResponseReasons.from_dict(d.pop("reasons"))

        argv = []
        _argv = d.pop("argv")
        for argv_item_data in _argv:
            argv_item = cast(list[str], argv_item_data)

            argv.append(argv_item)

        pending_obligations = []
        _pending_obligations = d.pop("pending_obligations")
        for pending_obligations_item_data in _pending_obligations:
            pending_obligations_item = TestSelectResponsePendingObligationsItem.from_dict(pending_obligations_item_data)

            pending_obligations.append(pending_obligations_item)

        record = TestSelectResponseRecord.from_dict(d.pop("record"))

        success = d.pop("success", UNSET)

        test_select_response = cls(
            selection_id=selection_id,
            mode=mode,
            recorded=recorded,
            full_required=full_required,
            full_suite_authorized=full_suite_authorized,
            final_modules=final_modules,
            ordered=ordered,
            fallback_modules=fallback_modules,
            mandatory_modules=mandatory_modules,
            static_modules=static_modules,
            jev_modules=jev_modules,
            jev_status=jev_status,
            fallback_reason=fallback_reason,
            jev_used_for_omission=jev_used_for_omission,
            reasons=reasons,
            argv=argv,
            pending_obligations=pending_obligations,
            record=record,
            success=success,
        )

        test_select_response.additional_properties = d
        return test_select_response

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
