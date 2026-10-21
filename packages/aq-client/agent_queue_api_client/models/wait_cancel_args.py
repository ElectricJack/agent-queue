from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_cancel_args_owner_kind_type_0 import WaitCancelArgsOwnerKindType0
from ..models.wait_cancel_args_schema_version import WaitCancelArgsSchemaVersion
from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitCancelArgs")


@_attrs_define
class WaitCancelArgs:
    """
    Attributes:
        wait_id (str):
        schema_version (WaitCancelArgsSchemaVersion | Unset):  Default: WaitCancelArgsSchemaVersion.VALUE_1.
        project_id (None | str | Unset):
        task_id (None | str | Unset):
        session_id (None | str | Unset):
        claim_epoch (int | None | Unset):
        owner_kind (None | Unset | WaitCancelArgsOwnerKindType0):
        owner_id (None | str | Unset):
    """

    wait_id: str
    schema_version: WaitCancelArgsSchemaVersion | Unset = WaitCancelArgsSchemaVersion.VALUE_1
    project_id: None | str | Unset = UNSET
    task_id: None | str | Unset = UNSET
    session_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    owner_kind: None | Unset | WaitCancelArgsOwnerKindType0 = UNSET
    owner_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        schema_version: int | Unset = UNSET
        if not isinstance(self.schema_version, Unset):
            schema_version = self.schema_version.value

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

        owner_kind: None | str | Unset
        if isinstance(self.owner_kind, Unset):
            owner_kind = UNSET
        elif isinstance(self.owner_kind, WaitCancelArgsOwnerKindType0):
            owner_kind = self.owner_kind.value
        else:
            owner_kind = self.owner_kind

        owner_id: None | str | Unset
        if isinstance(self.owner_id, Unset):
            owner_id = UNSET
        else:
            owner_id = self.owner_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_id": wait_id,
            }
        )
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if session_id is not UNSET:
            field_dict["session_id"] = session_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if owner_kind is not UNSET:
            field_dict["owner_kind"] = owner_kind
        if owner_id is not UNSET:
            field_dict["owner_id"] = owner_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        _schema_version = d.pop("schema_version", UNSET)
        schema_version: WaitCancelArgsSchemaVersion | Unset
        if isinstance(_schema_version, Unset):
            schema_version = UNSET
        else:
            schema_version = WaitCancelArgsSchemaVersion(_schema_version)

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

        def _parse_owner_kind(data: object) -> None | Unset | WaitCancelArgsOwnerKindType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                owner_kind_type_0 = WaitCancelArgsOwnerKindType0(data)

                return owner_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitCancelArgsOwnerKindType0, data)

        owner_kind = _parse_owner_kind(d.pop("owner_kind", UNSET))

        def _parse_owner_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner_id = _parse_owner_id(d.pop("owner_id", UNSET))

        wait_cancel_args = cls(
            wait_id=wait_id,
            schema_version=schema_version,
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            claim_epoch=claim_epoch,
            owner_kind=owner_kind,
            owner_id=owner_id,
        )

        return wait_cancel_args
