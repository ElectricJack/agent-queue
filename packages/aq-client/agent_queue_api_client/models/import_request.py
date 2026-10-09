from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.bundle import Bundle
    from ..models.import_request_selections import ImportRequestSelections
    from ..models.import_request_values import ImportRequestValues


T = TypeVar("T", bound="ImportRequest")


@_attrs_define
class ImportRequest:
    """
    Attributes:
        project_id (str):
        path (None | str | Unset):
        bundle (Bundle | None | Unset):
        archive (None | str | Unset):
        values (ImportRequestValues | Unset):
        selections (ImportRequestSelections | Unset):
        only (list[str] | Unset):
        skip (list[str] | Unset):
        no_overwrite (bool | Unset):  Default: False.
    """

    project_id: str
    path: None | str | Unset = UNSET
    bundle: Bundle | None | Unset = UNSET
    archive: None | str | Unset = UNSET
    values: ImportRequestValues | Unset = UNSET
    selections: ImportRequestSelections | Unset = UNSET
    only: list[str] | Unset = UNSET
    skip: list[str] | Unset = UNSET
    no_overwrite: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        from ..models.bundle import Bundle

        project_id = self.project_id

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        bundle: dict[str, Any] | None | Unset
        if isinstance(self.bundle, Unset):
            bundle = UNSET
        elif isinstance(self.bundle, Bundle):
            bundle = self.bundle.to_dict()
        else:
            bundle = self.bundle

        archive: None | str | Unset
        if isinstance(self.archive, Unset):
            archive = UNSET
        else:
            archive = self.archive

        values: dict[str, Any] | Unset = UNSET
        if not isinstance(self.values, Unset):
            values = self.values.to_dict()

        selections: dict[str, Any] | Unset = UNSET
        if not isinstance(self.selections, Unset):
            selections = self.selections.to_dict()

        only: list[str] | Unset = UNSET
        if not isinstance(self.only, Unset):
            only = self.only

        skip: list[str] | Unset = UNSET
        if not isinstance(self.skip, Unset):
            skip = self.skip

        no_overwrite = self.no_overwrite

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if path is not UNSET:
            field_dict["path"] = path
        if bundle is not UNSET:
            field_dict["bundle"] = bundle
        if archive is not UNSET:
            field_dict["archive"] = archive
        if values is not UNSET:
            field_dict["values"] = values
        if selections is not UNSET:
            field_dict["selections"] = selections
        if only is not UNSET:
            field_dict["only"] = only
        if skip is not UNSET:
            field_dict["skip"] = skip
        if no_overwrite is not UNSET:
            field_dict["no_overwrite"] = no_overwrite

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.bundle import Bundle
        from ..models.import_request_selections import ImportRequestSelections
        from ..models.import_request_values import ImportRequestValues

        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

        def _parse_bundle(data: object) -> Bundle | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                bundle_type_0 = Bundle.from_dict(data)

                return bundle_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Bundle | None | Unset, data)

        bundle = _parse_bundle(d.pop("bundle", UNSET))

        def _parse_archive(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        archive = _parse_archive(d.pop("archive", UNSET))

        _values = d.pop("values", UNSET)
        values: ImportRequestValues | Unset
        if isinstance(_values, Unset):
            values = UNSET
        else:
            values = ImportRequestValues.from_dict(_values)

        _selections = d.pop("selections", UNSET)
        selections: ImportRequestSelections | Unset
        if isinstance(_selections, Unset):
            selections = UNSET
        else:
            selections = ImportRequestSelections.from_dict(_selections)

        only = cast(list[str], d.pop("only", UNSET))

        skip = cast(list[str], d.pop("skip", UNSET))

        no_overwrite = d.pop("no_overwrite", UNSET)

        import_request = cls(
            project_id=project_id,
            path=path,
            bundle=bundle,
            archive=archive,
            values=values,
            selections=selections,
            only=only,
            skip=skip,
            no_overwrite=no_overwrite,
        )

        return import_request
