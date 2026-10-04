from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.object_loop_start_request_final_reserve import ObjectLoopStartRequestFinalReserve
    from ..models.object_loop_start_request_incumbent_artifact_type_0 import (
        ObjectLoopStartRequestIncumbentArtifactType0,
    )
    from ..models.object_loop_start_request_limits import ObjectLoopStartRequestLimits
    from ..models.object_loop_start_request_score_reservation import ObjectLoopStartRequestScoreReservation


T = TypeVar("T", bound="ObjectLoopStartRequest")


@_attrs_define
class ObjectLoopStartRequest:
    """
    Attributes:
        project_id (str):
        epic_task_id (str):
        object_id (str):
        attempt_id (str):
        incumbent_sha256 (str):
        reference_sha256 (str):
        rig_sha256 (str):
        scorer_sha256 (str):
        render_profile_sha256 (str):
        policy_sha256 (str):
        brief_review_id (str):
        brief_review_revision (int):
        brief_review_sha256 (str):
        mandatory_views (list[Any]):
        limits (ObjectLoopStartRequestLimits):
        final_reserve (ObjectLoopStartRequestFinalReserve):
        score_reservation (ObjectLoopStartRequestScoreReservation):
        noise_band (float):
        variants (list[Any]):
        incumbent_artifact (None | ObjectLoopStartRequestIncumbentArtifactType0 | Unset):
        max_rounds (int | Unset):  Default: 8.
        max_repair_rounds (int | Unset):  Default: 2.
        max_plateau_rounds (int | Unset):  Default: 3.
    """

    project_id: str
    epic_task_id: str
    object_id: str
    attempt_id: str
    incumbent_sha256: str
    reference_sha256: str
    rig_sha256: str
    scorer_sha256: str
    render_profile_sha256: str
    policy_sha256: str
    brief_review_id: str
    brief_review_revision: int
    brief_review_sha256: str
    mandatory_views: list[Any]
    limits: ObjectLoopStartRequestLimits
    final_reserve: ObjectLoopStartRequestFinalReserve
    score_reservation: ObjectLoopStartRequestScoreReservation
    noise_band: float
    variants: list[Any]
    incumbent_artifact: None | ObjectLoopStartRequestIncumbentArtifactType0 | Unset = UNSET
    max_rounds: int | Unset = 8
    max_repair_rounds: int | Unset = 2
    max_plateau_rounds: int | Unset = 3
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.object_loop_start_request_incumbent_artifact_type_0 import (
            ObjectLoopStartRequestIncumbentArtifactType0,
        )

        project_id = self.project_id

        epic_task_id = self.epic_task_id

        object_id = self.object_id

        attempt_id = self.attempt_id

        incumbent_sha256 = self.incumbent_sha256

        reference_sha256 = self.reference_sha256

        rig_sha256 = self.rig_sha256

        scorer_sha256 = self.scorer_sha256

        render_profile_sha256 = self.render_profile_sha256

        policy_sha256 = self.policy_sha256

        brief_review_id = self.brief_review_id

        brief_review_revision = self.brief_review_revision

        brief_review_sha256 = self.brief_review_sha256

        mandatory_views = self.mandatory_views

        limits = self.limits.to_dict()

        final_reserve = self.final_reserve.to_dict()

        score_reservation = self.score_reservation.to_dict()

        noise_band = self.noise_band

        variants = self.variants

        incumbent_artifact: dict[str, Any] | None | Unset
        if isinstance(self.incumbent_artifact, Unset):
            incumbent_artifact = UNSET
        elif isinstance(self.incumbent_artifact, ObjectLoopStartRequestIncumbentArtifactType0):
            incumbent_artifact = self.incumbent_artifact.to_dict()
        else:
            incumbent_artifact = self.incumbent_artifact

        max_rounds = self.max_rounds

        max_repair_rounds = self.max_repair_rounds

        max_plateau_rounds = self.max_plateau_rounds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "epic_task_id": epic_task_id,
                "object_id": object_id,
                "attempt_id": attempt_id,
                "incumbent_sha256": incumbent_sha256,
                "reference_sha256": reference_sha256,
                "rig_sha256": rig_sha256,
                "scorer_sha256": scorer_sha256,
                "render_profile_sha256": render_profile_sha256,
                "policy_sha256": policy_sha256,
                "brief_review_id": brief_review_id,
                "brief_review_revision": brief_review_revision,
                "brief_review_sha256": brief_review_sha256,
                "mandatory_views": mandatory_views,
                "limits": limits,
                "final_reserve": final_reserve,
                "score_reservation": score_reservation,
                "noise_band": noise_band,
                "variants": variants,
            }
        )
        if incumbent_artifact is not UNSET:
            field_dict["incumbent_artifact"] = incumbent_artifact
        if max_rounds is not UNSET:
            field_dict["max_rounds"] = max_rounds
        if max_repair_rounds is not UNSET:
            field_dict["max_repair_rounds"] = max_repair_rounds
        if max_plateau_rounds is not UNSET:
            field_dict["max_plateau_rounds"] = max_plateau_rounds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.object_loop_start_request_final_reserve import ObjectLoopStartRequestFinalReserve
        from ..models.object_loop_start_request_incumbent_artifact_type_0 import (
            ObjectLoopStartRequestIncumbentArtifactType0,
        )
        from ..models.object_loop_start_request_limits import ObjectLoopStartRequestLimits
        from ..models.object_loop_start_request_score_reservation import ObjectLoopStartRequestScoreReservation

        d = dict(src_dict)
        project_id = d.pop("project_id")

        epic_task_id = d.pop("epic_task_id")

        object_id = d.pop("object_id")

        attempt_id = d.pop("attempt_id")

        incumbent_sha256 = d.pop("incumbent_sha256")

        reference_sha256 = d.pop("reference_sha256")

        rig_sha256 = d.pop("rig_sha256")

        scorer_sha256 = d.pop("scorer_sha256")

        render_profile_sha256 = d.pop("render_profile_sha256")

        policy_sha256 = d.pop("policy_sha256")

        brief_review_id = d.pop("brief_review_id")

        brief_review_revision = d.pop("brief_review_revision")

        brief_review_sha256 = d.pop("brief_review_sha256")

        mandatory_views = cast(list[Any], d.pop("mandatory_views"))

        limits = ObjectLoopStartRequestLimits.from_dict(d.pop("limits"))

        final_reserve = ObjectLoopStartRequestFinalReserve.from_dict(d.pop("final_reserve"))

        score_reservation = ObjectLoopStartRequestScoreReservation.from_dict(d.pop("score_reservation"))

        noise_band = d.pop("noise_band")

        variants = cast(list[Any], d.pop("variants"))

        def _parse_incumbent_artifact(data: object) -> None | ObjectLoopStartRequestIncumbentArtifactType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                incumbent_artifact_type_0 = ObjectLoopStartRequestIncumbentArtifactType0.from_dict(data)

                return incumbent_artifact_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ObjectLoopStartRequestIncumbentArtifactType0 | Unset, data)

        incumbent_artifact = _parse_incumbent_artifact(d.pop("incumbent_artifact", UNSET))

        max_rounds = d.pop("max_rounds", UNSET)

        max_repair_rounds = d.pop("max_repair_rounds", UNSET)

        max_plateau_rounds = d.pop("max_plateau_rounds", UNSET)

        object_loop_start_request = cls(
            project_id=project_id,
            epic_task_id=epic_task_id,
            object_id=object_id,
            attempt_id=attempt_id,
            incumbent_sha256=incumbent_sha256,
            reference_sha256=reference_sha256,
            rig_sha256=rig_sha256,
            scorer_sha256=scorer_sha256,
            render_profile_sha256=render_profile_sha256,
            policy_sha256=policy_sha256,
            brief_review_id=brief_review_id,
            brief_review_revision=brief_review_revision,
            brief_review_sha256=brief_review_sha256,
            mandatory_views=mandatory_views,
            limits=limits,
            final_reserve=final_reserve,
            score_reservation=score_reservation,
            noise_band=noise_band,
            variants=variants,
            incumbent_artifact=incumbent_artifact,
            max_rounds=max_rounds,
            max_repair_rounds=max_repair_rounds,
            max_plateau_rounds=max_plateau_rounds,
        )

        object_loop_start_request.additional_properties = d
        return object_loop_start_request

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
