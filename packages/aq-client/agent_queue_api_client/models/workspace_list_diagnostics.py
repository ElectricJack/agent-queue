from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.workspace_list_diagnostics_git_status_counts import WorkspaceListDiagnosticsGitStatusCounts
    from ..models.workspace_list_diagnostics_presence_counts import WorkspaceListDiagnosticsPresenceCounts


T = TypeVar("T", bound="WorkspaceListDiagnostics")


@_attrs_define
class WorkspaceListDiagnostics:
    """
    Attributes:
        complete (bool):
        deadline_seconds (float):
        elapsed_seconds (float):
        owner_facts (str):
        git_requested (bool):
        authority (str):
        owner_facts_error (None | str | Unset):
        git_status_counts (WorkspaceListDiagnosticsGitStatusCounts | Unset):
        presence_counts (WorkspaceListDiagnosticsPresenceCounts | Unset):
        incomplete_rows (int | Unset):  Default: 0.
    """

    complete: bool
    deadline_seconds: float
    elapsed_seconds: float
    owner_facts: str
    git_requested: bool
    authority: str
    owner_facts_error: None | str | Unset = UNSET
    git_status_counts: WorkspaceListDiagnosticsGitStatusCounts | Unset = UNSET
    presence_counts: WorkspaceListDiagnosticsPresenceCounts | Unset = UNSET
    incomplete_rows: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        complete = self.complete

        deadline_seconds = self.deadline_seconds

        elapsed_seconds = self.elapsed_seconds

        owner_facts = self.owner_facts

        git_requested = self.git_requested

        authority = self.authority

        owner_facts_error: None | str | Unset
        if isinstance(self.owner_facts_error, Unset):
            owner_facts_error = UNSET
        else:
            owner_facts_error = self.owner_facts_error

        git_status_counts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.git_status_counts, Unset):
            git_status_counts = self.git_status_counts.to_dict()

        presence_counts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.presence_counts, Unset):
            presence_counts = self.presence_counts.to_dict()

        incomplete_rows = self.incomplete_rows

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "complete": complete,
                "deadline_seconds": deadline_seconds,
                "elapsed_seconds": elapsed_seconds,
                "owner_facts": owner_facts,
                "git_requested": git_requested,
                "authority": authority,
            }
        )
        if owner_facts_error is not UNSET:
            field_dict["owner_facts_error"] = owner_facts_error
        if git_status_counts is not UNSET:
            field_dict["git_status_counts"] = git_status_counts
        if presence_counts is not UNSET:
            field_dict["presence_counts"] = presence_counts
        if incomplete_rows is not UNSET:
            field_dict["incomplete_rows"] = incomplete_rows

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.workspace_list_diagnostics_git_status_counts import WorkspaceListDiagnosticsGitStatusCounts
        from ..models.workspace_list_diagnostics_presence_counts import WorkspaceListDiagnosticsPresenceCounts

        d = dict(src_dict)
        complete = d.pop("complete")

        deadline_seconds = d.pop("deadline_seconds")

        elapsed_seconds = d.pop("elapsed_seconds")

        owner_facts = d.pop("owner_facts")

        git_requested = d.pop("git_requested")

        authority = d.pop("authority")

        def _parse_owner_facts_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        owner_facts_error = _parse_owner_facts_error(d.pop("owner_facts_error", UNSET))

        _git_status_counts = d.pop("git_status_counts", UNSET)
        git_status_counts: WorkspaceListDiagnosticsGitStatusCounts | Unset
        if isinstance(_git_status_counts, Unset):
            git_status_counts = UNSET
        else:
            git_status_counts = WorkspaceListDiagnosticsGitStatusCounts.from_dict(_git_status_counts)

        _presence_counts = d.pop("presence_counts", UNSET)
        presence_counts: WorkspaceListDiagnosticsPresenceCounts | Unset
        if isinstance(_presence_counts, Unset):
            presence_counts = UNSET
        else:
            presence_counts = WorkspaceListDiagnosticsPresenceCounts.from_dict(_presence_counts)

        incomplete_rows = d.pop("incomplete_rows", UNSET)

        workspace_list_diagnostics = cls(
            complete=complete,
            deadline_seconds=deadline_seconds,
            elapsed_seconds=elapsed_seconds,
            owner_facts=owner_facts,
            git_requested=git_requested,
            authority=authority,
            owner_facts_error=owner_facts_error,
            git_status_counts=git_status_counts,
            presence_counts=presence_counts,
            incomplete_rows=incomplete_rows,
        )

        workspace_list_diagnostics.additional_properties = d
        return workspace_list_diagnostics

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
