from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.script_execution_dto_output_refs import ScriptExecutionDTOOutputRefs


T = TypeVar("T", bound="ScriptExecutionDTO")


@_attrs_define
class ScriptExecutionDTO:
    """Public execution facts and opaque handles, never process output or inputs.

    Attributes:
        job_id (str):
        execution_id (None | str | Unset):
        terminal_cause (None | str | Unset):
        exit_code (int | None | Unset):
        signal (int | None | Unset):
        started_at (float | None | Unset):
        ended_at (float | None | Unset):
        json_status (None | str | Unset):
        capture_complete (bool | None | Unset):
        cleanup_complete (bool | None | Unset):
        output_refs (ScriptExecutionDTOOutputRefs | Unset):
    """

    job_id: str
    execution_id: None | str | Unset = UNSET
    terminal_cause: None | str | Unset = UNSET
    exit_code: int | None | Unset = UNSET
    signal: int | None | Unset = UNSET
    started_at: float | None | Unset = UNSET
    ended_at: float | None | Unset = UNSET
    json_status: None | str | Unset = UNSET
    capture_complete: bool | None | Unset = UNSET
    cleanup_complete: bool | None | Unset = UNSET
    output_refs: ScriptExecutionDTOOutputRefs | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        job_id = self.job_id

        execution_id: None | str | Unset
        if isinstance(self.execution_id, Unset):
            execution_id = UNSET
        else:
            execution_id = self.execution_id

        terminal_cause: None | str | Unset
        if isinstance(self.terminal_cause, Unset):
            terminal_cause = UNSET
        else:
            terminal_cause = self.terminal_cause

        exit_code: int | None | Unset
        if isinstance(self.exit_code, Unset):
            exit_code = UNSET
        else:
            exit_code = self.exit_code

        signal: int | None | Unset
        if isinstance(self.signal, Unset):
            signal = UNSET
        else:
            signal = self.signal

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        ended_at: float | None | Unset
        if isinstance(self.ended_at, Unset):
            ended_at = UNSET
        else:
            ended_at = self.ended_at

        json_status: None | str | Unset
        if isinstance(self.json_status, Unset):
            json_status = UNSET
        else:
            json_status = self.json_status

        capture_complete: bool | None | Unset
        if isinstance(self.capture_complete, Unset):
            capture_complete = UNSET
        else:
            capture_complete = self.capture_complete

        cleanup_complete: bool | None | Unset
        if isinstance(self.cleanup_complete, Unset):
            cleanup_complete = UNSET
        else:
            cleanup_complete = self.cleanup_complete

        output_refs: dict[str, Any] | Unset = UNSET
        if not isinstance(self.output_refs, Unset):
            output_refs = self.output_refs.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job_id": job_id,
            }
        )
        if execution_id is not UNSET:
            field_dict["execution_id"] = execution_id
        if terminal_cause is not UNSET:
            field_dict["terminal_cause"] = terminal_cause
        if exit_code is not UNSET:
            field_dict["exit_code"] = exit_code
        if signal is not UNSET:
            field_dict["signal"] = signal
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if ended_at is not UNSET:
            field_dict["ended_at"] = ended_at
        if json_status is not UNSET:
            field_dict["json_status"] = json_status
        if capture_complete is not UNSET:
            field_dict["capture_complete"] = capture_complete
        if cleanup_complete is not UNSET:
            field_dict["cleanup_complete"] = cleanup_complete
        if output_refs is not UNSET:
            field_dict["output_refs"] = output_refs

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.script_execution_dto_output_refs import ScriptExecutionDTOOutputRefs

        d = dict(src_dict)
        job_id = d.pop("job_id")

        def _parse_execution_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        execution_id = _parse_execution_id(d.pop("execution_id", UNSET))

        def _parse_terminal_cause(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        terminal_cause = _parse_terminal_cause(d.pop("terminal_cause", UNSET))

        def _parse_exit_code(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        exit_code = _parse_exit_code(d.pop("exit_code", UNSET))

        def _parse_signal(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        signal = _parse_signal(d.pop("signal", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_ended_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        ended_at = _parse_ended_at(d.pop("ended_at", UNSET))

        def _parse_json_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        json_status = _parse_json_status(d.pop("json_status", UNSET))

        def _parse_capture_complete(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        capture_complete = _parse_capture_complete(d.pop("capture_complete", UNSET))

        def _parse_cleanup_complete(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        cleanup_complete = _parse_cleanup_complete(d.pop("cleanup_complete", UNSET))

        _output_refs = d.pop("output_refs", UNSET)
        output_refs: ScriptExecutionDTOOutputRefs | Unset
        if isinstance(_output_refs, Unset):
            output_refs = UNSET
        else:
            output_refs = ScriptExecutionDTOOutputRefs.from_dict(_output_refs)

        script_execution_dto = cls(
            job_id=job_id,
            execution_id=execution_id,
            terminal_cause=terminal_cause,
            exit_code=exit_code,
            signal=signal,
            started_at=started_at,
            ended_at=ended_at,
            json_status=json_status,
            capture_complete=capture_complete,
            cleanup_complete=cleanup_complete,
            output_refs=output_refs,
        )

        return script_execution_dto
