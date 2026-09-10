"""Host detection and the supported-platform matrix.

Every case is built from injected facts rather than the machine running the
suite: the matrix has to be provable on one box, and a test that only passes on
WSL2 proves nothing about the macOS rows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.install.platform import (
    HOST_MACOS_ARM,
    HOST_MACOS_INTEL,
    HOST_UNSUPPORTED,
    HOST_WSL2,
    TIER_COMPATIBILITY,
    TIER_SUPPORTED,
    TIER_UNSUPPORTED,
    PlatformFacts,
    detect_platform,
    detect_wsl,
    evaluate_support,
    parse_os_release,
)


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure detection tests never allocate a database."""


class _Uname:
    def __init__(self, system: str, release: str = "", machine: str = "") -> None:
        self.system = system
        self.release = release
        self.machine = machine


UBUNTU_2404 = 'ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.4 LTS"\n'


def _reader(mapping: dict[str, str]):
    def read(path: Path) -> str | None:
        return mapping.get(str(path))

    return read


def _wsl_facts(**overrides) -> PlatformFacts:
    base = {
        "system": "linux",
        "release": "6.6.0-microsoft-standard-WSL2",
        "machine": "x86_64",
        "arch": "x86_64",
        "python_version": "3.12.3",
        "distro_id": "ubuntu",
        "distro_version": "24.04",
        "distro_name": "Ubuntu 24.04.4 LTS",
        "wsl": True,
        "wsl_version": 2,
    }
    base.update(overrides)
    return PlatformFacts(**base)


def _mac_facts(**overrides) -> PlatformFacts:
    base = {
        "system": "darwin",
        "release": "23.5.0",
        "machine": "arm64",
        "arch": "arm64",
        "python_version": "3.12.3",
        "macos_version": "14.5",
    }
    base.update(overrides)
    return PlatformFacts(**base)


# -- os-release parsing -----------------------------------------------------


def test_os_release_parsing_tolerates_quotes_comments_and_junk():
    parsed = parse_os_release(
        "# a comment\nID=ubuntu\nVERSION_ID=\"24.04\"\nNAME='Ubuntu'\nnot-a-pair\n\n"
    )
    assert parsed == {"ID": "ubuntu", "VERSION_ID": "24.04", "NAME": "Ubuntu"}


def test_os_release_parsing_of_a_missing_file_is_empty_not_an_error():
    assert parse_os_release(None) == {}


# -- WSL generation ---------------------------------------------------------


def test_wsl2_is_recognised_from_the_kernel_string():
    assert detect_wsl(
        environ={}, kernel_release="6.6.0-microsoft-standard-WSL2", read_text=_reader({})
    ) == (True, 2)


def test_wsl1_kernel_without_an_interop_marker_is_reported_as_generation_one():
    """A "microsoft" kernel alone is the only signal WSL1 and WSL2 share."""
    assert detect_wsl(
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
        kernel_release="4.4.0-19041-Microsoft",
        read_text=_reader({}),
    ) == (True, 1)


def test_interop_registration_promotes_an_ambiguous_kernel_to_wsl2():
    assert detect_wsl(
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
        kernel_release="5.15.0-generic",
        read_text=_reader({"/proc/sys/fs/binfmt_misc/WSLInterop": "enabled"}),
    ) == (True, 2)


def test_a_plain_linux_host_is_not_wsl():
    assert detect_wsl(environ={}, kernel_release="6.8.0-generic", read_text=_reader({})) == (
        False,
        None,
    )


# -- detection --------------------------------------------------------------


def test_detection_reports_distro_architecture_and_wsl_generation():
    facts = detect_platform(
        environ={"WSL_DISTRO_NAME": "Ubuntu"},
        uname=_Uname("Linux", "6.6.0-microsoft-standard-WSL2", "aarch64"),
        read_text=_reader({"/etc/os-release": UBUNTU_2404}),
    )
    assert (facts.system, facts.distro_id, facts.distro_version) == ("linux", "ubuntu", "24.04")
    assert (facts.arch, facts.wsl, facts.wsl_version) == ("arm64", True, 2)


def test_detection_reports_the_macos_release():
    facts = detect_platform(
        environ={},
        uname=_Uname("Darwin", "23.5.0", "x86_64"),
        mac_ver=lambda: ("14.5", ("", "", ""), "x86_64"),
    )
    assert (facts.system, facts.macos_version, facts.arch) == ("darwin", "14.5", "x86_64")


def test_detection_round_trips_through_the_record_payload():
    facts = _wsl_facts()
    assert PlatformFacts.from_dict(facts.to_dict()) == facts


# -- the matrix -------------------------------------------------------------


def test_ubuntu_2404_on_wsl2_is_the_supported_windows_path():
    verdict = evaluate_support(_wsl_facts())
    assert (verdict.host_path, verdict.tier) == (HOST_WSL2, TIER_SUPPORTED)
    assert verdict.installable


def test_wsl1_is_refused_with_the_conversion_command():
    verdict = evaluate_support(_wsl_facts(wsl_version=1))
    assert (verdict.host_path, verdict.tier) == (HOST_UNSUPPORTED, TIER_UNSUPPORTED)
    assert not verdict.installable
    assert "wsl --set-version" in (verdict.remediation or "")


def test_an_unevidenced_wsl_distribution_is_detected_and_explained():
    verdict = evaluate_support(
        _wsl_facts(distro_id="debian", distro_version="12", distro_name="Debian GNU/Linux 12")
    )
    assert verdict.tier == TIER_UNSUPPORTED
    assert "Ubuntu 24.04" in " ".join(verdict.reasons)
    assert "wsl --install" in (verdict.remediation or "")


def test_a_plain_linux_host_is_pointed_at_the_supported_paths():
    verdict = evaluate_support(
        _wsl_facts(wsl=False, wsl_version=None, distro_name="Fedora Linux 40")
    )
    assert verdict.tier == TIER_UNSUPPORTED
    assert "WSL2" in " ".join(verdict.reasons)


def test_apple_silicon_on_sonoma_or_newer_is_supported():
    verdict = evaluate_support(_mac_facts(macos_version="15.1"))
    assert (verdict.host_path, verdict.tier) == (HOST_MACOS_ARM, TIER_SUPPORTED)


def test_intel_macos_installs_as_a_compatibility_tier_with_its_limits_stated():
    verdict = evaluate_support(_mac_facts(machine="x86_64", arch="x86_64"))
    assert (verdict.host_path, verdict.tier) == (HOST_MACOS_INTEL, TIER_COMPATIBILITY)
    assert verdict.installable
    assert "/opt/homebrew" in " ".join(verdict.notes)


def test_macos_older_than_the_baseline_is_refused_before_any_mutation():
    verdict = evaluate_support(_mac_facts(macos_version="13.6"))
    assert verdict.tier == TIER_UNSUPPORTED
    assert not verdict.installable
    assert "macOS 14" in (verdict.remediation or "")


def test_an_unreadable_macos_version_is_refused_rather_than_assumed():
    verdict = evaluate_support(_mac_facts(macos_version=None))
    assert verdict.tier == TIER_UNSUPPORTED


def test_an_unknown_operating_system_reports_what_it_observed():
    verdict = evaluate_support(
        PlatformFacts(
            system="windows", release="10", machine="AMD64", arch="x86_64", python_version="3.12.3"
        )
    )
    assert verdict.tier == TIER_UNSUPPORTED
    assert "windows" in " ".join(verdict.reasons)
    payload = verdict.to_dict()
    assert payload["installable"] is False
    assert payload["system"] == "windows"
