from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.layout_job import LayoutJob


T = TypeVar("T", bound="ExtentResponse")


@_attrs_define
class ExtentResponse:
    """
    Attributes:
        layout_version (int):
        extent_w (float):
        extent_h (float):
        node_count (int):
        job (LayoutJob | None | Unset):
    """

    layout_version: int
    extent_w: float
    extent_h: float
    node_count: int
    job: LayoutJob | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.layout_job import LayoutJob

        layout_version = self.layout_version

        extent_w = self.extent_w

        extent_h = self.extent_h

        node_count = self.node_count

        job: dict[str, Any] | None | Unset
        if isinstance(self.job, Unset):
            job = UNSET
        elif isinstance(self.job, LayoutJob):
            job = self.job.to_dict()
        else:
            job = self.job

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "layout_version": layout_version,
                "extent_w": extent_w,
                "extent_h": extent_h,
                "node_count": node_count,
            }
        )
        if job is not UNSET:
            field_dict["job"] = job

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.layout_job import LayoutJob

        d = dict(src_dict)
        layout_version = d.pop("layout_version")

        extent_w = d.pop("extent_w")

        extent_h = d.pop("extent_h")

        node_count = d.pop("node_count")

        def _parse_job(data: object) -> LayoutJob | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                job_type_0 = LayoutJob.from_dict(data)

                return job_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LayoutJob | None | Unset, data)

        job = _parse_job(d.pop("job", UNSET))

        extent_response = cls(
            layout_version=layout_version,
            extent_w=extent_w,
            extent_h=extent_h,
            node_count=node_count,
            job=job,
        )

        extent_response.additional_properties = d
        return extent_response

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
