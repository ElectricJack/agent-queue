"""Host detection and the supported-platform matrix.

The installer must recognise the host it was asked to change *before* it
changes anything, and it must refuse an unsupported host rather than reporting
a best-effort success.  ``docs/plans/install-onboarding/contract.md``
("Supported-platform matrix") is the authority for the rows below.

Detection is deliberately injectable — every reader is a parameter with a real
default — so the whole matrix is testable on one machine.  Nothing here
mutates the host or shells out to a package manager: that belongs to the
platform adapters (``noble-apex.3`` / ``.4``), which consume these facts.
"""

from __future__ import annotations

import os
import platform as _platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Host paths the matrix knows about.  ``unsupported`` is a real value, not an
#: absence: an unrecognised host is still reported with its observed facts.
HOST_WSL2 = "windows-wsl2"
HOST_MACOS_ARM = "macos-apple-silicon"
HOST_MACOS_INTEL = "macos-intel"
HOST_UNSUPPORTED = "unsupported"

#: The WSL2 distribution with native acceptance evidence.  Others are
#: "detect and explain" until they earn their own evidence row.
SUPPORTED_WSL_DISTRO = ("ubuntu", "24.04")

#: Minimum macOS release, matching Homebrew's supported Apple-Silicon tier.
MINIMUM_MACOS_MAJOR = 14

#: Support tiers.  ``compatibility`` still installs; it just does not carry an
#: AQ guarantee without its own native acceptance evidence per release.
TIER_SUPPORTED = "supported"
TIER_COMPATIBILITY = "compatibility"
TIER_UNSUPPORTED = "unsupported"

_SUPPORTED_PATH_DOC = "docs/plans/install-onboarding/contract.md"


def _normalise_arch(machine: str) -> str:
    value = (machine or "").strip().lower()
    if value in {"x86_64", "amd64", "x64"}:
        return "x86_64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value or "unknown"


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_os_release(text: str | None) -> dict[str, str]:
    """Parse ``/etc/os-release`` into a plain mapping.

    Values may be quoted and lines may be blank or commented; anything that is
    not ``KEY=VALUE`` is ignored rather than raising, because a malformed file
    on the host must not crash detection.
    """
    result: dict[str, str] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


@dataclass(frozen=True, slots=True)
class PlatformFacts:
    """Everything the matrix decides on, as observed on this host.

    These fields are non-secret by construction (no paths from a project
    checkout, no environment values) and are written verbatim into the resume
    record and the machine-readable result.
    """

    system: str
    release: str
    machine: str
    arch: str
    python_version: str
    distro_id: str | None = None
    distro_version: str | None = None
    distro_name: str | None = None
    wsl: bool = False
    wsl_version: int | None = None
    macos_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "release": self.release,
            "machine": self.machine,
            "arch": self.arch,
            "python_version": self.python_version,
            "distro_id": self.distro_id,
            "distro_version": self.distro_version,
            "distro_name": self.distro_name,
            "wsl": self.wsl,
            "wsl_version": self.wsl_version,
            "macos_version": self.macos_version,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> PlatformFacts:
        return cls(
            system=str(payload.get("system", "")),
            release=str(payload.get("release", "")),
            machine=str(payload.get("machine", "")),
            arch=str(payload.get("arch", "")),
            python_version=str(payload.get("python_version", "")),
            distro_id=payload.get("distro_id"),
            distro_version=payload.get("distro_version"),
            distro_name=payload.get("distro_name"),
            wsl=bool(payload.get("wsl", False)),
            wsl_version=payload.get("wsl_version"),
            macos_version=payload.get("macos_version"),
        )


