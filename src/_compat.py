"""Runtime compatibility shims for Python 3.12+ / modern setuptools.

This module patches around two related breakages that surface as
``AttributeError("module 'pkgutil' has no attribute 'ImpImporter'")`` (or
``ModuleNotFoundError("No module named 'pkg_resources'")``) during memory
system initialization:

1. **``pkgutil.ImpImporter`` was removed in Python 3.12.**  Older versions
   of ``pkg_resources`` access it at module load time without a
   ``hasattr`` guard, so merely importing ``pkg_resources`` raises
   ``AttributeError`` on Python 3.12+.

2. **``setuptools >= 82`` removed the ``pkg_resources`` package entirely.**
   Legacy third-party packages such as ``milvus_lite`` 2.5.x still
   ``from pkg_resources import DistributionNotFound, get_distribution`` at
   module import time.  Without a real ``pkg_resources`` those imports
   fail with ``ModuleNotFoundError``.

The symptoms previously observed on the running daemon:

- ``Failed to start vault watcher for aq_*: AttributeError("module
  'pkgutil' has no attribute 'ImpImporter'")``
- ``memory_store failed: Store failed: module 'pkgutil' has no attribute
  'ImpImporter'``
- ``Recall failed: module 'pkgutil' has no attribute 'ImpImporter'``

Both flow through :mod:`memsearch` → :mod:`pymilvus` → :mod:`milvus_lite`
→ ``pkg_resources``.

:func:`apply` is called unconditionally at module import time so a simple
``import src._compat`` (placed before any memsearch/pymilvus/milvus_lite
import) is enough to restore functionality.  It is idempotent and safe
to call multiple times.
"""

from __future__ import annotations

import logging
import pkgutil
import sys

logger = logging.getLogger(__name__)

_APPLIED = False


def apply() -> None:
    """Install all compat shims.  Idempotent.

    Call as early as possible in the process lifetime — before any import
    of :mod:`memsearch`, :mod:`pymilvus`, :mod:`milvus_lite`, or other
    packages that chain back to ``pkg_resources``.
    """
    global _APPLIED
    if _APPLIED:
        return
    _ensure_imp_importer()
    _ensure_pkg_resources()
    _APPLIED = True


def _ensure_imp_importer() -> None:
    """Ensure ``pkgutil.ImpImporter`` exists.

    Python 3.12 removed ``pkgutil.ImpImporter``.  Very old
    ``pkg_resources`` versions reference it at module top level without
    an ``hasattr`` guard, raising ``AttributeError`` on import.  We
    install a no-op stub so the attribute lookup succeeds; the stub is
    never used for real import-finder registration on modern Python.
    """
    if hasattr(pkgutil, "ImpImporter"):
        return

    class _ImpImporterStub:
        """Stub for removed ``pkgutil.ImpImporter``.

        Satisfies attribute lookups and ``register_finder`` /
        ``register_namespace_handler`` calls from legacy
        ``pkg_resources`` code.  Not functional.
        """

    pkgutil.ImpImporter = _ImpImporterStub  # type: ignore[attr-defined]
    logger.debug(
        "Installed pkgutil.ImpImporter stub (removed in Python 3.12)"
    )


def _ensure_pkg_resources() -> None:
    """Provide a minimal ``pkg_resources`` shim when the real one is gone.

    ``setuptools >= 82`` dropped the ``pkg_resources`` package.  Legacy
    libraries still import a small subset of its API at module load
    time — most commonly::

        from pkg_resources import DistributionNotFound, get_distribution

    (seen in ``milvus_lite`` 2.5.x).  When the real ``pkg_resources`` is
    present we do nothing.  Otherwise we install a tiny stub module that
    implements the handful of names those legacy imports need, backed
    by :mod:`importlib.metadata`.
    """
    # If a real pkg_resources is importable, leave it alone so installed
    # packages that genuinely need the full API continue to work.
    try:
        import pkg_resources  # noqa: F401
        return
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        # Anything else (e.g. a partially broken pkg_resources) means the
        # real one can't be relied on — fall through to install the stub.
        logger.debug(
            "pkg_resources import failed (%r); installing shim", exc
        )

    import importlib.metadata as _md
    import types

    shim = types.ModuleType("pkg_resources")
    shim.__doc__ = (
        "Minimal compatibility shim installed by src._compat because "
        "setuptools removed the real pkg_resources package."
    )

    class DistributionNotFound(Exception):
        """Raised when a distribution can't be found.

        Mirrors :class:`pkg_resources.DistributionNotFound` so legacy
        ``except`` / ``with suppress(...)`` blocks keep working.
        """

    class VersionConflict(Exception):
        """Mirrors :class:`pkg_resources.VersionConflict`."""

    class _Distribution:
        """Minimal Distribution stand-in with the fields legacy code reads."""

        def __init__(self, project_name: str, version: str) -> None:
            self.project_name = project_name
            self.key = project_name.lower().replace("_", "-")
            self.version = version

        def __repr__(self) -> str:
            return f"Distribution({self.project_name}=={self.version})"

    def get_distribution(name: str) -> _Distribution:
        """Return a Distribution-like object for *name*.

        Raises :class:`DistributionNotFound` if the package is not
        installed — mirroring the real API so callers'
        ``contextlib.suppress(DistributionNotFound)`` blocks still work.
        """
        try:
            version = _md.version(name)
        except _md.PackageNotFoundError as exc:
            raise DistributionNotFound(str(exc)) from exc
        return _Distribution(name, version)

    def iter_entry_points(group: str, name: str | None = None):
        """Minimal ``iter_entry_points`` using ``importlib.metadata``."""
        eps = _md.entry_points()
        # Python 3.10+ returns EntryPoints with select()
        if hasattr(eps, "select"):
            selected = eps.select(group=group)
        else:  # pragma: no cover - legacy mapping API
            selected = eps.get(group, [])
        for ep in selected:
            if name is None or ep.name == name:
                yield ep

    def require(*_requirements: str) -> list[_Distribution]:
        """No-op stub for :func:`pkg_resources.require`.

        Returns an empty list rather than attempting dependency
        resolution — legacy callers generally ignore the return value.
        """
        return []

    def resource_filename(package: str, resource: str) -> str:
        """Locate a resource file using :mod:`importlib.resources`."""
        import importlib.resources as _res

        try:
            path = _res.files(package).joinpath(resource)
            return str(path)
        except Exception:
            # Fall back to a best-effort join so callers that ignore
            # non-existent paths don't crash.
            return resource

    shim.DistributionNotFound = DistributionNotFound
    shim.VersionConflict = VersionConflict
    shim.Distribution = _Distribution
    shim.get_distribution = get_distribution
    shim.iter_entry_points = iter_entry_points
    shim.require = require
    shim.resource_filename = resource_filename
    shim.__all__ = [
        "Distribution",
        "DistributionNotFound",
        "VersionConflict",
        "get_distribution",
        "iter_entry_points",
        "require",
        "resource_filename",
    ]

    sys.modules["pkg_resources"] = shim
    logger.debug(
        "Installed pkg_resources shim (real package unavailable — "
        "setuptools >=82 removed it)"
    )


# Apply on import so ``import src._compat`` is enough.
apply()
