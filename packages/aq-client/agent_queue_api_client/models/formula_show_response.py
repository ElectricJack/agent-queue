from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.formula_show_response_errors_item import FormulaShowResponseErrorsItem
    from ..models.formula_show_response_graph_type_0 import FormulaShowResponseGraphType0
    from ..models.formula_show_response_vars_type_0 import FormulaShowResponseVarsType0
    from ..models.formula_show_response_warnings_item import FormulaShowResponseWarningsItem


T = TypeVar("T", bound="FormulaShowResponse")


@_attrs_define
class FormulaShowResponse:
    """``formula_show``'s shape varies by outcome (same reasoning as
    ``create_task_graph``): a validation failure reports ``errors`` and the
    raw merged document, ``as_cooked`` omits ``chain``/``chain_sha`` details
    that only apply to a live registry resolution.  ``extra="allow"`` plus
    all-optional fields keeps the model honest about that without pinning
    down a shape narrower than what the command actually returns.

        Attributes:
            success (bool):
            error (None | str | Unset):
            name (None | str | Unset):
            scope (None | str | Unset):
            path (None | str | Unset):
            chain (list[str] | None | Unset):
            chain_sha (None | str | Unset):
            vars_ (FormulaShowResponseVarsType0 | None | Unset):
            graph (FormulaShowResponseGraphType0 | None | Unset):
            errors (list[FormulaShowResponseErrorsItem] | Unset):
            warnings (list[FormulaShowResponseWarningsItem] | Unset):
            as_cooked (None | str | Unset):
    """

    success: bool
    error: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    scope: None | str | Unset = UNSET
    path: None | str | Unset = UNSET
    chain: list[str] | None | Unset = UNSET
    chain_sha: None | str | Unset = UNSET
    vars_: FormulaShowResponseVarsType0 | None | Unset = UNSET
    graph: FormulaShowResponseGraphType0 | None | Unset = UNSET
    errors: list[FormulaShowResponseErrorsItem] | Unset = UNSET
    warnings: list[FormulaShowResponseWarningsItem] | Unset = UNSET
    as_cooked: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.formula_show_response_graph_type_0 import FormulaShowResponseGraphType0
        from ..models.formula_show_response_vars_type_0 import FormulaShowResponseVarsType0

        success = self.success

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        scope: None | str | Unset
        if isinstance(self.scope, Unset):
            scope = UNSET
        else:
            scope = self.scope

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        chain: list[str] | None | Unset
        if isinstance(self.chain, Unset):
            chain = UNSET
        elif isinstance(self.chain, list):
            chain = self.chain

        else:
            chain = self.chain

        chain_sha: None | str | Unset
        if isinstance(self.chain_sha, Unset):
            chain_sha = UNSET
        else:
            chain_sha = self.chain_sha

        vars_: dict[str, Any] | None | Unset
        if isinstance(self.vars_, Unset):
            vars_ = UNSET
        elif isinstance(self.vars_, FormulaShowResponseVarsType0):
            vars_ = self.vars_.to_dict()
        else:
            vars_ = self.vars_

        graph: dict[str, Any] | None | Unset
        if isinstance(self.graph, Unset):
            graph = UNSET
        elif isinstance(self.graph, FormulaShowResponseGraphType0):
            graph = self.graph.to_dict()
        else:
            graph = self.graph

        errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = []
            for errors_item_data in self.errors:
                errors_item = errors_item_data.to_dict()
                errors.append(errors_item)

        warnings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)

        as_cooked: None | str | Unset
        if isinstance(self.as_cooked, Unset):
            as_cooked = UNSET
        else:
            as_cooked = self.as_cooked

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if error is not UNSET:
            field_dict["error"] = error
        if name is not UNSET:
            field_dict["name"] = name
        if scope is not UNSET:
            field_dict["scope"] = scope
        if path is not UNSET:
            field_dict["path"] = path
        if chain is not UNSET:
            field_dict["chain"] = chain
        if chain_sha is not UNSET:
            field_dict["chain_sha"] = chain_sha
        if vars_ is not UNSET:
            field_dict["vars"] = vars_
        if graph is not UNSET:
            field_dict["graph"] = graph
        if errors is not UNSET:
            field_dict["errors"] = errors
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if as_cooked is not UNSET:
            field_dict["as_cooked"] = as_cooked

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.formula_show_response_errors_item import FormulaShowResponseErrorsItem
        from ..models.formula_show_response_graph_type_0 import FormulaShowResponseGraphType0
        from ..models.formula_show_response_vars_type_0 import FormulaShowResponseVarsType0
        from ..models.formula_show_response_warnings_item import FormulaShowResponseWarningsItem

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope = _parse_scope(d.pop("scope", UNSET))

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

        def _parse_chain(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                chain_type_0 = cast(list[str], data)

                return chain_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        chain = _parse_chain(d.pop("chain", UNSET))

        def _parse_chain_sha(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        chain_sha = _parse_chain_sha(d.pop("chain_sha", UNSET))

        def _parse_vars_(data: object) -> FormulaShowResponseVarsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                vars_type_0 = FormulaShowResponseVarsType0.from_dict(data)

                return vars_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FormulaShowResponseVarsType0 | None | Unset, data)

        vars_ = _parse_vars_(d.pop("vars", UNSET))

        def _parse_graph(data: object) -> FormulaShowResponseGraphType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                graph_type_0 = FormulaShowResponseGraphType0.from_dict(data)

                return graph_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FormulaShowResponseGraphType0 | None | Unset, data)

        graph = _parse_graph(d.pop("graph", UNSET))

        _errors = d.pop("errors", UNSET)
        errors: list[FormulaShowResponseErrorsItem] | Unset = UNSET
        if _errors is not UNSET:
            errors = []
            for errors_item_data in _errors:
                errors_item = FormulaShowResponseErrorsItem.from_dict(errors_item_data)

                errors.append(errors_item)

        _warnings = d.pop("warnings", UNSET)
        warnings: list[FormulaShowResponseWarningsItem] | Unset = UNSET
        if _warnings is not UNSET:
            warnings = []
            for warnings_item_data in _warnings:
                warnings_item = FormulaShowResponseWarningsItem.from_dict(warnings_item_data)

                warnings.append(warnings_item)

        def _parse_as_cooked(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        as_cooked = _parse_as_cooked(d.pop("as_cooked", UNSET))

        formula_show_response = cls(
            success=success,
            error=error,
            name=name,
            scope=scope,
            path=path,
            chain=chain,
            chain_sha=chain_sha,
            vars_=vars_,
            graph=graph,
            errors=errors,
            warnings=warnings,
            as_cooked=as_cooked,
        )

        formula_show_response.additional_properties = d
        return formula_show_response

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
