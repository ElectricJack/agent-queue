from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.dashboard_state_get_request_namespace import DashboardStateGetRequestNamespace
from ..types import UNSET, Unset

T = TypeVar("T", bound="DashboardStateGetRequest")


@_attrs_define
class DashboardStateGetRequest:
    """
    Attributes:
        namespace (DashboardStateGetRequestNamespace):
        subject (None | str | Unset):
    """

    namespace: DashboardStateGetRequestNamespace
    subject: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        namespace = self.namespace.value

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "namespace": namespace,
            }
        )
        if subject is not UNSET:
            field_dict["subject"] = subject

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        namespace = DashboardStateGetRequestNamespace(d.pop("namespace"))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        dashboard_state_get_request = cls(
            namespace=namespace,
            subject=subject,
        )

        return dashboard_state_get_request
