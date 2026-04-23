"""Tests for :mod:`src._compat` — pkg_resources / pkgutil.ImpImporter shim.

These tests guard against a regression that previously broke memory
operations and vault watchers on Python 3.12+ with setuptools >= 82.

See ``src/_compat.py`` for background.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
import textwrap

import pytest


# ---------------------------------------------------------------------------
# In-process tests — verify the shim is idempotent and well-formed.
# ---------------------------------------------------------------------------


def test_compat_applied_on_src_import() -> None:
    """Importing :mod:`src` must install the ``pkgutil.ImpImporter`` stub.

    This is the fast-path guard.  The shim is also responsible for the
    ``pkg_resources`` stub, but we can't easily force the real package
    away mid-test (see the subprocess tests below for that).
    """
    # Importing src triggers the side-effect import of src._compat,
    # which calls apply() at module import time.
    import src  # noqa: F401  (side-effect: applies compat shim)

    # pkgutil.ImpImporter must be accessible (real or stub).  This
    # prevents ``AttributeError("module 'pkgutil' has no attribute
    # 'ImpImporter'")`` when legacy ``pkg_resources`` tries to register
    # it as a finder.
    assert hasattr(pkgutil, "ImpImporter"), (
        "pkgutil.ImpImporter must exist (real on <3.12, stub on >=3.12)"
    )


def test_apply_is_idempotent() -> None:
    """Calling :func:`apply` multiple times must be safe."""
    from src import _compat

    _compat.apply()
    _compat.apply()
    _compat.apply()

    assert hasattr(pkgutil, "ImpImporter")


def test_pkg_resources_importable_after_shim() -> None:
    """``import pkg_resources`` must succeed after the shim is applied.

    Whether backed by the real package or the shim, the imports used by
    :mod:`milvus_lite` must work.
    """
    import src  # noqa: F401  (side-effect: applies compat shim)

    import pkg_resources  # noqa: PLC0415

    assert hasattr(pkg_resources, "DistributionNotFound")
    assert hasattr(pkg_resources, "get_distribution")


# ---------------------------------------------------------------------------
# Subprocess tests — force the missing / broken scenarios deterministically.
# ---------------------------------------------------------------------------


def _run_in_subprocess(code: str) -> subprocess.CompletedProcess[str]:
    """Run *code* in a fresh interpreter so we control sys.modules state."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_shim_provides_pkg_resources_when_missing() -> None:
    """When ``pkg_resources`` is absent, the shim installs a working stub.

    We simulate setuptools >= 82 (which removed ``pkg_resources``) by
    blocking the import in a fresh subprocess and verifying that, after
    applying the shim, ``from pkg_resources import DistributionNotFound,
    get_distribution`` succeeds — exactly the call ``milvus_lite``
    performs at import time.
    """
    result = _run_in_subprocess(
        """
        import sys

        # Block the real pkg_resources so the shim path is exercised.
        class _Blocker:
            def find_module(self, fullname, path=None):
                if fullname == "pkg_resources" or fullname.startswith("pkg_resources."):
                    return self
                return None
            def load_module(self, fullname):
                raise ImportError(f"blocked: {fullname}")
            # Modern meta-path API
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "pkg_resources" or fullname.startswith("pkg_resources."):
                    raise ModuleNotFoundError(f"blocked: {fullname}")
                return None

        sys.meta_path.insert(0, _Blocker())

        # Discard any previously loaded pkg_resources so the shim
        # actually runs the "install stub" branch.
        for m in list(sys.modules):
            if m == "pkg_resources" or m.startswith("pkg_resources."):
                del sys.modules[m]

        sys.path.insert(0, %r)

        from src import _compat
        _compat.apply()

        # Remove the blocker now that the shim is in sys.modules.
        sys.meta_path.pop(0)

        from pkg_resources import DistributionNotFound, get_distribution

        # get_distribution works for a real installed package
        d = get_distribution("pytest")
        assert d.version, f"expected pytest version, got {d!r}"

        # DistributionNotFound is raised for missing packages
        try:
            get_distribution("definitely-not-a-real-package-xyz-12345")
        except DistributionNotFound:
            pass
        else:
            raise AssertionError("DistributionNotFound was not raised")

        print("SHIM_OK")
        """ % ("/mnt/d/Dev/agent-queue3",)
    )

    assert "SHIM_OK" in result.stdout, (
        f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


def test_shim_installs_imp_importer_stub_when_missing() -> None:
    """When ``pkgutil.ImpImporter`` is absent, the shim must install a stub."""
    result = _run_in_subprocess(
        """
        import sys, pkgutil

        # Force the "missing" branch even on Pythons that still have it.
        if hasattr(pkgutil, "ImpImporter"):
            del pkgutil.ImpImporter

        sys.path.insert(0, %r)
        from src import _compat
        _compat.apply()

        assert hasattr(pkgutil, "ImpImporter"), "stub was not installed"
        # Stub must be a class that can be instantiated so
        # pkg_resources.register_finder(pkgutil.ImpImporter, ...) works.
        assert isinstance(pkgutil.ImpImporter, type), "stub is not a class"
        pkgutil.ImpImporter()  # stub must be instantiable

        print("STUB_OK")
        """ % ("/mnt/d/Dev/agent-queue3",)
    )

    assert "STUB_OK" in result.stdout, (
        f"subprocess failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )


@pytest.mark.integration
def test_milvus_lite_importable_after_shim() -> None:
    """Regression test: importing :mod:`milvus_lite` must not raise.

    This was the original failure mode — ``milvus_lite.__init__`` does
    ``from pkg_resources import DistributionNotFound, get_distribution``
    at module load time, which blew up with
    ``AttributeError("module 'pkgutil' has no attribute 'ImpImporter'")``
    or ``ModuleNotFoundError("No module named 'pkg_resources'")``
    depending on the installed ``setuptools`` version.

    Gated behind the ``integration`` marker because it requires the
    optional ``memsearch[milvus_lite]`` extras to be installed.
    """
    pytest.importorskip("milvus_lite")

    # Clear any cached failure so we exercise the full import path.
    for m in list(sys.modules):
        if m == "milvus_lite" or m.startswith("milvus_lite."):
            del sys.modules[m]

    import src  # noqa: F401  (side-effect: applies compat shim)

    milvus_lite = importlib.import_module("milvus_lite")
    # Sanity check: the module has a version attribute.
    assert hasattr(milvus_lite, "__version__")
