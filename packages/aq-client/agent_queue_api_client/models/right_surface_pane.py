from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.right_surface_pane_args import RightSurfacePaneArgs


T = TypeVar("T", bound="RightSurfacePane")


@_attrs_define
class RightSurfacePane:
    """
    Attributes:
        view (str):
        args (RightSurfacePaneArgs | Unset):
    """

    view: str
    args: RightSurfacePaneArgs | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        view = self.view

        args: dict[str, Any] | Unset = UNSET
        if not isinstance(self.args, Unset):
            args = self.args.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "view": view,
            }
        )
        if args is not UNSET:
            field_dict["args"] = args

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.right_surface_pane_args import RightSurfacePaneArgs

        d = dict(src_dict)
        view = d.pop("view")

        _args = d.pop("args", UNSET)
        args: RightSurfacePaneArgs | Unset
        if isinstance(_args, Unset):
            args = UNSET
        else:
            args = RightSurfacePaneArgs.from_dict(_args)

        right_surface_pane = cls(
            view=view,
            args=args,
        )

        return right_surface_pane
