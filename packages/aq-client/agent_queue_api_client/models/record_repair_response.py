from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.record_repair_response_batches_type_0_item import RecordRepairResponseBatchesType0Item
    from ..models.record_repair_response_inventory_type_0 import RecordRepairResponseInventoryType0


T = TypeVar("T", bound="RecordRepairResponse")


@_attrs_define
class RecordRepairResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        record_id (None | str | Unset):
        replay (bool | Unset):  Default: False.
        dry_run (bool | Unset):  Default: True.
        inventory (None | RecordRepairResponseInventoryType0 | Unset):
        batches (list[RecordRepairResponseBatchesType0Item] | None | Unset):
        done (bool | None | Unset):
        eligible (bool | None | Unset):
    """

    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    record_id: None | str | Unset = UNSET
    replay: bool | Unset = False
    dry_run: bool | Unset = True
    inventory: None | RecordRepairResponseInventoryType0 | Unset = UNSET
    batches: list[RecordRepairResponseBatchesType0Item] | None | Unset = UNSET
    done: bool | None | Unset = UNSET
    eligible: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.record_repair_response_inventory_type_0 import RecordRepairResponseInventoryType0

        success = self.success

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        record_id: None | str | Unset
        if isinstance(self.record_id, Unset):
            record_id = UNSET
        else:
            record_id = self.record_id

        replay = self.replay

        dry_run = self.dry_run

        inventory: dict[str, Any] | None | Unset
        if isinstance(self.inventory, Unset):
            inventory = UNSET
        elif isinstance(self.inventory, RecordRepairResponseInventoryType0):
            inventory = self.inventory.to_dict()
        else:
            inventory = self.inventory

        batches: list[dict[str, Any]] | None | Unset
        if isinstance(self.batches, Unset):
            batches = UNSET
        elif isinstance(self.batches, list):
            batches = []
            for batches_type_0_item_data in self.batches:
                batches_type_0_item = batches_type_0_item_data.to_dict()
                batches.append(batches_type_0_item)

        else:
            batches = self.batches

        done: bool | None | Unset
        if isinstance(self.done, Unset):
            done = UNSET
        else:
            done = self.done

        eligible: bool | None | Unset
        if isinstance(self.eligible, Unset):
            eligible = UNSET
        else:
            eligible = self.eligible

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if record_id is not UNSET:
            field_dict["record_id"] = record_id
        if replay is not UNSET:
            field_dict["replay"] = replay
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if inventory is not UNSET:
            field_dict["inventory"] = inventory
        if batches is not UNSET:
            field_dict["batches"] = batches
        if done is not UNSET:
            field_dict["done"] = done
        if eligible is not UNSET:
            field_dict["eligible"] = eligible

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.record_repair_response_batches_type_0_item import RecordRepairResponseBatchesType0Item
        from ..models.record_repair_response_inventory_type_0 import RecordRepairResponseInventoryType0

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_record_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        record_id = _parse_record_id(d.pop("record_id", UNSET))

        replay = d.pop("replay", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        def _parse_inventory(data: object) -> None | RecordRepairResponseInventoryType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                inventory_type_0 = RecordRepairResponseInventoryType0.from_dict(data)

                return inventory_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecordRepairResponseInventoryType0 | Unset, data)

        inventory = _parse_inventory(d.pop("inventory", UNSET))

        def _parse_batches(data: object) -> list[RecordRepairResponseBatchesType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                batches_type_0 = []
                _batches_type_0 = data
                for batches_type_0_item_data in _batches_type_0:
                    batches_type_0_item = RecordRepairResponseBatchesType0Item.from_dict(batches_type_0_item_data)

                    batches_type_0.append(batches_type_0_item)

                return batches_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[RecordRepairResponseBatchesType0Item] | None | Unset, data)

        batches = _parse_batches(d.pop("batches", UNSET))

        def _parse_done(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        done = _parse_done(d.pop("done", UNSET))

        def _parse_eligible(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        eligible = _parse_eligible(d.pop("eligible", UNSET))

        record_repair_response = cls(
            success=success,
            outcome=outcome,
            record_id=record_id,
            replay=replay,
            dry_run=dry_run,
            inventory=inventory,
            batches=batches,
            done=done,
            eligible=eligible,
        )

        record_repair_response.additional_properties = d
        return record_repair_response

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
