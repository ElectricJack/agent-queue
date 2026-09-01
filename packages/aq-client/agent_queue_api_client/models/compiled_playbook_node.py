from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.compiled_playbook_node_action_type_0 import CompiledPlaybookNodeActionType0
    from ..models.compiled_playbook_node_for_each_type_0 import CompiledPlaybookNodeForEachType0
    from ..models.compiled_playbook_node_output_type_0 import CompiledPlaybookNodeOutputType0
    from ..models.playbook_node_llm_config import PlaybookNodeLlmConfig
    from ..models.playbook_transition_detail import PlaybookTransitionDetail


T = TypeVar("T", bound="CompiledPlaybookNode")


@_attrs_define
class CompiledPlaybookNode:
    """The serializable fields produced by ``PlaybookNode.to_dict()``.

    Each field is optional according to the compiled-node rules: a key is
    present only when the compiler set it.  This is what the dashboard node
    inspector renders, so the prompt here is the full untruncated text.

        Attributes:
            prompt (None | str | Unset):
            entry (bool | None | Unset):
            terminal (bool | None | Unset):
            transitions (list[PlaybookTransitionDetail] | None | Unset):
            goto (None | str | Unset):
            wait_for_human (bool | None | Unset):
            timeout_seconds (int | None | Unset):
            pause_timeout_seconds (int | None | Unset):
            on_timeout (None | str | Unset):
            llm_config (None | PlaybookNodeLlmConfig | Unset):
            transition_llm_config (None | PlaybookNodeLlmConfig | Unset):
            for_each (CompiledPlaybookNodeForEachType0 | None | Unset):
            output (CompiledPlaybookNodeOutputType0 | None | Unset):
            action (CompiledPlaybookNodeActionType0 | None | Unset):
    """

    prompt: None | str | Unset = UNSET
    entry: bool | None | Unset = UNSET
    terminal: bool | None | Unset = UNSET
    transitions: list[PlaybookTransitionDetail] | None | Unset = UNSET
    goto: None | str | Unset = UNSET
    wait_for_human: bool | None | Unset = UNSET
    timeout_seconds: int | None | Unset = UNSET
    pause_timeout_seconds: int | None | Unset = UNSET
    on_timeout: None | str | Unset = UNSET
    llm_config: None | PlaybookNodeLlmConfig | Unset = UNSET
    transition_llm_config: None | PlaybookNodeLlmConfig | Unset = UNSET
    for_each: CompiledPlaybookNodeForEachType0 | None | Unset = UNSET
    output: CompiledPlaybookNodeOutputType0 | None | Unset = UNSET
    action: CompiledPlaybookNodeActionType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.compiled_playbook_node_action_type_0 import CompiledPlaybookNodeActionType0  # noqa: PLC0415
        from ..models.compiled_playbook_node_for_each_type_0 import CompiledPlaybookNodeForEachType0  # noqa: PLC0415
        from ..models.compiled_playbook_node_output_type_0 import CompiledPlaybookNodeOutputType0  # noqa: PLC0415
        from ..models.playbook_node_llm_config import PlaybookNodeLlmConfig  # noqa: PLC0415

        prompt: None | str | Unset
        if isinstance(self.prompt, Unset):
            prompt = UNSET
        else:
            prompt = self.prompt

        entry: bool | None | Unset
        if isinstance(self.entry, Unset):
            entry = UNSET
        else:
            entry = self.entry

        terminal: bool | None | Unset
        if isinstance(self.terminal, Unset):
            terminal = UNSET
        else:
            terminal = self.terminal

        transitions: list[dict[str, Any]] | None | Unset
        if isinstance(self.transitions, Unset):
            transitions = UNSET
        elif isinstance(self.transitions, list):
            transitions = []
            for transitions_type_0_item_data in self.transitions:
                transitions_type_0_item = transitions_type_0_item_data.to_dict()
                transitions.append(transitions_type_0_item)

        else:
            transitions = self.transitions

        goto: None | str | Unset
        if isinstance(self.goto, Unset):
            goto = UNSET
        else:
            goto = self.goto

        wait_for_human: bool | None | Unset
        if isinstance(self.wait_for_human, Unset):
            wait_for_human = UNSET
        else:
            wait_for_human = self.wait_for_human

        timeout_seconds: int | None | Unset
        if isinstance(self.timeout_seconds, Unset):
            timeout_seconds = UNSET
        else:
            timeout_seconds = self.timeout_seconds

        pause_timeout_seconds: int | None | Unset
        if isinstance(self.pause_timeout_seconds, Unset):
            pause_timeout_seconds = UNSET
        else:
            pause_timeout_seconds = self.pause_timeout_seconds

        on_timeout: None | str | Unset
        if isinstance(self.on_timeout, Unset):
            on_timeout = UNSET
        else:
            on_timeout = self.on_timeout

        llm_config: dict[str, Any] | None | Unset
        if isinstance(self.llm_config, Unset):
            llm_config = UNSET
        elif isinstance(self.llm_config, PlaybookNodeLlmConfig):
            llm_config = self.llm_config.to_dict()
        else:
            llm_config = self.llm_config

        transition_llm_config: dict[str, Any] | None | Unset
        if isinstance(self.transition_llm_config, Unset):
            transition_llm_config = UNSET
        elif isinstance(self.transition_llm_config, PlaybookNodeLlmConfig):
            transition_llm_config = self.transition_llm_config.to_dict()
        else:
            transition_llm_config = self.transition_llm_config

        for_each: dict[str, Any] | None | Unset
        if isinstance(self.for_each, Unset):
            for_each = UNSET
        elif isinstance(self.for_each, CompiledPlaybookNodeForEachType0):
            for_each = self.for_each.to_dict()
        else:
            for_each = self.for_each

        output: dict[str, Any] | None | Unset
        if isinstance(self.output, Unset):
            output = UNSET
        elif isinstance(self.output, CompiledPlaybookNodeOutputType0):
            output = self.output.to_dict()
        else:
            output = self.output

        action: dict[str, Any] | None | Unset
        if isinstance(self.action, Unset):
            action = UNSET
        elif isinstance(self.action, CompiledPlaybookNodeActionType0):
            action = self.action.to_dict()
        else:
            action = self.action

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if prompt is not UNSET:
            field_dict["prompt"] = prompt
        if entry is not UNSET:
            field_dict["entry"] = entry
        if terminal is not UNSET:
            field_dict["terminal"] = terminal
        if transitions is not UNSET:
            field_dict["transitions"] = transitions
        if goto is not UNSET:
            field_dict["goto"] = goto
        if wait_for_human is not UNSET:
            field_dict["wait_for_human"] = wait_for_human
        if timeout_seconds is not UNSET:
            field_dict["timeout_seconds"] = timeout_seconds
        if pause_timeout_seconds is not UNSET:
            field_dict["pause_timeout_seconds"] = pause_timeout_seconds
        if on_timeout is not UNSET:
            field_dict["on_timeout"] = on_timeout
        if llm_config is not UNSET:
            field_dict["llm_config"] = llm_config
        if transition_llm_config is not UNSET:
            field_dict["transition_llm_config"] = transition_llm_config
        if for_each is not UNSET:
            field_dict["for_each"] = for_each
        if output is not UNSET:
            field_dict["output"] = output
        if action is not UNSET:
            field_dict["action"] = action

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiled_playbook_node_action_type_0 import CompiledPlaybookNodeActionType0  # noqa: PLC0415
        from ..models.compiled_playbook_node_for_each_type_0 import CompiledPlaybookNodeForEachType0  # noqa: PLC0415
        from ..models.compiled_playbook_node_output_type_0 import CompiledPlaybookNodeOutputType0  # noqa: PLC0415
        from ..models.playbook_node_llm_config import PlaybookNodeLlmConfig  # noqa: PLC0415
        from ..models.playbook_transition_detail import PlaybookTransitionDetail  # noqa: PLC0415

        d = dict(src_dict)

        def _parse_prompt(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prompt = _parse_prompt(d.pop("prompt", UNSET))

        def _parse_entry(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        entry = _parse_entry(d.pop("entry", UNSET))

        def _parse_terminal(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        terminal = _parse_terminal(d.pop("terminal", UNSET))

        def _parse_transitions(data: object) -> list[PlaybookTransitionDetail] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                transitions_type_0 = []
                _transitions_type_0 = data
                for transitions_type_0_item_data in _transitions_type_0:
                    transitions_type_0_item = PlaybookTransitionDetail.from_dict(transitions_type_0_item_data)

                    transitions_type_0.append(transitions_type_0_item)

                return transitions_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[PlaybookTransitionDetail] | None | Unset, data)

        transitions = _parse_transitions(d.pop("transitions", UNSET))

        def _parse_goto(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        goto = _parse_goto(d.pop("goto", UNSET))

        def _parse_wait_for_human(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        wait_for_human = _parse_wait_for_human(d.pop("wait_for_human", UNSET))

        def _parse_timeout_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        timeout_seconds = _parse_timeout_seconds(d.pop("timeout_seconds", UNSET))

        def _parse_pause_timeout_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        pause_timeout_seconds = _parse_pause_timeout_seconds(d.pop("pause_timeout_seconds", UNSET))

        def _parse_on_timeout(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        on_timeout = _parse_on_timeout(d.pop("on_timeout", UNSET))

        def _parse_llm_config(data: object) -> None | PlaybookNodeLlmConfig | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                llm_config_type_0 = PlaybookNodeLlmConfig.from_dict(data)

                return llm_config_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookNodeLlmConfig | Unset, data)

        llm_config = _parse_llm_config(d.pop("llm_config", UNSET))

        def _parse_transition_llm_config(data: object) -> None | PlaybookNodeLlmConfig | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                transition_llm_config_type_0 = PlaybookNodeLlmConfig.from_dict(data)

                return transition_llm_config_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookNodeLlmConfig | Unset, data)

        transition_llm_config = _parse_transition_llm_config(d.pop("transition_llm_config", UNSET))

        def _parse_for_each(data: object) -> CompiledPlaybookNodeForEachType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                for_each_type_0 = CompiledPlaybookNodeForEachType0.from_dict(data)

                return for_each_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledPlaybookNodeForEachType0 | None | Unset, data)

        for_each = _parse_for_each(d.pop("for_each", UNSET))

        def _parse_output(data: object) -> CompiledPlaybookNodeOutputType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                output_type_0 = CompiledPlaybookNodeOutputType0.from_dict(data)

                return output_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledPlaybookNodeOutputType0 | None | Unset, data)

        output = _parse_output(d.pop("output", UNSET))

        def _parse_action(data: object) -> CompiledPlaybookNodeActionType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                action_type_0 = CompiledPlaybookNodeActionType0.from_dict(data)

                return action_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CompiledPlaybookNodeActionType0 | None | Unset, data)

        action = _parse_action(d.pop("action", UNSET))

        compiled_playbook_node = cls(
            prompt=prompt,
            entry=entry,
            terminal=terminal,
            transitions=transitions,
            goto=goto,
            wait_for_human=wait_for_human,
            timeout_seconds=timeout_seconds,
            pause_timeout_seconds=pause_timeout_seconds,
            on_timeout=on_timeout,
            llm_config=llm_config,
            transition_llm_config=transition_llm_config,
            for_each=for_each,
            output=output,
            action=action,
        )

        compiled_playbook_node.additional_properties = d
        return compiled_playbook_node

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
