from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.project_relocate_response_binding_type_0 import ProjectRelocateResponseBindingType0
    from ..models.project_relocate_response_plan_type_0 import ProjectRelocateResponsePlanType0


T = TypeVar("T", bound="ProjectRelocateResponse")


@_attrs_define
class ProjectRelocateResponse:
    """
    Attributes:
        success (bool):
        preview (bool | None | Unset):
        digest (None | str | Unset):
        plan (None | ProjectRelocateResponsePlanType0 | Unset):
        blockers (list[str] | Unset):
        already_applied (bool | Unset):  Default: False.
        binding (None | ProjectRelocateResponseBindingType0 | Unset):
        recovery_manifest (None | str | Unset):
        rollback (None | str | Unset):
        error_code (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool
    preview: bool | None | Unset = UNSET
    digest: None | str | Unset = UNSET
    plan: None | ProjectRelocateResponsePlanType0 | Unset = UNSET
    blockers: list[str] | Unset = UNSET
    already_applied: bool | Unset = False
    binding: None | ProjectRelocateResponseBindingType0 | Unset = UNSET
    recovery_manifest: None | str | Unset = UNSET
    rollback: None | str | Unset = UNSET
    error_code: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.project_relocate_response_binding_type_0 import ProjectRelocateResponseBindingType0
        from ..models.project_relocate_response_plan_type_0 import ProjectRelocateResponsePlanType0

        success = self.success

        preview: bool | None | Unset
        if isinstance(self.preview, Unset):
            preview = UNSET
        else:
            preview = self.preview

        digest: None | str | Unset
        if isinstance(self.digest, Unset):
            digest = UNSET
        else:
            digest = self.digest

        plan: dict[str, Any] | None | Unset
        if isinstance(self.plan, Unset):
            plan = UNSET
        elif isinstance(self.plan, ProjectRelocateResponsePlanType0):
            plan = self.plan.to_dict()
        else:
            plan = self.plan

        blockers: list[str] | Unset = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = self.blockers

        already_applied = self.already_applied

        binding: dict[str, Any] | None | Unset
        if isinstance(self.binding, Unset):
            binding = UNSET
        elif isinstance(self.binding, ProjectRelocateResponseBindingType0):
            binding = self.binding.to_dict()
        else:
            binding = self.binding

        recovery_manifest: None | str | Unset
        if isinstance(self.recovery_manifest, Unset):
            recovery_manifest = UNSET
        else:
            recovery_manifest = self.recovery_manifest

        rollback: None | str | Unset
        if isinstance(self.rollback, Unset):
            rollback = UNSET
        else:
            rollback = self.rollback

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if preview is not UNSET:
            field_dict["preview"] = preview
        if digest is not UNSET:
            field_dict["digest"] = digest
        if plan is not UNSET:
            field_dict["plan"] = plan
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if already_applied is not UNSET:
            field_dict["already_applied"] = already_applied
        if binding is not UNSET:
            field_dict["binding"] = binding
        if recovery_manifest is not UNSET:
            field_dict["recovery_manifest"] = recovery_manifest
        if rollback is not UNSET:
            field_dict["rollback"] = rollback
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.project_relocate_response_binding_type_0 import ProjectRelocateResponseBindingType0
        from ..models.project_relocate_response_plan_type_0 import ProjectRelocateResponsePlanType0

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_preview(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        preview = _parse_preview(d.pop("preview", UNSET))

        def _parse_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        digest = _parse_digest(d.pop("digest", UNSET))

        def _parse_plan(data: object) -> None | ProjectRelocateResponsePlanType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                plan_type_0 = ProjectRelocateResponsePlanType0.from_dict(data)

                return plan_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProjectRelocateResponsePlanType0 | Unset, data)

        plan = _parse_plan(d.pop("plan", UNSET))

        blockers = cast(list[str], d.pop("blockers", UNSET))

        already_applied = d.pop("already_applied", UNSET)

        def _parse_binding(data: object) -> None | ProjectRelocateResponseBindingType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                binding_type_0 = ProjectRelocateResponseBindingType0.from_dict(data)

                return binding_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProjectRelocateResponseBindingType0 | Unset, data)

        binding = _parse_binding(d.pop("binding", UNSET))

        def _parse_recovery_manifest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recovery_manifest = _parse_recovery_manifest(d.pop("recovery_manifest", UNSET))

        def _parse_rollback(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rollback = _parse_rollback(d.pop("rollback", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        project_relocate_response = cls(
            success=success,
            preview=preview,
            digest=digest,
            plan=plan,
            blockers=blockers,
            already_applied=already_applied,
            binding=binding,
            recovery_manifest=recovery_manifest,
            rollback=rollback,
            error_code=error_code,
            error=error,
        )

        project_relocate_response.additional_properties = d
        return project_relocate_response

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
