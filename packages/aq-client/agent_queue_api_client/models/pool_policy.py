from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pool_settings import PoolSettings


T = TypeVar("T", bound="PoolPolicy")


@_attrs_define
class PoolPolicy:
    """
    Attributes:
        settings (PoolSettings):
        type_ (Literal['pool'] | Unset):  Default: 'pool'.
    """

    settings: PoolSettings
    type_: Literal["pool"] | Unset = "pool"

    def to_dict(self) -> dict[str, Any]:
        settings = self.settings.to_dict()

        type_ = self.type_

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "settings": settings,
            }
        )
        if type_ is not UNSET:
            field_dict["type"] = type_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pool_settings import PoolSettings

        d = dict(src_dict)
        settings = PoolSettings.from_dict(d.pop("settings"))

        type_ = cast(Literal["pool"] | Unset, d.pop("type", UNSET))
        if type_ != "pool" and not isinstance(type_, Unset):
            raise ValueError(f"type must match const 'pool', got '{type_}'")

        pool_policy = cls(
            settings=settings,
            type_=type_,
        )

        return pool_policy
