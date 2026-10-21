from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.script_node_detail_dto_input_pins_item import ScriptNodeDetailDTOInputPinsItem
    from ..models.script_node_detail_dto_program import ScriptNodeDetailDTOProgram


T = TypeVar("T", bound="ScriptNodeDetailDTO")


@_attrs_define
class ScriptNodeDetailDTO:
    """
    Attributes:
        program (ScriptNodeDetailDTOProgram):
        source_revision (None | str | Unset):
        program_sha256 (None | str | Unset):
        runtime_sha256 (None | str | Unset):
        executable_sha256 (None | str | Unset):
        input_pins (list[ScriptNodeDetailDTOInputPinsItem] | Unset):
        environment_names (list[str] | Unset):
    """

    program: ScriptNodeDetailDTOProgram
    source_revision: None | str | Unset = UNSET
    program_sha256: None | str | Unset = UNSET
    runtime_sha256: None | str | Unset = UNSET
    executable_sha256: None | str | Unset = UNSET
    input_pins: list[ScriptNodeDetailDTOInputPinsItem] | Unset = UNSET
    environment_names: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        program = self.program.to_dict()

        source_revision: None | str | Unset
        if isinstance(self.source_revision, Unset):
            source_revision = UNSET
        else:
            source_revision = self.source_revision

        program_sha256: None | str | Unset
        if isinstance(self.program_sha256, Unset):
            program_sha256 = UNSET
        else:
            program_sha256 = self.program_sha256

        runtime_sha256: None | str | Unset
        if isinstance(self.runtime_sha256, Unset):
            runtime_sha256 = UNSET
        else:
            runtime_sha256 = self.runtime_sha256

        executable_sha256: None | str | Unset
        if isinstance(self.executable_sha256, Unset):
            executable_sha256 = UNSET
        else:
            executable_sha256 = self.executable_sha256

        input_pins: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.input_pins, Unset):
            input_pins = []
            for input_pins_item_data in self.input_pins:
                input_pins_item = input_pins_item_data.to_dict()
                input_pins.append(input_pins_item)

        environment_names: list[str] | Unset = UNSET
        if not isinstance(self.environment_names, Unset):
            environment_names = self.environment_names

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "program": program,
            }
        )
        if source_revision is not UNSET:
            field_dict["source_revision"] = source_revision
        if program_sha256 is not UNSET:
            field_dict["program_sha256"] = program_sha256
        if runtime_sha256 is not UNSET:
            field_dict["runtime_sha256"] = runtime_sha256
        if executable_sha256 is not UNSET:
            field_dict["executable_sha256"] = executable_sha256
        if input_pins is not UNSET:
            field_dict["input_pins"] = input_pins
        if environment_names is not UNSET:
            field_dict["environment_names"] = environment_names

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.script_node_detail_dto_input_pins_item import ScriptNodeDetailDTOInputPinsItem
        from ..models.script_node_detail_dto_program import ScriptNodeDetailDTOProgram

        d = dict(src_dict)
        program = ScriptNodeDetailDTOProgram.from_dict(d.pop("program"))

        def _parse_source_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_revision = _parse_source_revision(d.pop("source_revision", UNSET))

        def _parse_program_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        program_sha256 = _parse_program_sha256(d.pop("program_sha256", UNSET))

        def _parse_runtime_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        runtime_sha256 = _parse_runtime_sha256(d.pop("runtime_sha256", UNSET))

        def _parse_executable_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        executable_sha256 = _parse_executable_sha256(d.pop("executable_sha256", UNSET))

        _input_pins = d.pop("input_pins", UNSET)
        input_pins: list[ScriptNodeDetailDTOInputPinsItem] | Unset = UNSET
        if _input_pins is not UNSET:
            input_pins = []
            for input_pins_item_data in _input_pins:
                input_pins_item = ScriptNodeDetailDTOInputPinsItem.from_dict(input_pins_item_data)

                input_pins.append(input_pins_item)

        environment_names = cast(list[str], d.pop("environment_names", UNSET))

        script_node_detail_dto = cls(
            program=program,
            source_revision=source_revision,
            program_sha256=program_sha256,
            runtime_sha256=runtime_sha256,
            executable_sha256=executable_sha256,
            input_pins=input_pins,
            environment_names=environment_names,
        )

        return script_node_detail_dto
