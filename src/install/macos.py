"""The macOS platform adapter.

``noble-apex.4``: the steps that make a Mac — Apple Silicon or Intel — ready
for the prerequisites the engine then checks.  The adapter owns exactly the
things that are true of macOS and of nowhere else:

* the shell really is running natively, not translated by Rosetta (the
  contract's macOS row says "No Rosetta-based primary installation", and a
  translated shell reports ``x86_64`` on Apple Silicon, which would otherwise
  place a supported Mac in the compatibility tier by accident);
* the Xcode Command Line Tools are present, because Git, the compilers and
  Homebrew's own installer need them;
* Homebrew is discovered **at its real prefix** — ``/opt/homebrew`` on Apple
  Silicon, ``/usr/local`` on Intel, or wherever ``brew`` actually lives — never
  assumed;
* the prefix is on ``PATH`` for the *next* shell, not only for this process;
* missing prerequisites are installed with Homebrew rather than by hand;
* the per-user ``launchd`` domain is reachable, so a later step can install a
  login service (PostgreSQL) instead of a process that dies with the terminal;
* AQ is not about to be installed into Apple's system Python.

Nothing here enters a password.  Homebrew's own installer and
``xcode-select --install`` both need an administrator or a click, so they are
``needs_user`` checkpoints carrying the official command — which is what the
contract asks of every human-only action.

Every reader is injectable (``environ``, ``which``, ``runner``, ``prefixes``,
``home``, ``executable``, ``uid``) so the whole matrix is provable from Linux
CI; the defaults are the real ones.

Homebrew's requirements, prefixes, install command and ``brew shellenv`` PATH
guidance are from <https://docs.brew.sh/Installation>.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .command import INSTALL_TIMEOUT, CommandOutput, CommandRunner, run_command
from .prerequisites import STEP_HOST
from .results import ResourceRecord, StepResult
from .steps import StepContext, StepSpec

OWNER = "macos"

STEP_ARCH = "macos.architecture"
STEP_DEVELOPER_TOOLS = "macos.developer-tools"
STEP_HOMEBREW = "macos.homebrew"
STEP_SHELL_PATH = "macos.shell-path"
STEP_PACKAGES = "macos.packages"
STEP_SERVICES = "macos.launch-services"
STEP_PYTHON_RUNTIME = "macos.python-runtime"

#: Homebrew's documented default prefixes.  They are data, not a guess: the
#: adapter looks for ``brew`` at both and reports the one it found.
ARM_PREFIX = Path("/opt/homebrew")
INTEL_PREFIX = Path("/usr/local")
DEFAULT_PREFIXES: dict[str, Path] = {"arm64": ARM_PREFIX, "x86_64": INTEL_PREFIX}

#: The official installation command.  It is quoted, never executed: it asks
#: for an administrator password, and the installer never types one.
HOMEBREW_INSTALL_COMMAND = (
    '/bin/bash -c "$(curl -fsSL '
    'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)

#: The prerequisites this adapter will install, as ``(formula, executable)``.
#: Git is listed because a Mac without the Command Line Tools has no Git at
#: all; when the tools are present, ``git`` resolves and no formula is touched.
BREW_PREREQUISITES: tuple[tuple[str, str], ...] = (("tmux", "tmux"), ("git", "git"))

#: Login files, by the shell that reads them.  ``zsh`` is the macOS default.
LOGIN_PROFILES: dict[str, str] = {
    "zsh": ".zprofile",
    "bash": ".bash_profile",
    "sh": ".profile",
}

#: The markers that fence the one block this adapter writes into a login file.
#: They are public because they are a contract with uninstall as much as with
#: the operator reading the file: the block AQ added is the block AQ removes,
#: and everything outside the markers is the operator's own configuration.
BEGIN_MARKER = "# >>> agent-queue (aq install) >>>"
END_MARKER = "# <<< agent-queue (aq install) <<<"

#: Prefixes of interpreters macOS itself owns.  ``pip`` into these is
#: externally managed and is replaced by OS updates.
_SYSTEM_PYTHON_PREFIXES: tuple[str, ...] = ("/usr/bin/", "/System/")


def _environ(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _home(environ: Mapping[str, str], home: Path | None) -> Path:
    if home is not None:
        return home
    value = (environ.get("HOME") or "").strip()
    return Path(value) if value else Path(os.path.expanduser("~"))


def _writable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


# -- architecture ------------------------------------------------------------


def architecture_step(*, runner: CommandRunner | None = None) -> StepSpec:
    """Confirm the installer is running natively on this Mac.

    Two independent facts decide it: ``sysctl.proc_translated`` is ``1`` in a
    process Rosetta is translating, and ``hw.optional.arm64`` is ``1`` on
    Apple-Silicon hardware.  Either one disagreeing with the observed
    architecture means the whole matrix verdict was taken through a translation
    layer, so the step fails with the command that opens a native shell rather
    than installing an x86_64 toolchain onto an Apple-Silicon Mac.
    """
    execute = runner or run_command

    def _sysctl(name: str) -> str:
        output = execute(["sysctl", "-n", name])
        return output.out if output.ok else ""

    def _translated_facts() -> tuple[bool, bool]:
        return _sysctl("sysctl.proc_translated") == "1", _sysctl("hw.optional.arm64") == "1"

    def run(context: StepContext) -> StepResult:
        arch = context.facts.arch
        translated, apple_silicon = _translated_facts()
        detail = {
            "arch": arch,
            "machine": context.facts.machine,
            "proc_translated": translated,
            "apple_silicon_hardware": apple_silicon,
        }
        if translated or (apple_silicon and arch != "arm64"):
            return StepResult.failed(
                STEP_ARCH,
                (
                    "this shell is running under Rosetta translation on Apple-Silicon "
                    f"hardware (observed architecture {arch})"
                ),
                (
                    "Open a native shell and rerun `aq install` — for example `arch -arm64 "
                    "zsh`, or uncheck 'Open using Rosetta' in the terminal application's "
                    "Get Info panel. AQ does not support a Rosetta-based installation: an "
                    "x86_64 Homebrew and toolchain would be installed on an arm64 Mac."
                ),
                detail=detail,
            )
        if arch == "arm64":
            return StepResult.succeeded(STEP_ARCH, "native arm64 (Apple Silicon)", detail=detail)
        return StepResult.succeeded(
            STEP_ARCH,
            (
                "native x86_64 (Intel) — the compatibility tier: this release records its "
                "own Intel acceptance evidence"
            ),
            detail=detail,
        )

    def verify(context: StepContext) -> bool:
        translated, apple_silicon = _translated_facts()
        return not translated and not (apple_silicon and context.facts.arch != "arm64")

    return StepSpec(
        id=STEP_ARCH,
        title="Confirm the Mac's architecture",
        description="Rejects a Rosetta-translated shell and reports Apple Silicon or Intel.",
        run=run,
        depends_on=(STEP_HOST,),
        verify=verify,
        owner=OWNER,
    )


# -- Command Line Tools ------------------------------------------------------


def developer_tools_step(*, runner: CommandRunner | None = None) -> StepSpec:
    """Check the Xcode Command Line Tools.

    ``xcode-select --install`` opens a macOS dialog that a human has to accept,
    so a missing toolchain is ``needs_user`` — the contract's word for a
    human-only action — and never an attempt to drive the GUI.
    """
    execute = runner or run_command

    def _selected_path() -> Path | None:
        output = execute(["xcode-select", "-p"])
        if not output.ok or not output.out:
            return None
        return Path(output.out)

    def run(context: StepContext) -> StepResult:
        path = _selected_path()
        if path is None:
            return StepResult.needs_user(
                STEP_DEVELOPER_TOOLS,
                "the Xcode Command Line Tools are not installed",
                (
                    "Run `xcode-select --install` and accept the macOS dialog it opens "
                    "(it needs a click, and an administrator password on a managed Mac), "
                    "then rerun `aq install`. Git, the compilers and Homebrew's own "
                    "installer all come from these tools."
                ),
            )
        if not path.exists():
            return StepResult.needs_user(
                STEP_DEVELOPER_TOOLS,
                f"the selected developer directory {path} does not exist",
                (
                    f"The active developer directory is {path}, which is missing. Run "
                    "`sudo xcode-select --reset` (or point it at an installed Xcode with "
                    "`sudo xcode-select --switch /Applications/Xcode.app`), then rerun "
                    "`aq install`."
                ),
                detail={"path": str(path)},
            )
        return StepResult.succeeded(
            STEP_DEVELOPER_TOOLS,
            f"command line tools at {path}",
            detail={"path": str(path)},
            resources=(
                ResourceRecord(
                    kind="developer-tools",
                    id="command-line-tools",
                    owned=False,
                    reused=True,
                    detail={"path": str(path)},
                ),
            ),
        )

    def verify(context: StepContext) -> bool:
        path = _selected_path()
        return path is not None and path.exists()

    return StepSpec(
        id=STEP_DEVELOPER_TOOLS,
        title="Check the Xcode Command Line Tools",
        description="Git, the compilers and Homebrew's installer all require them.",
        run=run,
        depends_on=(STEP_ARCH,),
        verify=verify,
        owner=OWNER,
    )


# -- Homebrew ----------------------------------------------------------------


def _candidate_prefixes(arch: str, prefixes: Mapping[str, Path]) -> tuple[Path, ...]:
    """The prefix this architecture uses first, then the other documented one."""
    preferred = prefixes.get(arch)
    ordered = [preferred] if preferred else []
    ordered += [value for key, value in prefixes.items() if key != arch]
    seen: dict[Path, None] = {}
    for path in ordered:
        seen.setdefault(path, None)
    return tuple(seen)


def find_brew(
    *,
    arch: str,
    environ: Mapping[str, str],
    which: Callable[[str], str | None],
    prefixes: Mapping[str, Path],
) -> Path | None:
    """Locate the ``brew`` executable without assuming a prefix.

    Three sources, in the order that is actually authoritative: the
    ``HOMEBREW_PREFIX`` a ``brew shellenv`` exported, then ``PATH``, then the
    documented default prefixes for this architecture.  A Mac whose Homebrew
    lives somewhere else is found by the first two.
    """
    exported = (environ.get("HOMEBREW_PREFIX") or "").strip()
    if exported:
        candidate = Path(exported) / "bin" / "brew"
        if candidate.exists():
            return candidate
    resolved = which("brew")
    if resolved:
        return Path(resolved)
    for prefix in _candidate_prefixes(arch, prefixes):
        candidate = prefix / "bin" / "brew"
        if candidate.exists():
            return candidate
    return None


def homebrew_step(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: CommandRunner | None = None,
    prefixes: Mapping[str, Path] | None = None,
) -> StepSpec:
    """Find Homebrew, or say exactly how to install it.

    Three things go wrong on real Macs and each gets its own answer: Homebrew
    is absent (``needs_user`` with the official one-liner — its installer asks
    for an administrator password, which AQ never types), Homebrew is the
    *Intel* one on an Apple-Silicon Mac (``failed``: bottles would be
    x86_64 and the contract excludes a Rosetta-based installation), or the
    prefix is not writable by this user (``failed`` with Homebrew's own
    ``chown`` repair, the classic ``/usr/local`` case).
    """
    lookup = which or shutil.which
    execute = runner or run_command
    table = dict(prefixes or DEFAULT_PREFIXES)

    def _locate(context: StepContext) -> Path | None:
        return find_brew(
            arch=context.facts.arch,
            environ=_environ(environ),
            which=lookup,
            prefixes=table,
        )

    def _prefix_of(brew: Path) -> Path:
        output = execute([str(brew), "--prefix"])
        if output.ok and output.out:
            return Path(output.out)
        # ``<prefix>/bin/brew`` is the documented layout; falling back to it
        # keeps a broken `brew --prefix` from hiding a working installation.
        return brew.parent.parent

    def run(context: StepContext) -> StepResult:
        arch = context.facts.arch
        brew = _locate(context)
        if brew is None:
            expected = table.get(arch, ARM_PREFIX)
            return StepResult.needs_user(
                STEP_HOMEBREW,
                "Homebrew is not installed",
                (
                    "Install Homebrew with its official command:\n"
                    f"    {HOMEBREW_INSTALL_COMMAND}\n"
                    "It asks for your administrator password, which the AQ installer "
                    "never enters for you. It installs to "
                    f"{expected} on this Mac; when it finishes, run the `brew shellenv` "
                    "line it prints and rerun `aq install`."
                ),
                detail={"expected_prefix": str(expected), "arch": arch},
            )

        prefix = _prefix_of(brew)
        other = {value for key, value in table.items() if key != arch}
        detail: dict[str, object] = {
            "brew": str(brew),
            "prefix": str(prefix),
            "arch": arch,
            "default_prefix": str(table.get(arch, "")),
        }
        version = execute([str(brew), "--version"])
        if version.ok and version.out:
            detail["version"] = version.out.splitlines()[0].strip()

        if arch == "arm64" and prefix in other:
            return StepResult.failed(
                STEP_HOMEBREW,
                f"the Homebrew at {prefix} is the Intel build, on Apple-Silicon hardware",
                (
                    f"Install the native Homebrew with:\n    {HOMEBREW_INSTALL_COMMAND}\n"
                    f"run from a native arm64 shell; it installs to {ARM_PREFIX}. The "
                    f"existing {prefix} installation can stay — put {ARM_PREFIX}/bin first "
                    "on PATH — but AQ will not build its dependencies through Rosetta."
                ),
                detail=detail,
            )

        bin_dir = prefix / "bin"
        if not _writable(bin_dir):
            return StepResult.failed(
                STEP_HOMEBREW,
                f"{bin_dir} is not writable by this user",
                (
                    f"Homebrew installs without `sudo` and needs to own its prefix. Run "
                    f"`sudo chown -R $(whoami) {prefix}` (Homebrew's documented repair), "
                    "or `brew doctor` for the full report, then rerun `aq install`."
                ),
                detail=detail,
            )

        note = ""
        if prefix not in set(table.values()):
            note = (
                f" — a non-default prefix; Homebrew builds most formulae from source "
                f"outside {table.get(arch, ARM_PREFIX)}"
            )
        return StepResult.succeeded(
            STEP_HOMEBREW,
            f"Homebrew at {prefix}{note}",
            detail=detail,
            resources=(
                ResourceRecord(
                    kind="package-manager",
                    id="homebrew",
                    owned=False,
                    reused=True,
                    detail={"prefix": str(prefix), "brew": str(brew)},
                ),
            ),
        )

    return StepSpec(
        id=STEP_HOMEBREW,
        title="Find Homebrew",
        description="Discovers Homebrew at its real prefix (/opt/homebrew on Apple Silicon, "
        "/usr/local on Intel) and checks this user can install with it.",
        run=run,
        depends_on=(STEP_DEVELOPER_TOOLS,),
        verify=lambda context: _locate(context) is not None,
        owner=OWNER,
    )


# -- shell PATH --------------------------------------------------------------


def _login_profile(environ: Mapping[str, str], home: Path) -> tuple[str, Path | None]:
    """Return ``(shell name, login file)``; the file is ``None`` when unknown."""
    shell = Path((environ.get("SHELL") or "").strip() or "zsh").name
    filename = LOGIN_PROFILES.get(shell)
    return shell, (home / filename) if filename else None


def shellenv_line(prefix: Path) -> str:
    """The line Homebrew documents for putting its prefix on ``PATH``."""
    return f'eval "$({prefix}/bin/brew shellenv)"'


def shell_path_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    prefixes: Mapping[str, Path] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> StepSpec:
    """Put the Homebrew prefix on ``PATH`` for the shells that come after this one.

    Mutating, and therefore consent-gated: the contract forbids implicit shell
    edits.  The edit is a single marked block containing Homebrew's own
    ``brew shellenv`` line, so a rerun recognises its own work, an uninstall
    can find it, and nothing the user wrote is rewritten.  An unrecognised
    shell (fish, tcsh, nu) is a ``needs_user`` with the line to add rather than
    a guess at that shell's syntax.
    """
    lookup = which or shutil.which
    table = dict(prefixes or DEFAULT_PREFIXES)

    def _prefix(context: StepContext) -> Path | None:
        record = context.existing_resource("package-manager", "homebrew")
        if record and record.detail.get("prefix"):
            return Path(str(record.detail["prefix"]))
        brew = find_brew(
            arch=context.facts.arch,
            environ=_environ(environ),
            which=lookup,
            prefixes=table,
        )
        return brew.parent.parent if brew else None

    def _on_path(prefix: Path) -> bool:
        entries = (_environ(environ).get("PATH") or "").split(os.pathsep)
        return str(prefix / "bin") in entries

    def _configured(profile: Path | None, prefix: Path) -> bool:
        if profile is None or not profile.is_file():
            return False
        text = profile.read_text(encoding="utf-8", errors="replace")
        return BEGIN_MARKER in text or shellenv_line(prefix) in text or "brew shellenv" in text

    def run(context: StepContext) -> StepResult:
        prefix = _prefix(context)
        if prefix is None:
            return StepResult.failed(
                STEP_SHELL_PATH,
                "Homebrew's prefix is unknown, so its bin directory cannot be added to PATH",
                "Complete the `macos.homebrew` step first, then rerun `aq install`.",
            )
        env = _environ(environ)
        shell, profile = _login_profile(env, _home(env, home))
        line = shellenv_line(prefix)
        detail: dict[str, object] = {"shell": shell, "prefix": str(prefix), "line": line}

        if _configured(profile, prefix):
            detail["profile"] = str(profile)
            return StepResult.succeeded(
                STEP_SHELL_PATH,
                f"{profile} already puts {prefix}/bin on PATH",
                detail=detail,
            )
        if profile is None:
            return StepResult.needs_user(
                STEP_SHELL_PATH,
                f"{shell} has no login file this installer edits",
                (
                    f"Add Homebrew to PATH the way {shell} does it — the equivalent of "
                    f"`{prefix}/bin/brew shellenv` — to your login configuration, then "
                    "rerun `aq install`. See https://docs.brew.sh/Installation."
                ),
                detail=detail,
            )

        created = not profile.exists()
        block = f"\n{BEGIN_MARKER}\n{line}\n{END_MARKER}\n"
        try:
            profile.parent.mkdir(parents=True, exist_ok=True)
            with profile.open("a", encoding="utf-8") as handle:
                handle.write(block)
        except OSError as error:
            return StepResult.failed(
                STEP_SHELL_PATH,
                f"could not write {profile}: {error}",
                f"Add this line to {profile} yourself, then rerun `aq install`:\n    {line}",
                detail=detail,
            )
        detail["profile"] = str(profile)
        detail["created"] = created
        return StepResult.succeeded(
            STEP_SHELL_PATH,
            f"added Homebrew's shellenv line to {profile}",
            detail=detail,
            resources=(
                ResourceRecord(
                    kind="shell-profile",
                    id=str(profile),
                    owned=created,
                    reused=not created,
                    detail={
                        "marker": BEGIN_MARKER,
                        "marker_end": END_MARKER,
                        "line": line,
                        "created": created,
                    },
                ),
            ),
        )

    def verify(context: StepContext) -> bool:
        prefix = _prefix(context)
        if prefix is None:
            return False
        env = _environ(environ)
        _, profile = _login_profile(env, _home(env, home))
        return _configured(profile, prefix) or _on_path(prefix)

    consent = "Add Homebrew's `brew shellenv` line to your shell's login file?"
    return StepSpec(
        id=STEP_SHELL_PATH,
        title="Put Homebrew on PATH for new shells",
        description="Appends one marked block with Homebrew's documented `brew shellenv` "
        "line to the login file of the invoking user's shell.",
        run=run,
        depends_on=(STEP_HOMEBREW,),
        mutating=True,
        consent_prompt=consent,
        verify=verify,
        owner=OWNER,
    )


# -- Homebrew packages -------------------------------------------------------


def brew_aware_which(
    base: Callable[[str], str | None] | None = None,
    prefixes: Sequence[Path] | None = None,
) -> Callable[[str], str | None]:
    """``which`` that also looks inside the Homebrew prefixes.

    A formula this run installed lands in ``<prefix>/bin``, which is on the
    *next* shell's PATH — not on this process's.  Without this, ``aq install``
    would install tmux and then report tmux missing on the very same run.
    """
    lookup = base or shutil.which
    roots = tuple(prefixes) if prefixes is not None else tuple(DEFAULT_PREFIXES.values())

    def which(command: str) -> str | None:
        found = lookup(command)
        if found:
            return found
        for prefix in roots:
            candidate = prefix / "bin" / command
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    return which


def packages_step(
    *,
    which: Callable[[str], str | None] | None = None,
    runner: CommandRunner | None = None,
    packages: Sequence[tuple[str, str]] = BREW_PREREQUISITES,
    timeout: float = INSTALL_TIMEOUT,
) -> StepSpec:
    """Install the prerequisites Homebrew owns on this platform.

    Mutating and consent-gated, and idempotent by construction: what is already
    on PATH is never reinstalled, and the step reports the formulae it actually
    installed as owned resources so repair and uninstall know what AQ put
    there.
    """
    lookup = which or shutil.which
    execute = runner or run_command
    required = tuple(packages)

    def _missing() -> tuple[tuple[str, str], ...]:
        return tuple((formula, command) for formula, command in required if not lookup(command))

    def run(context: StepContext) -> StepResult:
        missing = _missing()
        names = [formula for formula, _ in required]
        if not missing:
            return StepResult.succeeded(
                STEP_PACKAGES,
                f"already installed: {', '.join(names)}",
                detail={"required": names, "installed": []},
            )
        record = context.existing_resource("package-manager", "homebrew")
        recorded = str(record.detail.get("brew") or "") if record else ""
        brew = recorded or lookup("brew")
        if not brew:
            return StepResult.failed(
                STEP_PACKAGES,
                f"Homebrew is required to install {', '.join(f for f, _ in missing)}",
                (
                    "Complete the `macos.homebrew` step (or install the listed formulae "
                    "yourself), then rerun `aq install`."
                ),
                detail={"missing": [formula for formula, _ in missing]},
            )

        installed: list[str] = []
        resources: list[ResourceRecord] = []
        for formula, command in missing:
            output: CommandOutput = execute([brew, "install", formula], timeout=timeout)
            if not output.ok:
                return StepResult.failed(
                    STEP_PACKAGES,
                    f"`brew install {formula}` failed: {output.message()}",
                    (
                        f"Run `brew install {formula}` yourself to see the full output, "
                        "then rerun `aq install`. `brew doctor` reports the usual causes "
                        "(an outdated Command Line Tools, a prefix this user cannot write)."
                    ),
                    detail={
                        "formula": formula,
                        "installed": installed,
                        "exit_code": output.returncode,
                    },
                    resources=tuple(resources),
                )
            installed.append(formula)
            resources.append(
                ResourceRecord(
                    kind="brew-formula",
                    id=formula,
                    owned=True,
                    reused=False,
                    detail={"command": command},
                )
            )

        return StepResult.succeeded(
            STEP_PACKAGES,
            f"installed with Homebrew: {', '.join(installed)}",
            detail={"required": names, "installed": installed},
            resources=tuple(resources),
        )

    return StepSpec(
        id=STEP_PACKAGES,
        title="Install prerequisites with Homebrew",
        description=(
            "Installs the missing prerequisites ("
            + ", ".join(formula for formula, _ in required)
            + ") with `brew install`. Anything already on PATH is left alone."
        ),
        run=run,
        depends_on=(STEP_HOMEBREW,),
        mutating=True,
        consent_prompt="Install the missing prerequisites with Homebrew?",
        verify=lambda context: not _missing(),
        owner=OWNER,
    )


# -- launchd -----------------------------------------------------------------


def launch_services_step(
    *,
    runner: CommandRunner | None = None,
    uid: int | None = None,
) -> StepSpec:
    """Check that this session can manage per-user ``launchd`` services.

    ``brew services`` installs a login agent into the invoking user's
    ``launchd`` domain, which exists in a desktop session and not in a plain
    SSH one.  A missing domain is *not* a failed install — nothing on this
    machine is broken and every later step still works — so it is recorded
    ``skipped`` with the reason preserved, which is what the contract's
    ``skipped`` state is for: a later run in a desktop session picks it up.
    """
    execute = runner or run_command

    def _domain() -> str:
        return f"gui/{os.getuid() if uid is None else uid}"

    def run(context: StepContext) -> StepResult:
        domain = _domain()
        output = execute(["launchctl", "print", domain])
        if output.ok:
            return StepResult.succeeded(
                STEP_SERVICES,
                f"per-user launchd domain {domain} is reachable",
                detail={"domain": domain},
            )
        return StepResult.skipped(
            STEP_SERVICES,
            (
                f"the launchd domain {domain} is not reachable from this session "
                f"({output.message()}); services started with `brew services` will run "
                "now but will not start at login. Log in to this Mac's desktop session "
                "once and rerun `aq install`, or manage the service with "
                "`sudo brew services` for a system-wide one."
            ),
            detail={"domain": domain},
        )

    return StepSpec(
        id=STEP_SERVICES,
        title="Check per-user launchd services",
        description="A login service (PostgreSQL) needs the invoking user's launchd domain.",
        run=run,
        depends_on=(STEP_HOMEBREW,),
        owner=OWNER,
    )


# -- Python ------------------------------------------------------------------


def python_runtime_step(
    *,
    executable: str | None = None,
    system_prefixes: Sequence[str] = _SYSTEM_PYTHON_PREFIXES,
) -> StepSpec:
    """Refuse to install AQ into Apple's system Python.

    The acceptance criterion is explicit that macOS installation happens
    "without modifying system Python": ``/usr/bin/python3`` belongs to the OS,
    is externally managed, and is replaced wholesale by macOS updates. Running
    the installer with it is a configuration mistake with a one-line fix, so it
    is reported before the interpreter's *version* is judged.
    """
    interpreter = executable or sys.executable

    def _base() -> str:
        """The interpreter a virtual environment was built from, if any."""
        return str(Path(interpreter or "").resolve()) if interpreter else ""

    def _is_system() -> bool:
        # Resolved, so that a virtual environment created *from* Apple's Python
        # is recognised for what it is: the venv is a symlink to the same
        # externally managed interpreter macOS replaces on every update.
        return any(_base().startswith(prefix) for prefix in system_prefixes)

    def run(context: StepContext) -> StepResult:
        base = _base()
        detail = {
            "executable": interpreter,
            "base_executable": base,
            "version": context.facts.python_version,
        }
        if not _is_system():
            return StepResult.succeeded(
                STEP_PYTHON_RUNTIME,
                f"using {interpreter}, which macOS does not manage",
                detail=detail,
            )
        summary = (
            f"{interpreter} is the macOS system Python"
            if base == interpreter
            else f"{interpreter} is built on the macOS system Python ({base})"
        )
        return StepResult.failed(
            STEP_PYTHON_RUNTIME,
            summary,
            (
                "Install AQ with a Python macOS does not own: `brew install python@3.12`, "
                "then rerun `aq install` with that interpreter (for example "
                "`/opt/homebrew/bin/python3.12 -m venv ~/.agent-queue/venv`). AQ never "
                "modifies /usr/bin/python3 — Apple's Python is externally managed, is "
                "replaced by macOS updates, and takes a virtual environment built on it "
                "down with it."
            ),
            detail=detail,
            retryable=False,
        )

    return StepSpec(
        id=STEP_PYTHON_RUNTIME,
        title="Check the Python running the installer",
        description="AQ is never installed into the macOS system Python.",
        run=run,
        depends_on=(STEP_HOMEBREW,),
        verify=lambda context: not _is_system(),
        owner=OWNER,
    )


# -- the adapter -------------------------------------------------------------


def macos_steps(
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
    runner: CommandRunner | None = None,
    prefixes: Mapping[str, Path] | None = None,
    home: Path | None = None,
    executable: str | None = None,
    uid: int | None = None,
) -> tuple[StepSpec, ...]:
    """Every macOS step, in the order a Mac needs them."""
    return (
        architecture_step(runner=runner),
        developer_tools_step(runner=runner),
        homebrew_step(environ=environ, which=which, runner=runner, prefixes=prefixes),
        shell_path_step(environ=environ, home=home, prefixes=prefixes, which=which),
        packages_step(which=which, runner=runner),
        launch_services_step(runner=runner, uid=uid),
        python_runtime_step(executable=executable),
    )


__all__ = [
    "ARM_PREFIX",
    "BEGIN_MARKER",
    "BREW_PREREQUISITES",
    "DEFAULT_PREFIXES",
    "END_MARKER",
    "HOMEBREW_INSTALL_COMMAND",
    "INTEL_PREFIX",
    "LOGIN_PROFILES",
    "OWNER",
    "STEP_ARCH",
    "STEP_DEVELOPER_TOOLS",
    "STEP_HOMEBREW",
    "STEP_PACKAGES",
    "STEP_PYTHON_RUNTIME",
    "STEP_SERVICES",
    "STEP_SHELL_PATH",
    "architecture_step",
    "brew_aware_which",
    "developer_tools_step",
    "find_brew",
    "homebrew_step",
    "launch_services_step",
    "macos_steps",
    "packages_step",
    "python_runtime_step",
    "shell_path_step",
    "shellenv_line",
]