def detect_wsl(
    *,
    environ: Mapping[str, str],
    kernel_release: str,
    read_text: Callable[[Path], str | None] = _read_text,
) -> tuple[bool, int | None]:
    """Return ``(is_wsl, wsl_generation)``.

    WSL2 announces itself in three independent ways and no single one is
    reliable alone: ``WSL_INTEROP`` exists only when interop is enabled,
    ``WSL_DISTRO_NAME`` is absent in some service contexts, and the kernel
    string is the only signal a WSL1 host shares with WSL2.  The generation is
    what the matrix cares about, so a "microsoft" kernel with no WSL2 marker is
    reported as WSL **1** rather than as an unknown WSL.
    """
    kernel = (kernel_release or "").lower()
    marked = "WSL_DISTRO_NAME" in environ or "WSL_INTEROP" in environ
    kernel_says_wsl = "microsoft" in kernel or "wsl" in kernel
    if not marked and not kernel_says_wsl:
        return False, None
    if "wsl2" in kernel:
        return True, 2
    if environ.get("WSL_INTEROP") or read_text(Path("/proc/sys/fs/binfmt_misc/WSLInterop")):
        # Interop is a WSL2-only feature in current Windows builds; WSL1 has no
        # binfmt handler registered under that name.
        return True, 2
    return True, 1


def detect_platform(
    *,
    environ: Mapping[str, str] | None = None,
    uname: Any = None,
    mac_ver: Callable[[], tuple[str, Any, str]] | None = None,
    read_text: Callable[[Path], str | None] = _read_text,
    python_version: str | None = None,
) -> PlatformFacts:
    """Observe the current host.  Read-only; never raises on a strange host."""
    environ = os.environ if environ is None else environ
    uname = _platform.uname() if uname is None else uname
    system = (getattr(uname, "system", "") or "").lower()
    release = getattr(uname, "release", "") or ""
    machine = getattr(uname, "machine", "") or ""

    distro_id = distro_version = distro_name = None
    wsl, wsl_version = False, None
    macos_version = None

    if system == "linux":
        os_release = parse_os_release(read_text(Path("/etc/os-release")))
        distro_id = (os_release.get("ID") or "").lower() or None
        distro_version = os_release.get("VERSION_ID") or None
        distro_name = os_release.get("PRETTY_NAME") or os_release.get("NAME") or None
        wsl, wsl_version = detect_wsl(environ=environ, kernel_release=release, read_text=read_text)
    elif system == "darwin":
        reader = mac_ver or _platform.mac_ver
        macos_version = (reader()[0] or "").strip() or None

    return PlatformFacts(
        system=system,
        release=release,
        machine=machine,
        arch=_normalise_arch(machine),
        python_version=python_version or _python_version(),
        distro_id=distro_id,
        distro_version=distro_version,
        distro_name=distro_name,
        wsl=wsl,
        wsl_version=wsl_version,
        macos_version=macos_version,
    )


def _python_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


@dataclass(frozen=True, slots=True)
class SupportVerdict:
    """Where the observed host sits in the matrix, and why."""

    host_path: str
    tier: str
    facts: PlatformFacts
    reasons: tuple[str, ...] = ()
    remediation: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def installable(self) -> bool:
        """True when the installer may proceed to mutate this host."""
        return self.tier in (TIER_SUPPORTED, TIER_COMPATIBILITY)

    def to_dict(self) -> dict[str, Any]:
        payload = self.facts.to_dict()
        payload.update(
            {
                "host_path": self.host_path,
                "tier": self.tier,
                "installable": self.installable,
                "reasons": list(self.reasons),
                "remediation": self.remediation,
                "notes": list(self.notes),
            }
        )
        return payload


def _major(version: str | None) -> int | None:
    head = (version or "").split(".", 1)[0].strip()
    return int(head) if head.isdigit() else None


def evaluate_support(facts: PlatformFacts) -> SupportVerdict:
    """Place *facts* in the supported-platform matrix."""
    if facts.system == "linux" and facts.wsl:
        return _evaluate_wsl(facts)
    if facts.system == "darwin":
        return _evaluate_macos(facts)
    if facts.system == "linux":
        return SupportVerdict(
            host_path=HOST_UNSUPPORTED,
            tier=TIER_UNSUPPORTED,
            facts=facts,
            reasons=(
                (
                    f"{facts.distro_name or 'this Linux host'} is not a WSL2 distribution; "
                    "the supported Linux path is Windows + WSL2."
                ),
            ),
            remediation=(
                "Install AQ inside a supported WSL2 distribution on Windows, or on macOS 14+. "
                f"See {_SUPPORTED_PATH_DOC}."
            ),
        )
    return SupportVerdict(
        host_path=HOST_UNSUPPORTED,
        tier=TIER_UNSUPPORTED,
        facts=facts,
        reasons=(f"unsupported operating system: {facts.system or 'unknown'}",),
        remediation=(
            "Supported hosts are Windows 10 2004+/11 with WSL2, and macOS 14 or newer. "
            f"See {_SUPPORTED_PATH_DOC}."
        ),
    )


