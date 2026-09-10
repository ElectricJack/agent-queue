from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.dashboard_state_put_request_namespace import DashboardStatePutRequestNamespace
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.dashboard_state_put_request_value import DashboardStatePutRequestValue


T = TypeVar("T", bound="DashboardStatePutRequest")


@_attrs_define
class DashboardStatePutRequest:
    """Flat fallback keeps command-level validation/error envelopes intact.

    Response documents still carry the discriminated value union, so both
    generated clients expose every namespace value type without FastAPI
    pre-empting ``invalid_value`` and ``unknown_namespace`` with its generic
    request-validation response.

        Attributes:
            namespace (DashboardStatePutRequestNamespace):
            value (DashboardStatePutRequestValue):
            subject (None | str | Unset):
            base_revision (int | None | Unset):
    """

    namespace: DashboardStatePutRequestNamespace
    value: DashboardStatePutRequestValue
    subject: None | str | Unset = UNSET
    base_revision: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        namespace = self.namespace.value

        value = self.value.to_dict()

        subject: None | str | Unset
        if isinstance(self.subject, Unset):
            subject = UNSET
        else:
            subject = self.subject

        base_revision: int | None | Unset
        if isinstance(self.base_revision, Unset):
            base_revision = UNSET
        else:
            base_revision = self.base_revision

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "namespace": namespace,
                "value": value,
            }
        )
        if subject is not UNSET:
            field_dict["subject"] = subject
        if base_revision is not UNSET:
            field_dict["base_revision"] = base_revision

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.dashboard_state_put_request_value import DashboardStatePutRequestValue

        d = dict(src_dict)
        namespace = DashboardStatePutRequestNamespace(d.pop("namespace"))

        value = DashboardStatePutRequestValue.from_dict(d.pop("value"))

        def _parse_subject(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        subject = _parse_subject(d.pop("subject", UNSET))

        def _parse_base_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        base_revision = _parse_base_revision(d.pop("base_revision", UNSET))

        dashboard_state_put_request = cls(
            namespace=namespace,
            value=value,
            subject=subject,
            base_revision=base_revision,
        )

        return dashboard_state_put_request
