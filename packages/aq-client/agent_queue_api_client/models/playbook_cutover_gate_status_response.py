from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_cutover_gate_status_response_runtime_type_0 import PlaybookCutoverGateStatusResponseRuntimeType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_authorization_dto import CutoverAuthorizationDTO
    from ..models.cutover_event_dto import CutoverEventDTO
    from ..models.playbook_cutover_gate_status_response_checks_item import PlaybookCutoverGateStatusResponseChecksItem


T = TypeVar("T", bound="PlaybookCutoverGateStatusResponse")


@_attrs_define
class PlaybookCutoverGateStatusResponse:
    """Readiness, the current G1 sign-off and the G2 signatures, recomputed
    from source on every call.  ``can_switch`` is the conjunction.

        Attributes:
            success (bool | Unset):  Default: True.
            generated_at (float | None | Unset):
            runtime (None | PlaybookCutoverGateStatusResponseRuntimeType0 | Unset):
            ready (bool | Unset):  Default: False.
            checks (list[PlaybookCutoverGateStatusResponseChecksItem] | Unset):
            drain_signoff (CutoverEventDTO | None | Unset):
            authorizations (list[CutoverAuthorizationDTO] | Unset):
            blocking_reasons (list[str] | Unset):
            can_switch (bool | Unset):  Default: False.
            error (None | str | Unset):
    """

    success: bool | Unset = True
    generated_at: float | None | Unset = UNSET
    runtime: None | PlaybookCutoverGateStatusResponseRuntimeType0 | Unset = UNSET
    ready: bool | Unset = False
    checks: list[PlaybookCutoverGateStatusResponseChecksItem] | Unset = UNSET
    drain_signoff: CutoverEventDTO | None | Unset = UNSET
    authorizations: list[CutoverAuthorizationDTO] | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
    can_switch: bool | Unset = False
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.cutover_event_dto import CutoverEventDTO

        success = self.success

        generated_at: float | None | Unset
        if isinstance(self.generated_at, Unset):
            generated_at = UNSET
        else:
            generated_at = self.generated_at

        runtime: None | str | Unset
        if isinstance(self.runtime, Unset):
            runtime = UNSET
        elif isinstance(self.runtime, PlaybookCutoverGateStatusResponseRuntimeType0):
            runtime = self.runtime.value
        else:
            runtime = self.runtime

        ready = self.ready

        checks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.checks, Unset):
            checks = []
            for checks_item_data in self.checks:
                checks_item = checks_item_data.to_dict()
                checks.append(checks_item)

        drain_signoff: dict[str, Any] | None | Unset
        if isinstance(self.drain_signoff, Unset):
            drain_signoff = UNSET
        elif isinstance(self.drain_signoff, CutoverEventDTO):
            drain_signoff = self.drain_signoff.to_dict()
        else:
            drain_signoff = self.drain_signoff

        authorizations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.authorizations, Unset):
            authorizations = []
            for authorizations_item_data in self.authorizations:
                authorizations_item = authorizations_item_data.to_dict()
                authorizations.append(authorizations_item)

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        can_switch = self.can_switch

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
        if ready is not UNSET:
            field_dict["ready"] = ready
        if checks is not UNSET:
            field_dict["checks"] = checks
        if drain_signoff is not UNSET:
            field_dict["drain_signoff"] = drain_signoff
        if authorizations is not UNSET:
            field_dict["authorizations"] = authorizations
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if can_switch is not UNSET:
            field_dict["can_switch"] = can_switch
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_authorization_dto import CutoverAuthorizationDTO
        from ..models.cutover_event_dto import CutoverEventDTO
        from ..models.playbook_cutover_gate_status_response_checks_item import (
            PlaybookCutoverGateStatusResponseChecksItem,
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

        def _parse_runtime(data: object) -> None | PlaybookCutoverGateStatusResponseRuntimeType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                runtime_type_0 = PlaybookCutoverGateStatusResponseRuntimeType0(data)

                return runtime_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookCutoverGateStatusResponseRuntimeType0 | Unset, data)

        runtime = _parse_runtime(d.pop("runtime", UNSET))

        ready = d.pop("ready", UNSET)

        _checks = d.pop("checks", UNSET)
        checks: list[PlaybookCutoverGateStatusResponseChecksItem] | Unset = UNSET
        if _checks is not UNSET:
            checks = []
            for checks_item_data in _checks:
                checks_item = PlaybookCutoverGateStatusResponseChecksItem.from_dict(checks_item_data)

                checks.append(checks_item)

        def _parse_drain_signoff(data: object) -> CutoverEventDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                drain_signoff_type_0 = CutoverEventDTO.from_dict(data)

                return drain_signoff_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CutoverEventDTO | None | Unset, data)

        drain_signoff = _parse_drain_signoff(d.pop("drain_signoff", UNSET))

        _authorizations = d.pop("authorizations", UNSET)
        authorizations: list[CutoverAuthorizationDTO] | Unset = UNSET
        if _authorizations is not UNSET:
            authorizations = []
            for authorizations_item_data in _authorizations:
                authorizations_item = CutoverAuthorizationDTO.from_dict(authorizations_item_data)

                authorizations.append(authorizations_item)

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        can_switch = d.pop("can_switch", UNSET)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_cutover_gate_status_response = cls(
            success=success,
            generated_at=generated_at,
            runtime=runtime,
            ready=ready,
            checks=checks,
            drain_signoff=drain_signoff,
            authorizations=authorizations,
            blocking_reasons=blocking_reasons,
            can_switch=can_switch,
            error=error,
        )

        return playbook_cutover_gate_status_response
