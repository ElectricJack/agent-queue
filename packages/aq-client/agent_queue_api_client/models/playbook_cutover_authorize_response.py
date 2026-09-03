from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_authorization_dto import CutoverAuthorizationDTO
    from ..models.cutover_event_dto import CutoverEventDTO


T = TypeVar("T", bound="PlaybookCutoverAuthorizeResponse")


@_attrs_define
class PlaybookCutoverAuthorizeResponse:
    """
    Attributes:
        success (bool):
        event (CutoverEventDTO | None | Unset):
        drain_signoff_event_id (None | str | Unset):
        authorizations (list[CutoverAuthorizationDTO] | Unset):
        blocking_reasons (list[str] | Unset):
        can_switch (bool | Unset):  Default: False.
        error (None | str | Unset):
    """

    success: bool
    event: CutoverEventDTO | None | Unset = UNSET
    drain_signoff_event_id: None | str | Unset = UNSET
    authorizations: list[CutoverAuthorizationDTO] | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
    can_switch: bool | Unset = False
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.cutover_event_dto import CutoverEventDTO

        success = self.success

        event: dict[str, Any] | None | Unset
        if isinstance(self.event, Unset):
            event = UNSET
        elif isinstance(self.event, CutoverEventDTO):
            event = self.event.to_dict()
        else:
            event = self.event

        drain_signoff_event_id: None | str | Unset
        if isinstance(self.drain_signoff_event_id, Unset):
            drain_signoff_event_id = UNSET
        else:
            drain_signoff_event_id = self.drain_signoff_event_id

        authorizations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.authorizations, Unset):
            authorizations = []
            for authorizations_item_data in self.authorizations:
                authorizations_item = authorizations_item_data.to_dict()
                authorizations.append(authorizations_item)

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        can_switch = self.can_switch

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
            }
        )
        if event is not UNSET:
            field_dict["event"] = event
        if drain_signoff_event_id is not UNSET:
            field_dict["drain_signoff_event_id"] = drain_signoff_event_id
        if authorizations is not UNSET:
            field_dict["authorizations"] = authorizations
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if can_switch is not UNSET:
            field_dict["can_switch"] = can_switch
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_authorization_dto import CutoverAuthorizationDTO
        from ..models.cutover_event_dto import CutoverEventDTO

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_event(data: object) -> CutoverEventDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                event_type_0 = CutoverEventDTO.from_dict(data)

                return event_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CutoverEventDTO | None | Unset, data)

        event = _parse_event(d.pop("event", UNSET))

        def _parse_drain_signoff_event_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        drain_signoff_event_id = _parse_drain_signoff_event_id(d.pop("drain_signoff_event_id", UNSET))

        _authorizations = d.pop("authorizations", UNSET)
        authorizations: list[CutoverAuthorizationDTO] | Unset = UNSET
        if _authorizations is not UNSET:
            authorizations = []
            for authorizations_item_data in _authorizations:
                authorizations_item = CutoverAuthorizationDTO.from_dict(authorizations_item_data)

                authorizations.append(authorizations_item)

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        can_switch = d.pop("can_switch", UNSET)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_cutover_authorize_response = cls(
            success=success,
            event=event,
            drain_signoff_event_id=drain_signoff_event_id,
            authorizations=authorizations,
            blocking_reasons=blocking_reasons,
            can_switch=can_switch,
            error=error,
        )

        return playbook_cutover_authorize_response
