"""Reviewed script programs and limits; no process execution or authored host paths.

E3 embeds ``ScriptProgram`` in its ScriptStep. Keeping this foundation independent
of definition.py lets old artifacts remain byte-identical and unsupported scripts
remain unactivatable until the executor and real-host acceptance ship.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from src.playbooks.expressions import Value, check_expression_depth

MIB = 1024**2
DIGEST = r"^[0-9a-f]{64}$"
Digest = Annotated[str, Field(pattern=DIGEST)]
Identifier = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class ScriptError(ValueError):
    """Payload-free diagnostics suitable for public admission/receipt surfaces."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def literal_text(value: str) -> str:
    if not value or "\0" in value or any(token in value for token in ("{{", "}}", "${")):
        raise ValueError("script identity must be nonempty literal text")
    return value


def relative_path(value: str) -> str:
    literal_text(value)
    if (len(value) > 512 or any(ord(char) < 32 for char in value)
            or value.startswith(("/", "~")) or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("script path must be a canonical relative path")
    return value


# Names are checked even if an operator accidentally allowlists them. Interpreter
# startup/search hooks and every AQ/provider/DB/agent credential stay outside.
_FORBIDDEN_NAMES = {
    "PATH", "HOME", "PWD", "OLDPWD", "SHELL", "ENV", "BASH_ENV", "CDPATH", "IFS",
    "ZDOTDIR", "GCONV_PATH", "LOCPATH", "TMPDIR", "TMP", "TEMP", "LANG", "LANGUAGE",
    "NODE_OPTIONS", "NODE_PATH", "RUBYOPT", "RUBYLIB", "PERL5OPT", "PERL5LIB",
    "CLASSPATH", "JAVA_TOOL_OPTIONS", "JDK_JAVA_OPTIONS", "_JAVA_OPTIONS",
    "SSLKEYLOGFILE", "GLIBC_TUNABLES",
}
_FORBIDDEN_PREFIXES = (
    "AQ_", "AGENT_QUEUE_", "PYTHON", "LD_", "DYLD_", "LC_", "BASH_FUNC_", "SSH_",
    "GIT_", "AWS_", "AZURE_", "GOOGLE_", "OPENAI_", "ANTHROPIC_", "GEMINI_",
    "DATABASE_", "PG", "DB_", "XDG_", "DBUS_",
)


def environment_name(value: str) -> str:
    if (not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value)
            or value in _FORBIDDEN_NAMES or value.startswith(_FORBIDDEN_PREFIXES)
            or any(word in value for word in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "KEY"))):
        raise ValueError("script environment name is not permitted")
    return value


class ScriptLimits(FrozenModel):
    wall_seconds: StrictInt = Field(default=60, ge=1, le=900)
    cpu_seconds: StrictInt = Field(default=30, ge=1, le=900)
    memory_bytes: StrictInt = Field(default=256 * MIB, ge=MIB)
    pids: StrictInt = Field(default=32, ge=4)
    fds: StrictInt = Field(default=128, ge=16)
    scratch_bytes: StrictInt = Field(default=16 * MIB, ge=4096)
    stdout_bytes: StrictInt = Field(default=MIB, ge=1, le=MIB)
    stderr_bytes: StrictInt = Field(default=MIB, ge=1, le=MIB)

    def narrow(self, ceiling: ScriptLimits) -> ScriptLimits:
        if any(getattr(self, key) > getattr(ceiling, key) for key in type(self).model_fields):
            raise ScriptError("script_limits_exceed_host")
        return self


class ScriptProgram(FrozenModel):
    """Source-authored DTO, independently of the compiler's semantic body.

    Runtime is a reviewed operator manifest identifier, never a host path. Argv
    has a literal first element; all other elements and ``arguments`` are values.
    Exactly one of script/argv/shell is legal. No template engine touches code.
    """

    runtime: Identifier
    script: str | None = None
    argv: tuple[str | Value, ...] | None = None
    shell: str | None = None
    interpreter: str | None = None
    arguments: tuple[Value, ...] = ()
    supporting_files: tuple[str, ...] = ()
    source_kind: Literal["repository", "vault"] = "repository"
    cwd: Literal["scratch", "inputs"] = "scratch"
    stdin: Value | None = None
    env_allowlist: tuple[str, ...] = ()
    limits: ScriptLimits = Field(default_factory=ScriptLimits)
    network: Literal["none"] = "none"
    output: Literal["text", "json"] = "text"
    json_required: bool = False

    @field_validator("script", "interpreter")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return relative_path(value) if value is not None else None

    @field_validator("supporting_files")
    @classmethod
    def _files(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 256 or len(set(values)) != len(values):
            raise ValueError("too many or duplicate script supporting files")
        return tuple(relative_path(value) for value in values)

    @field_validator("env_allowlist")
    @classmethod
    def _names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 64 or len(set(values)) != len(values):
            raise ValueError("too many or duplicate script environment names")
        return tuple(environment_name(value) for value in values)

    @model_validator(mode="after")
    def _program(self) -> ScriptProgram:
        if sum(value is not None for value in (self.script, self.argv, self.shell)) != 1:
            raise ValueError("exactly one script program form is required")
        if self.argv is not None:
            if (not self.argv or not isinstance(self.argv[0], str)
                    or self.interpreter is not None or self.arguments):
                raise ValueError("argv needs a literal executable and separate value arguments")
            relative_path(self.argv[0])
            if any(isinstance(arg, str) for arg in self.argv[1:]):
                raise ValueError("dynamic argv arguments must be typed values")
        elif self.interpreter is None:
            raise ValueError("script and shell require a literal interpreter")
        if self.shell is not None:
            # Shell parameter syntax (${1}, for example) is part of reviewed
            # code. Nothing here evaluates it as a playbook template.
            if not self.shell or "\0" in self.shell or len(self.shell.encode()) > 65536:
                raise ValueError("shell text must be literal, nonempty and at most 64 KiB")
        if self.json_required and self.output != "json":
            raise ValueError("json_required needs JSON output")
        values = [*self.arguments, *(self.argv[1:] if self.argv else ())]
        if len(values) > 256:
            raise ValueError("too many script arguments")
        for value in [*values, *([self.stdin] if self.stdin is not None else [])]:
            check_expression_depth(value)
        return self

    @property
    def executable(self) -> str:
        return self.argv[0] if self.argv is not None else self.interpreter  # type: ignore[return-value]


def script_environment(
    authored: tuple[str, ...], allowed: tuple[str, ...], supplied: dict[str, str]
) -> dict[str, str]:
    """Never read os.environ; even PATH/HOME/locale have fixed sandbox values."""
    permitted = set(authored) & set(allowed)
    if any(not isinstance(k, str) or not isinstance(v, str) for k, v in supplied.items()):
        raise ScriptError("script_environment_denied")
    if len(supplied) > 64 or sum(len(k.encode()) + len(v.encode()) for k, v in supplied.items()) > 32768:
        raise ScriptError("script_environment_too_large")
    result = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": "/scratch"}
    for name, value in supplied.items():
        try:
            environment_name(name)
        except ValueError:
            raise ScriptError("script_environment_denied") from None
        if name not in permitted or not isinstance(value, str) or "\0" in value:
            raise ScriptError("script_environment_denied")
        result[name] = value
    return result
