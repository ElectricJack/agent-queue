from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.ingest_batch_detail_state import IngestBatchDetailState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ingest_batch_detail_check_results_type_0 import IngestBatchDetailCheckResultsType0
    from ..models.ingest_batch_detail_diff_type_0 import IngestBatchDetailDiffType0
    from ..models.ingest_batch_detail_lineage import IngestBatchDetailLineage
    from ..models.ingest_batch_detail_members import IngestBatchDetailMembers
    from ..models.ingest_batch_detail_membership_item import IngestBatchDetailMembershipItem
    from ..models.ingest_batch_detail_receipts_item import IngestBatchDetailReceiptsItem
    from ..models.ingest_batch_detail_release_receipt_type_0 import IngestBatchDetailReleaseReceiptType0
    from ..models.ingest_batch_detail_section_verdicts import IngestBatchDetailSectionVerdicts
    from ..models.ingest_batch_detail_structural_findings_item import IngestBatchDetailStructuralFindingsItem


T = TypeVar("T", bound="IngestBatchDetail")


@_attrs_define
class IngestBatchDetail:
    """Durable review evidence, including receipts superseded by a graph edit.

    Attributes:
        batch_id (str):
        state (IngestBatchDetailState):
        structure_revision (int):
        source (str):
        lineage (IngestBatchDetailLineage):
        project_id (str):
        structure_digest (str):
        source_review_id (str):
        source_revision (int):
        source_content_sha256 (str):
        decision_id (str):
        members (IngestBatchDetailMembers):
        membership (list[IngestBatchDetailMembershipItem]):
        reviewer_task_id (None | str | Unset):
        check_results (IngestBatchDetailCheckResultsType0 | None | Unset):
        section_verdicts (IngestBatchDetailSectionVerdicts | Unset):
        reverse_coverage_verdict (None | str | Unset):
        walkthrough_verdict (None | str | Unset):
        structural_findings (list[IngestBatchDetailStructuralFindingsItem] | Unset):
        diff (IngestBatchDetailDiffType0 | None | Unset):
        receipts (list[IngestBatchDetailReceiptsItem] | Unset):
        release_receipt (IngestBatchDetailReleaseReceiptType0 | None | Unset):
        release_actor (None | str | Unset):
        release_revision (int | None | Unset):
        release_digest (None | str | Unset):
        released_at (float | None | Unset):
    """

    batch_id: str
    state: IngestBatchDetailState
    structure_revision: int
    source: str
    lineage: IngestBatchDetailLineage
    project_id: str
    structure_digest: str
    source_review_id: str
    source_revision: int
    source_content_sha256: str
    decision_id: str
    members: IngestBatchDetailMembers
    membership: list[IngestBatchDetailMembershipItem]
    reviewer_task_id: None | str | Unset = UNSET
    check_results: IngestBatchDetailCheckResultsType0 | None | Unset = UNSET
    section_verdicts: IngestBatchDetailSectionVerdicts | Unset = UNSET
    reverse_coverage_verdict: None | str | Unset = UNSET
    walkthrough_verdict: None | str | Unset = UNSET
    structural_findings: list[IngestBatchDetailStructuralFindingsItem] | Unset = UNSET
    diff: IngestBatchDetailDiffType0 | None | Unset = UNSET
    receipts: list[IngestBatchDetailReceiptsItem] | Unset = UNSET
    release_receipt: IngestBatchDetailReleaseReceiptType0 | None | Unset = UNSET
    release_actor: None | str | Unset = UNSET
    release_revision: int | None | Unset = UNSET
    release_digest: None | str | Unset = UNSET
    released_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.ingest_batch_detail_check_results_type_0 import IngestBatchDetailCheckResultsType0
        from ..models.ingest_batch_detail_diff_type_0 import IngestBatchDetailDiffType0
        from ..models.ingest_batch_detail_release_receipt_type_0 import IngestBatchDetailReleaseReceiptType0

        batch_id = self.batch_id

        state = self.state.value

        structure_revision = self.structure_revision

        source = self.source

        lineage = self.lineage.to_dict()

        project_id = self.project_id

        structure_digest = self.structure_digest

        source_review_id = self.source_review_id

        source_revision = self.source_revision

        source_content_sha256 = self.source_content_sha256

        decision_id = self.decision_id

        members = self.members.to_dict()

        membership = []
        for membership_item_data in self.membership:
            membership_item = membership_item_data.to_dict()
            membership.append(membership_item)

        reviewer_task_id: None | str | Unset
        if isinstance(self.reviewer_task_id, Unset):
            reviewer_task_id = UNSET
        else:
            reviewer_task_id = self.reviewer_task_id

        check_results: dict[str, Any] | None | Unset
        if isinstance(self.check_results, Unset):
            check_results = UNSET
        elif isinstance(self.check_results, IngestBatchDetailCheckResultsType0):
            check_results = self.check_results.to_dict()
        else:
            check_results = self.check_results

        section_verdicts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.section_verdicts, Unset):
            section_verdicts = self.section_verdicts.to_dict()

        reverse_coverage_verdict: None | str | Unset
        if isinstance(self.reverse_coverage_verdict, Unset):
            reverse_coverage_verdict = UNSET
        else:
            reverse_coverage_verdict = self.reverse_coverage_verdict

        walkthrough_verdict: None | str | Unset
        if isinstance(self.walkthrough_verdict, Unset):
            walkthrough_verdict = UNSET
        else:
            walkthrough_verdict = self.walkthrough_verdict

        structural_findings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.structural_findings, Unset):
            structural_findings = []
            for structural_findings_item_data in self.structural_findings:
                structural_findings_item = structural_findings_item_data.to_dict()
                structural_findings.append(structural_findings_item)

        diff: dict[str, Any] | None | Unset
        if isinstance(self.diff, Unset):
            diff = UNSET
        elif isinstance(self.diff, IngestBatchDetailDiffType0):
            diff = self.diff.to_dict()
        else:
            diff = self.diff

        receipts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.receipts, Unset):
            receipts = []
            for receipts_item_data in self.receipts:
                receipts_item = receipts_item_data.to_dict()
                receipts.append(receipts_item)

        release_receipt: dict[str, Any] | None | Unset
        if isinstance(self.release_receipt, Unset):
            release_receipt = UNSET
        elif isinstance(self.release_receipt, IngestBatchDetailReleaseReceiptType0):
            release_receipt = self.release_receipt.to_dict()
        else:
            release_receipt = self.release_receipt

        release_actor: None | str | Unset
        if isinstance(self.release_actor, Unset):
            release_actor = UNSET
        else:
            release_actor = self.release_actor

        release_revision: int | None | Unset
        if isinstance(self.release_revision, Unset):
            release_revision = UNSET
        else:
            release_revision = self.release_revision

        release_digest: None | str | Unset
        if isinstance(self.release_digest, Unset):
            release_digest = UNSET
        else:
            release_digest = self.release_digest

        released_at: float | None | Unset
        if isinstance(self.released_at, Unset):
            released_at = UNSET
        else:
            released_at = self.released_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "batch_id": batch_id,
                "state": state,
                "structure_revision": structure_revision,
                "source": source,
                "lineage": lineage,
                "project_id": project_id,
                "structure_digest": structure_digest,
                "source_review_id": source_review_id,
                "source_revision": source_revision,
                "source_content_sha256": source_content_sha256,
                "decision_id": decision_id,
                "members": members,
                "membership": membership,
            }
        )
        if reviewer_task_id is not UNSET:
            field_dict["reviewer_task_id"] = reviewer_task_id
        if check_results is not UNSET:
            field_dict["check_results"] = check_results
        if section_verdicts is not UNSET:
            field_dict["section_verdicts"] = section_verdicts
        if reverse_coverage_verdict is not UNSET:
            field_dict["reverse_coverage_verdict"] = reverse_coverage_verdict
        if walkthrough_verdict is not UNSET:
            field_dict["walkthrough_verdict"] = walkthrough_verdict
        if structural_findings is not UNSET:
            field_dict["structural_findings"] = structural_findings
        if diff is not UNSET:
            field_dict["diff"] = diff
        if receipts is not UNSET:
            field_dict["receipts"] = receipts
        if release_receipt is not UNSET:
            field_dict["release_receipt"] = release_receipt
        if release_actor is not UNSET:
            field_dict["release_actor"] = release_actor
        if release_revision is not UNSET:
            field_dict["release_revision"] = release_revision
        if release_digest is not UNSET:
            field_dict["release_digest"] = release_digest
        if released_at is not UNSET:
            field_dict["released_at"] = released_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ingest_batch_detail_check_results_type_0 import IngestBatchDetailCheckResultsType0
        from ..models.ingest_batch_detail_diff_type_0 import IngestBatchDetailDiffType0
        from ..models.ingest_batch_detail_lineage import IngestBatchDetailLineage
        from ..models.ingest_batch_detail_members import IngestBatchDetailMembers
        from ..models.ingest_batch_detail_membership_item import IngestBatchDetailMembershipItem
        from ..models.ingest_batch_detail_receipts_item import IngestBatchDetailReceiptsItem
        from ..models.ingest_batch_detail_release_receipt_type_0 import IngestBatchDetailReleaseReceiptType0
        from ..models.ingest_batch_detail_section_verdicts import IngestBatchDetailSectionVerdicts
        from ..models.ingest_batch_detail_structural_findings_item import IngestBatchDetailStructuralFindingsItem

        d = dict(src_dict)
        batch_id = d.pop("batch_id")

        state = IngestBatchDetailState(d.pop("state"))

        structure_revision = d.pop("structure_revision")

        source = d.pop("source")

        lineage = IngestBatchDetailLineage.from_dict(d.pop("lineage"))

        project_id = d.pop("project_id")

        structure_digest = d.pop("structure_digest")

        source_review_id = d.pop("source_review_id")

        source_revision = d.pop("source_revision")

        source_content_sha256 = d.pop("source_content_sha256")

        decision_id = d.pop("decision_id")

        members = IngestBatchDetailMembers.from_dict(d.pop("members"))

        membership = []
        _membership = d.pop("membership")
        for membership_item_data in _membership:
            membership_item = IngestBatchDetailMembershipItem.from_dict(membership_item_data)

            membership.append(membership_item)

        def _parse_reviewer_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reviewer_task_id = _parse_reviewer_task_id(d.pop("reviewer_task_id", UNSET))

        def _parse_check_results(data: object) -> IngestBatchDetailCheckResultsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                check_results_type_0 = IngestBatchDetailCheckResultsType0.from_dict(data)

                return check_results_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IngestBatchDetailCheckResultsType0 | None | Unset, data)

        check_results = _parse_check_results(d.pop("check_results", UNSET))

        _section_verdicts = d.pop("section_verdicts", UNSET)
        section_verdicts: IngestBatchDetailSectionVerdicts | Unset
        if isinstance(_section_verdicts, Unset):
            section_verdicts = UNSET
        else:
            section_verdicts = IngestBatchDetailSectionVerdicts.from_dict(_section_verdicts)

        def _parse_reverse_coverage_verdict(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reverse_coverage_verdict = _parse_reverse_coverage_verdict(d.pop("reverse_coverage_verdict", UNSET))

        def _parse_walkthrough_verdict(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        walkthrough_verdict = _parse_walkthrough_verdict(d.pop("walkthrough_verdict", UNSET))

        _structural_findings = d.pop("structural_findings", UNSET)
        structural_findings: list[IngestBatchDetailStructuralFindingsItem] | Unset = UNSET
        if _structural_findings is not UNSET:
            structural_findings = []
            for structural_findings_item_data in _structural_findings:
                structural_findings_item = IngestBatchDetailStructuralFindingsItem.from_dict(
                    structural_findings_item_data
                )

                structural_findings.append(structural_findings_item)

        def _parse_diff(data: object) -> IngestBatchDetailDiffType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diff_type_0 = IngestBatchDetailDiffType0.from_dict(data)

                return diff_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IngestBatchDetailDiffType0 | None | Unset, data)

        diff = _parse_diff(d.pop("diff", UNSET))

        _receipts = d.pop("receipts", UNSET)
        receipts: list[IngestBatchDetailReceiptsItem] | Unset = UNSET
        if _receipts is not UNSET:
            receipts = []
            for receipts_item_data in _receipts:
                receipts_item = IngestBatchDetailReceiptsItem.from_dict(receipts_item_data)

                receipts.append(receipts_item)

        def _parse_release_receipt(data: object) -> IngestBatchDetailReleaseReceiptType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                release_receipt_type_0 = IngestBatchDetailReleaseReceiptType0.from_dict(data)

                return release_receipt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(IngestBatchDetailReleaseReceiptType0 | None | Unset, data)

        release_receipt = _parse_release_receipt(d.pop("release_receipt", UNSET))

        def _parse_release_actor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_actor = _parse_release_actor(d.pop("release_actor", UNSET))

        def _parse_release_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        release_revision = _parse_release_revision(d.pop("release_revision", UNSET))

        def _parse_release_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        release_digest = _parse_release_digest(d.pop("release_digest", UNSET))

        def _parse_released_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        released_at = _parse_released_at(d.pop("released_at", UNSET))

        ingest_batch_detail = cls(
            batch_id=batch_id,
            state=state,
            structure_revision=structure_revision,
            source=source,
            lineage=lineage,
            project_id=project_id,
            structure_digest=structure_digest,
            source_review_id=source_review_id,
            source_revision=source_revision,
            source_content_sha256=source_content_sha256,
            decision_id=decision_id,
            members=members,
            membership=membership,
            reviewer_task_id=reviewer_task_id,
            check_results=check_results,
            section_verdicts=section_verdicts,
            reverse_coverage_verdict=reverse_coverage_verdict,
            walkthrough_verdict=walkthrough_verdict,
            structural_findings=structural_findings,
            diff=diff,
            receipts=receipts,
            release_receipt=release_receipt,
            release_actor=release_actor,
            release_revision=release_revision,
            release_digest=release_digest,
            released_at=released_at,
        )

        ingest_batch_detail.additional_properties = d
        return ingest_batch_detail

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
