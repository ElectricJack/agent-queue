from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_cutover_window_status_response_admission_type_0 import (
    PlaybookCutoverWindowStatusResponseAdmissionType0,
)
from ..models.playbook_cutover_window_status_response_runtime_type_0 import (
    PlaybookCutoverWindowStatusResponseRuntimeType0,
)
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_window_dto import CutoverWindowDTO
    from ..models.playbook_cutover_window_status_response_measures_item import (
        PlaybookCutoverWindowStatusResponseMeasuresItem,
    )


T = TypeVar("T", bound="PlaybookCutoverWindowStatusResponse")


@_attrs_define
class PlaybookCutoverWindowStatusResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        generated_at (float | None | Unset):
        runtime (None | PlaybookCutoverWindowStatusResponseRuntimeType0 | Unset):
        admission (None | PlaybookCutoverWindowStatusResponseAdmissionType0 | Unset):
        measures (list[PlaybookCutoverWindowStatusResponseMeasuresItem] | Unset):
        window (CutoverWindowDTO | None | Unset):
        blocking_reasons (list[str] | Unset):
        evidence_errors (list[str] | Unset):
        can_close (bool | Unset):  Default: False.
        error (None | str | Unset):
    """

    success: bool | Unset = True
    generated_at: float | None | Unset = UNSET
    runtime: None | PlaybookCutoverWindowStatusResponseRuntimeType0 | Unset = UNSET
    admission: None | PlaybookCutoverWindowStatusResponseAdmissionType0 | Unset = UNSET
    measures: list[PlaybookCutoverWindowStatusResponseMeasuresItem] | Unset = UNSET
    window: CutoverWindowDTO | None | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
    evidence_errors: list[str] | Unset = UNSET
    can_close: bool | Unset = False
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.cutover_window_dto import CutoverWindowDTO

        success = self.success

        generated_at: float | None | Unset
        if isinstance(self.generated_at, Unset):
            generated_at = UNSET
        else:
            generated_at = self.generated_at

        runtime: None | str | Unset
        if isinstance(self.runtime, Unset):
            runtime = UNSET
        elif isinstance(self.runtime, PlaybookCutoverWindowStatusResponseRuntimeType0):
            runtime = self.runtime.value
        else:
            runtime = self.runtime

        admission: None | str | Unset
        if isinstance(self.admission, Unset):
            admission = UNSET
        elif isinstance(self.admission, PlaybookCutoverWindowStatusResponseAdmissionType0):
            admission = self.admission.value
        else:
            admission = self.admission

        measures: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.measures, Unset):
            measures = []
            for measures_item_data in self.measures:
                measures_item = measures_item_data.to_dict()
                measures.append(measures_item)

        window: dict[str, Any] | None | Unset
        if isinstance(self.window, Unset):
            window = UNSET
        elif isinstance(self.window, CutoverWindowDTO):
            window = self.window.to_dict()
        else:
            window = self.window

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        evidence_errors: list[str] | Unset = UNSET
        if not isinstance(self.evidence_errors, Unset):
            evidence_errors = self.evidence_errors

        can_close = self.can_close

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if generated_at is not UNSET:
            field_dict["generated_at"] = generated_at
        if runtime is not UNSET:
            field_dict["runtime"] = runtime
        if admission is not UNSET:
            field_dict["admission"] = admission
        if measures is not UNSET:
            field_dict["measures"] = measures
        if window is not UNSET:
            field_dict["window"] = window
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if evidence_errors is not UNSET:
            field_dict["evidence_errors"] = evidence_errors
        if can_close is not UNSET:
            field_dict["can_close"] = can_close
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_window_dto import CutoverWindowDTO
        from ..models.playbook_cutover_window_status_response_measures_item import (
            PlaybookCutoverWindowStatusResponseMeasuresItem,
        )

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_generated_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        generated_at = _parse_generated_at(d.pop("generated_at", UNSET))

        def _parse_runtime(data: object) -> None | PlaybookCutoverWindowStatusResponseRuntimeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                runtime_type_0 = PlaybookCutoverWindowStatusResponseRuntimeType0(data)

                return runtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookCutoverWindowStatusResponseRuntimeType0 | Unset, data)

        runtime = _parse_runtime(d.pop("runtime", UNSET))

        def _parse_admission(data: object) -> None | PlaybookCutoverWindowStatusResponseAdmissionType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                admission_type_0 = PlaybookCutoverWindowStatusResponseAdmissionType0(data)

                return admission_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookCutoverWindowStatusResponseAdmissionType0 | Unset, data)

        admission = _parse_admission(d.pop("admission", UNSET))

        _measures = d.pop("measures", UNSET)
        measures: list[PlaybookCutoverWindowStatusResponseMeasuresItem] | Unset = UNSET
        if _measures is not UNSET:
            measures = []
            for measures_item_data in _measures:
                measures_item = PlaybookCutoverWindowStatusResponseMeasuresItem.from_dict(measures_item_data)

                measures.append(measures_item)

        def _parse_window(data: object) -> CutoverWindowDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                window_type_0 = CutoverWindowDTO.from_dict(data)

                return window_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CutoverWindowDTO | None | Unset, data)

        window = _parse_window(d.pop("window", UNSET))

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        evidence_errors = cast(list[str], d.pop("evidence_errors", UNSET))

        can_close = d.pop("can_close", UNSET)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_cutover_window_status_response = cls(
            success=success,
            generated_at=generated_at,
            runtime=runtime,
            admission=admission,
            measures=measures,
            window=window,
            blocking_reasons=blocking_reasons,
            evidence_errors=evidence_errors,
            can_close=can_close,
            error=error,
        )

        return playbook_cutover_window_status_response
