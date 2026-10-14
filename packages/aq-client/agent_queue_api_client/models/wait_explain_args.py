from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitExplainArgs")


@_attrs_define
class WaitExplainArgs:
    """
    Attributes:
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        claim_epoch (int | None | Unset):
        wait_id (None | str | Unset):
        history (bool | Unset):  Default: False.
    """

    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    wait_id: None | str | Unset = UNSET
    history: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        session_id: None | str | Unset
        if isinstance(self.session_id, Unset):
            session_id = UNSET
        else:
            session_id = self.session_id

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        wait_id: None | str | Unset
        if isinstance(self.wait_id, Unset):
            wait_id = UNSET
        else:
            wait_id = self.wait_id

        history = self.history

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if wait_id is not UNSET:
            field_dict["wait_id"] = wait_id
        if history is not UNSET:
            field_dict["history"] = history

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_session_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_id = _parse_session_id(d.pop("session_id", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_wait_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        wait_id = _parse_wait_id(d.pop("wait_id", UNSET))

        history = d.pop("history", UNSET)

        wait_explain_args = cls(
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            claim_epoch=claim_epoch,
            wait_id=wait_id,
            history=history,
        )

        return wait_explain_args
