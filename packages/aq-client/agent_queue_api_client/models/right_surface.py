from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.right_surface_activity_tab import RightSurfaceActivityTab
from ..models.right_surface_kind_type_0 import RightSurfaceKindType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.right_surface_pane import RightSurfacePane


T = TypeVar("T", bound="RightSurface")


@_attrs_define
class RightSurface:
    """
    Attributes:
        width (int | Unset):  Default: 480.
        kind (None | RightSurfaceKindType0 | Unset):
        activity_tab (RightSurfaceActivityTab | Unset):  Default: RightSurfaceActivityTab.GATES.
        pane (None | RightSurfacePane | Unset):
    """

    width: int | Unset = 480
    kind: None | RightSurfaceKindType0 | Unset = UNSET
    activity_tab: RightSurfaceActivityTab | Unset = RightSurfaceActivityTab.GATES
    pane: None | RightSurfacePane | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.right_surface_pane import RightSurfacePane

        width = self.width

        kind: None | str | Unset
        if isinstance(self.kind, Unset):
            kind = UNSET
        elif isinstance(self.kind, RightSurfaceKindType0):
            kind = self.kind.value
        else:
            kind = self.kind

        activity_tab: str | Unset = UNSET
        if not isinstance(self.activity_tab, Unset):
            activity_tab = self.activity_tab.value

        pane: dict[str, Any] | None | Unset
        if isinstance(self.pane, Unset):
            pane = UNSET
        elif isinstance(self.pane, RightSurfacePane):
            pane = self.pane.to_dict()
        else:
            pane = self.pane

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if width is not UNSET:
            field_dict["width"] = width
        if kind is not UNSET:
            field_dict["kind"] = kind
        if activity_tab is not UNSET:
            field_dict["activity_tab"] = activity_tab
        if pane is not UNSET:
            field_dict["pane"] = pane

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.right_surface_pane import RightSurfacePane

        d = dict(src_dict)
        width = d.pop("width", UNSET)

        def _parse_kind(data: object) -> None | RightSurfaceKindType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                kind_type_0 = RightSurfaceKindType0(data)

                return kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RightSurfaceKindType0 | Unset, data)

        kind = _parse_kind(d.pop("kind", UNSET))

        _activity_tab = d.pop("activity_tab", UNSET)
        activity_tab: RightSurfaceActivityTab | Unset
        if isinstance(_activity_tab, Unset):
            activity_tab = UNSET
        else:
            activity_tab = RightSurfaceActivityTab(_activity_tab)

        def _parse_pane(data: object) -> None | RightSurfacePane | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                pane_type_0 = RightSurfacePane.from_dict(data)

                return pane_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RightSurfacePane | Unset, data)

        pane = _parse_pane(d.pop("pane", UNSET))

        right_surface = cls(
            width=width,
            kind=kind,
            activity_tab=activity_tab,
            pane=pane,
        )

        return right_surface