def _evaluate_wsl(facts: PlatformFacts) -> SupportVerdict:
    if facts.wsl_version != 2:
        return SupportVerdict(
            host_path=HOST_UNSUPPORTED,
            tier=TIER_UNSUPPORTED,
            facts=facts,
            reasons=(f"WSL{facts.wsl_version or 1} is not supported; AQ requires WSL2.",),
            remediation=(
                "Convert the distribution with `wsl --set-version <distro> 2` from Windows, "
                "then rerun the installer inside it."
            ),
        )
    if facts.arch not in {"x86_64", "arm64"}:
        return SupportVerdict(
            host_path=HOST_UNSUPPORTED,
            tier=TIER_UNSUPPORTED,
            facts=facts,
            reasons=(f"unsupported WSL2 architecture: {facts.machine or 'unknown'}",),
            remediation="AQ supports x86_64 and arm64 WSL2 hosts.",
        )
    distro_id = (facts.distro_id or "").lower()
    version = facts.distro_version or ""
    if (distro_id, version) == SUPPORTED_WSL_DISTRO:
        return SupportVerdict(host_path=HOST_WSL2, tier=TIER_SUPPORTED, facts=facts)
    return SupportVerdict(
        host_path=HOST_UNSUPPORTED,
        tier=TIER_UNSUPPORTED,
        facts=facts,
        reasons=(
            f"{facts.distro_name or distro_id or 'this distribution'} {version or ''}".strip()
            + " has no native acceptance evidence; the supported WSL2 distribution is "
            "Ubuntu 24.04 LTS.",
        ),
        remediation=(
            "Install Ubuntu 24.04 with `wsl --install -d Ubuntu-24.04` and rerun the "
            "installer inside it."
        ),
    )


def _evaluate_macos(facts: PlatformFacts) -> SupportVerdict:
    major = _major(facts.macos_version)
    if major is None:
        return SupportVerdict(
            host_path=HOST_UNSUPPORTED,
            tier=TIER_UNSUPPORTED,
            facts=facts,
            reasons=("could not determine the macOS version",),
            remediation="Report `sw_vers` output; AQ requires macOS 14 (Sonoma) or newer.",
        )
    if major < MINIMUM_MACOS_MAJOR:
        return SupportVerdict(
            host_path=HOST_UNSUPPORTED,
            tier=TIER_UNSUPPORTED,
            facts=facts,
            reasons=(
                (
                    f"macOS {facts.macos_version} is older than the supported baseline "
                    f"(macOS {MINIMUM_MACOS_MAJOR})."
                ),
            ),
            remediation="Upgrade to macOS 14 (Sonoma) or newer, then rerun the installer.",
        )
    if facts.arch == "arm64":
        return SupportVerdict(host_path=HOST_MACOS_ARM, tier=TIER_SUPPORTED, facts=facts)
    if facts.arch == "x86_64":
        return SupportVerdict(
            host_path=HOST_MACOS_INTEL,
            tier=TIER_COMPATIBILITY,
            facts=facts,
            notes=(
                (
                    "Intel macOS is a compatibility tier: the installer must not assume "
                    "/opt/homebrew, and each release records its own native acceptance "
                    "evidence."
                ),
            ),
        )
    return SupportVerdict(
        host_path=HOST_UNSUPPORTED,
        tier=TIER_UNSUPPORTED,
        facts=facts,
        reasons=(f"unsupported macOS architecture: {facts.machine or 'unknown'}",),
        remediation="AQ supports Apple Silicon (arm64) and Intel (x86_64) Macs.",
    )


def describe_host(*, environ: Mapping[str, str] | None = None, uname: Any = None) -> SupportVerdict:
    """Convenience: detect the host and place it in the matrix in one call."""
    return evaluate_support(detect_platform(environ=environ, uname=uname))
