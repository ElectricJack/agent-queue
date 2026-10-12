from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="PoolSettings")


@_attrs_define
class PoolSettings:
    """
    Attributes:
        name (str | Unset):  Default: ''.
        enabled (bool | Unset):  Default: True.
        min_active (int | None | Unset):
        max_active (int | None | Unset):
        min_per_project (int | None | Unset):
        max_claims_per_session (int | None | Unset):
        derived (bool | Unset):  Default: False.
        extends (str | Unset):  Default: ''.
        harness (None | str | Unset):
        default_class (str | Unset):  Default: ''.
        lifecycle (str | Unset):  Default: 'task'.
    """

    name: str | Unset = ""
    enabled: bool | Unset = True
    min_active: int | None | Unset = UNSET
    max_active: int | None | Unset = UNSET
    min_per_project: int | None | Unset = UNSET
    max_claims_per_session: int | None | Unset = UNSET
    derived: bool | Unset = False
    extends: str | Unset = ""
    harness: None | str | Unset = UNSET
    default_class: str | Unset = ""
    lifecycle: str | Unset = "task"

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        enabled = self.enabled

        min_active: int | None | Unset
        if isinstance(self.min_active, Unset):
            min_active = UNSET
        else:
            min_active = self.min_active

        max_active: int | None | Unset
        if isinstance(self.max_active, Unset):
            max_active = UNSET
        else:
            max_active = self.max_active

        min_per_project: int | None | Unset
        if isinstance(self.min_per_project, Unset):
            min_per_project = UNSET
        else:
            min_per_project = self.min_per_project

        max_claims_per_session: int | None | Unset
        if isinstance(self.max_claims_per_session, Unset):
            max_claims_per_session = UNSET
        else:
            max_claims_per_session = self.max_claims_per_session

        derived = self.derived

        extends = self.extends

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        default_class = self.default_class

        lifecycle = self.lifecycle

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if min_active is not UNSET:
            field_dict["min_active"] = min_active
        if max_active is not UNSET:
            field_dict["max_active"] = max_active
        if min_per_project is not UNSET:
            field_dict["min_per_project"] = min_per_project
        if max_claims_per_session is not UNSET:
            field_dict["max_claims_per_session"] = max_claims_per_session
        if derived is not UNSET:
            field_dict["derived"] = derived
        if extends is not UNSET:
            field_dict["extends"] = extends
        if harness is not UNSET:
            field_dict["harness"] = harness
        if default_class is not UNSET:
            field_dict["default_class"] = default_class
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name", UNSET)

        enabled = d.pop("enabled", UNSET)

        def _parse_min_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_active = _parse_min_active(d.pop("min_active", UNSET))

        def _parse_max_active(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_active = _parse_max_active(d.pop("max_active", UNSET))

        def _parse_min_per_project(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        min_per_project = _parse_min_per_project(d.pop("min_per_project", UNSET))

        def _parse_max_claims_per_session(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_claims_per_session = _parse_max_claims_per_session(d.pop("max_claims_per_session", UNSET))

        derived = d.pop("derived", UNSET)

        extends = d.pop("extends", UNSET)

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        default_class = d.pop("default_class", UNSET)

        lifecycle = d.pop("lifecycle", UNSET)

        pool_settings = cls(
            name=name,
            enabled=enabled,
            min_active=min_active,
            max_active=max_active,
            min_per_project=min_per_project,
            max_claims_per_session=max_claims_per_session,
            derived=derived,
            extends=extends,
            harness=harness,
            default_class=default_class,
            lifecycle=lifecycle,
        )

        return pool_settings
