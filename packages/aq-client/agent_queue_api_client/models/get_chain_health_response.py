from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_chain_health_response_stuck_chains_type_0_item import GetChainHealthResponseStuckChainsType0Item
    from ..models.get_chain_health_response_stuck_downstream_type_0_item import (
        GetChainHealthResponseStuckDownstreamType0Item,
    )


T = TypeVar("T", bound="GetChainHealthResponse")


@_attrs_define
class GetChainHealthResponse:
    """
    Attributes:
        task_id (None | str | Unset):
        project_id (None | str | Unset):
        status (None | str | Unset):
        title (None | str | Unset):
        stuck_downstream (list[GetChainHealthResponseStuckDownstreamType0Item] | None | Unset):
        stuck_count (int | None | Unset):
        stuck_chains (list[GetChainHealthResponseStuckChainsType0Item] | None | Unset):
        total_stuck_chains (int | None | Unset):
        message (None | str | Unset):
    """

    task_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    status: None | str | Unset = UNSET
    title: None | str | Unset = UNSET
    stuck_downstream: list[GetChainHealthResponseStuckDownstreamType0Item] | None | Unset = UNSET
    stuck_count: int | None | Unset = UNSET
    stuck_chains: list[GetChainHealthResponseStuckChainsType0Item] | None | Unset = UNSET
    total_stuck_chains: int | None | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        stuck_downstream: list[dict[str, Any]] | None | Unset
        if isinstance(self.stuck_downstream, Unset):
            stuck_downstream = UNSET
        elif isinstance(self.stuck_downstream, list):
            stuck_downstream = []
            for stuck_downstream_type_0_item_data in self.stuck_downstream:
                stuck_downstream_type_0_item = stuck_downstream_type_0_item_data.to_dict()
                stuck_downstream.append(stuck_downstream_type_0_item)

        else:
            stuck_downstream = self.stuck_downstream

        stuck_count: int | None | Unset
        if isinstance(self.stuck_count, Unset):
            stuck_count = UNSET
        else:
            stuck_count = self.stuck_count

        stuck_chains: list[dict[str, Any]] | None | Unset
        if isinstance(self.stuck_chains, Unset):
            stuck_chains = UNSET
        elif isinstance(self.stuck_chains, list):
            stuck_chains = []
            for stuck_chains_type_0_item_data in self.stuck_chains:
                stuck_chains_type_0_item = stuck_chains_type_0_item_data.to_dict()
                stuck_chains.append(stuck_chains_type_0_item)

        else:
            stuck_chains = self.stuck_chains

        total_stuck_chains: int | None | Unset
        if isinstance(self.total_stuck_chains, Unset):
            total_stuck_chains = UNSET
        else:
            total_stuck_chains = self.total_stuck_chains

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if status is not UNSET:
            field_dict["status"] = status
        if title is not UNSET:
            field_dict["title"] = title
        if stuck_downstream is not UNSET:
            field_dict["stuck_downstream"] = stuck_downstream
        if stuck_count is not UNSET:
            field_dict["stuck_count"] = stuck_count
        if stuck_chains is not UNSET:
            field_dict["stuck_chains"] = stuck_chains
        if total_stuck_chains is not UNSET:
            field_dict["total_stuck_chains"] = total_stuck_chains
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_chain_health_response_stuck_chains_type_0_item import (
            GetChainHealthResponseStuckChainsType0Item,
        )
        from ..models.get_chain_health_response_stuck_downstream_type_0_item import (
            GetChainHealthResponseStuckDownstreamType0Item,
        )

        d = dict(src_dict)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_stuck_downstream(
            data: object,
        ) -> list[GetChainHealthResponseStuckDownstreamType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                stuck_downstream_type_0 = []
                _stuck_downstream_type_0 = data
                for stuck_downstream_type_0_item_data in _stuck_downstream_type_0:
                    stuck_downstream_type_0_item = GetChainHealthResponseStuckDownstreamType0Item.from_dict(
                        stuck_downstream_type_0_item_data
                    )

                    stuck_downstream_type_0.append(stuck_downstream_type_0_item)

                return stuck_downstream_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[GetChainHealthResponseStuckDownstreamType0Item] | None | Unset, data)

        stuck_downstream = _parse_stuck_downstream(d.pop("stuck_downstream", UNSET))

        def _parse_stuck_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        stuck_count = _parse_stuck_count(d.pop("stuck_count", UNSET))

        def _parse_stuck_chains(data: object) -> list[GetChainHealthResponseStuckChainsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                stuck_chains_type_0 = []
                _stuck_chains_type_0 = data
                for stuck_chains_type_0_item_data in _stuck_chains_type_0:
                    stuck_chains_type_0_item = GetChainHealthResponseStuckChainsType0Item.from_dict(
                        stuck_chains_type_0_item_data
                    )

                    stuck_chains_type_0.append(stuck_chains_type_0_item)

                return stuck_chains_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[GetChainHealthResponseStuckChainsType0Item] | None | Unset, data)

        stuck_chains = _parse_stuck_chains(d.pop("stuck_chains", UNSET))

        def _parse_total_stuck_chains(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        total_stuck_chains = _parse_total_stuck_chains(d.pop("total_stuck_chains", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        get_chain_health_response = cls(
            task_id=task_id,
            project_id=project_id,
            status=status,
            title=title,
            stuck_downstream=stuck_downstream,
            stuck_count=stuck_count,
            stuck_chains=stuck_chains,
            total_stuck_chains=total_stuck_chains,
            message=message,
        )

        get_chain_health_response.additional_properties = d
        return get_chain_health_response

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
