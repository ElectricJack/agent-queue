from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.job_retain_response_artifacts_item import JobRetainResponseArtifactsItem
    from ..models.job_retain_response_candidate_artifact import JobRetainResponseCandidateArtifact
    from ..models.job_retain_response_captures_item import JobRetainResponseCapturesItem
    from ..models.job_retain_response_editor_pin_type_0 import JobRetainResponseEditorPinType0
    from ..models.job_retain_response_render_profile_type_0 import JobRetainResponseRenderProfileType0


T = TypeVar("T", bound="JobRetainResponse")


@_attrs_define
class JobRetainResponse:
    """
    Attributes:
        job_id (str):
        candidate_artifact (JobRetainResponseCandidateArtifact):
        candidate_sha256 (None | str | Unset):
        rig_sha256 (None | str | Unset):
        render_profile (JobRetainResponseRenderProfileType0 | None | Unset):
        render_profile_sha256 (None | str | Unset):
        editor_pin (JobRetainResponseEditorPinType0 | None | Unset):
        artifacts (list[JobRetainResponseArtifactsItem] | Unset):
        captures (list[JobRetainResponseCapturesItem] | Unset):
        next_step (None | str | Unset):
        success (bool | Unset):  Default: True.
    """

    job_id: str
    candidate_artifact: JobRetainResponseCandidateArtifact
    candidate_sha256: None | str | Unset = UNSET
    rig_sha256: None | str | Unset = UNSET
    render_profile: JobRetainResponseRenderProfileType0 | None | Unset = UNSET
    render_profile_sha256: None | str | Unset = UNSET
    editor_pin: JobRetainResponseEditorPinType0 | None | Unset = UNSET
    artifacts: list[JobRetainResponseArtifactsItem] | Unset = UNSET
    captures: list[JobRetainResponseCapturesItem] | Unset = UNSET
    next_step: None | str | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.job_retain_response_editor_pin_type_0 import JobRetainResponseEditorPinType0
        from ..models.job_retain_response_render_profile_type_0 import JobRetainResponseRenderProfileType0

        job_id = self.job_id

        candidate_artifact = self.candidate_artifact.to_dict()

        candidate_sha256: None | str | Unset
        if isinstance(self.candidate_sha256, Unset):
            candidate_sha256 = UNSET
        else:
            candidate_sha256 = self.candidate_sha256

        rig_sha256: None | str | Unset
        if isinstance(self.rig_sha256, Unset):
            rig_sha256 = UNSET
        else:
            rig_sha256 = self.rig_sha256

        render_profile: dict[str, Any] | None | Unset
        if isinstance(self.render_profile, Unset):
            render_profile = UNSET
        elif isinstance(self.render_profile, JobRetainResponseRenderProfileType0):
            render_profile = self.render_profile.to_dict()
        else:
            render_profile = self.render_profile

        render_profile_sha256: None | str | Unset
        if isinstance(self.render_profile_sha256, Unset):
            render_profile_sha256 = UNSET
        else:
            render_profile_sha256 = self.render_profile_sha256

        editor_pin: dict[str, Any] | None | Unset
        if isinstance(self.editor_pin, Unset):
            editor_pin = UNSET
        elif isinstance(self.editor_pin, JobRetainResponseEditorPinType0):
            editor_pin = self.editor_pin.to_dict()
        else:
            editor_pin = self.editor_pin

        artifacts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.artifacts, Unset):
            artifacts = []
            for artifacts_item_data in self.artifacts:
                artifacts_item = artifacts_item_data.to_dict()
                artifacts.append(artifacts_item)

        captures: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.captures, Unset):
            captures = []
            for captures_item_data in self.captures:
                captures_item = captures_item_data.to_dict()
                captures.append(captures_item)

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "job_id": job_id,
                "candidate_artifact": candidate_artifact,
            }
        )
        if candidate_sha256 is not UNSET:
            field_dict["candidate_sha256"] = candidate_sha256
        if rig_sha256 is not UNSET:
            field_dict["rig_sha256"] = rig_sha256
        if render_profile is not UNSET:
            field_dict["render_profile"] = render_profile
        if render_profile_sha256 is not UNSET:
            field_dict["render_profile_sha256"] = render_profile_sha256
        if editor_pin is not UNSET:
            field_dict["editor_pin"] = editor_pin
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if captures is not UNSET:
            field_dict["captures"] = captures
        if next_step is not UNSET:
            field_dict["next_step"] = next_step
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_retain_response_artifacts_item import JobRetainResponseArtifactsItem
        from ..models.job_retain_response_candidate_artifact import JobRetainResponseCandidateArtifact
        from ..models.job_retain_response_captures_item import JobRetainResponseCapturesItem
        from ..models.job_retain_response_editor_pin_type_0 import JobRetainResponseEditorPinType0
        from ..models.job_retain_response_render_profile_type_0 import JobRetainResponseRenderProfileType0

        d = dict(src_dict)
        job_id = d.pop("job_id")

        candidate_artifact = JobRetainResponseCandidateArtifact.from_dict(d.pop("candidate_artifact"))

        def _parse_candidate_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        candidate_sha256 = _parse_candidate_sha256(d.pop("candidate_sha256", UNSET))

        def _parse_rig_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rig_sha256 = _parse_rig_sha256(d.pop("rig_sha256", UNSET))

        def _parse_render_profile(data: object) -> JobRetainResponseRenderProfileType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                render_profile_type_0 = JobRetainResponseRenderProfileType0.from_dict(data)

                return render_profile_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobRetainResponseRenderProfileType0 | None | Unset, data)

        render_profile = _parse_render_profile(d.pop("render_profile", UNSET))

        def _parse_render_profile_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        render_profile_sha256 = _parse_render_profile_sha256(d.pop("render_profile_sha256", UNSET))

        def _parse_editor_pin(data: object) -> JobRetainResponseEditorPinType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                editor_pin_type_0 = JobRetainResponseEditorPinType0.from_dict(data)

                return editor_pin_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(JobRetainResponseEditorPinType0 | None | Unset, data)

        editor_pin = _parse_editor_pin(d.pop("editor_pin", UNSET))

        _artifacts = d.pop("artifacts", UNSET)
        artifacts: list[JobRetainResponseArtifactsItem] | Unset = UNSET
        if _artifacts is not UNSET:
            artifacts = []
            for artifacts_item_data in _artifacts:
                artifacts_item = JobRetainResponseArtifactsItem.from_dict(artifacts_item_data)

                artifacts.append(artifacts_item)

        _captures = d.pop("captures", UNSET)
        captures: list[JobRetainResponseCapturesItem] | Unset = UNSET
        if _captures is not UNSET:
            captures = []
            for captures_item_data in _captures:
                captures_item = JobRetainResponseCapturesItem.from_dict(captures_item_data)

                captures.append(captures_item)

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        success = d.pop("success", UNSET)

        job_retain_response = cls(
            job_id=job_id,
            candidate_artifact=candidate_artifact,
            candidate_sha256=candidate_sha256,
            rig_sha256=rig_sha256,
            render_profile=render_profile,
            render_profile_sha256=render_profile_sha256,
            editor_pin=editor_pin,
            artifacts=artifacts,
            captures=captures,
            next_step=next_step,
            success=success,
        )

        return job_retain_response
