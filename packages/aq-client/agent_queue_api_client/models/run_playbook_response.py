from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.run_playbook_response_failed_steps_item import RunPlaybookResponseFailedStepsItem
    from ..models.run_playbook_response_node_trace_item import RunPlaybookResponseNodeTraceItem
    from ..models.run_playbook_response_runs_item import RunPlaybookResponseRunsItem


T = TypeVar("T", bound="RunPlaybookResponse")


@_attrs_define
class RunPlaybookResponse:
    """
    Attributes:
        run_id (str):
        playbook_id (str):
        status (str):
        version (int | Unset):  Default: 0.
        tokens_used (int | Unset):  Default: 0.
        node_count (int | Unset):  Default: 0.
        node_trace (list[RunPlaybookResponseNodeTraceItem] | Unset):
        error (None | str | Unset):
        final_response (None | str | Unset):
        run_ids (list[str] | Unset):
        runs (list[RunPlaybookResponseRunsItem] | Unset):
        failed_steps (list[RunPlaybookResponseFailedStepsItem] | Unset):
    """

    run_id: str
    playbook_id: str
    status: str
    version: int | Unset = 0
    tokens_used: int | Unset = 0
    node_count: int | Unset = 0
    node_trace: list[RunPlaybookResponseNodeTraceItem] | Unset = UNSET
    error: None | str | Unset = UNSET
    final_response: None | str | Unset = UNSET
    run_ids: list[str] | Unset = UNSET
    runs: list[RunPlaybookResponseRunsItem] | Unset = UNSET
    failed_steps: list[RunPlaybookResponseFailedStepsItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id

        playbook_id = self.playbook_id

        status = self.status

        version = self.version

        tokens_used = self.tokens_used

        node_count = self.node_count

        node_trace: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.node_trace, Unset):
            node_trace = []
            for node_trace_item_data in self.node_trace:
                node_trace_item = node_trace_item_data.to_dict()
                node_trace.append(node_trace_item)

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        final_response: None | str | Unset
        if isinstance(self.final_response, Unset):
            final_response = UNSET
        else:
            final_response = self.final_response

        run_ids: list[str] | Unset = UNSET
        if not isinstance(self.run_ids, Unset):
            run_ids = self.run_ids

        runs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.runs, Unset):
            runs = []
            for runs_item_data in self.runs:
                runs_item = runs_item_data.to_dict()
                runs.append(runs_item)

        failed_steps: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.failed_steps, Unset):
            failed_steps = []
            for failed_steps_item_data in self.failed_steps:
                failed_steps_item = failed_steps_item_data.to_dict()
                failed_steps.append(failed_steps_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
                "playbook_id": playbook_id,
                "status": status,
            }
        )
        if version is not UNSET:
            field_dict["version"] = version
        if tokens_used is not UNSET:
            field_dict["tokens_used"] = tokens_used
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if node_trace is not UNSET:
            field_dict["node_trace"] = node_trace
        if error is not UNSET:
            field_dict["error"] = error
        if final_response is not UNSET:
            field_dict["final_response"] = final_response
        if run_ids is not UNSET:
            field_dict["run_ids"] = run_ids
        if runs is not UNSET:
            field_dict["runs"] = runs
        if failed_steps is not UNSET:
            field_dict["failed_steps"] = failed_steps

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_playbook_response_failed_steps_item import RunPlaybookResponseFailedStepsItem
        from ..models.run_playbook_response_node_trace_item import RunPlaybookResponseNodeTraceItem
        from ..models.run_playbook_response_runs_item import RunPlaybookResponseRunsItem

        d = dict(src_dict)
        run_id = d.pop("run_id")

        playbook_id = d.pop("playbook_id")

        status = d.pop("status")

        version = d.pop("version", UNSET)

        tokens_used = d.pop("tokens_used", UNSET)

        node_count = d.pop("node_count", UNSET)

        _node_trace = d.pop("node_trace", UNSET)
        node_trace: list[RunPlaybookResponseNodeTraceItem] | Unset = UNSET
        if _node_trace is not UNSET:
            node_trace = []
            for node_trace_item_data in _node_trace:
                node_trace_item = RunPlaybookResponseNodeTraceItem.from_dict(node_trace_item_data)

                node_trace.append(node_trace_item)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_final_response(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        final_response = _parse_final_response(d.pop("final_response", UNSET))

        run_ids = cast(list[str], d.pop("run_ids", UNSET))

        _runs = d.pop("runs", UNSET)
        runs: list[RunPlaybookResponseRunsItem] | Unset = UNSET
        if _runs is not UNSET:
            runs = []
            for runs_item_data in _runs:
                runs_item = RunPlaybookResponseRunsItem.from_dict(runs_item_data)

                runs.append(runs_item)

        _failed_steps = d.pop("failed_steps", UNSET)
        failed_steps: list[RunPlaybookResponseFailedStepsItem] | Unset = UNSET
        if _failed_steps is not UNSET:
            failed_steps = []
            for failed_steps_item_data in _failed_steps:
                failed_steps_item = RunPlaybookResponseFailedStepsItem.from_dict(failed_steps_item_data)

                failed_steps.append(failed_steps_item)

        run_playbook_response = cls(
            run_id=run_id,
            playbook_id=playbook_id,
            status=status,
            version=version,
            tokens_used=tokens_used,
            node_count=node_count,
            node_trace=node_trace,
            error=error,
            final_response=final_response,
            run_ids=run_ids,
            runs=runs,
            failed_steps=failed_steps,
        )

        run_playbook_response.additional_properties = d
        return run_playbook_response

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
