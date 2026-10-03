"""A missing project never implicitly selects global record authority."""

from pydantic import Field, model_validator

from src.commands.contracts.models import CommandArgs


class RecordScopeArgs(CommandArgs):
    project_id: str | None = Field(default=None, min_length=1)
    global_scope: bool = False

    @model_validator(mode="after")
    def explicit_scope(self):
        if bool(self.project_id) == self.global_scope:
            raise ValueError("Choose exactly one of project_id and global_scope")
        return self
