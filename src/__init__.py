# Compatibility shims must be applied before any other imports that may
# chain back to ``pkg_resources`` (notably ``memsearch`` → ``pymilvus`` →
# ``milvus_lite``).  Keep this import first — see :mod:`src._compat` for
# the background.
from src import _compat as _compat  # noqa: F401,E402  (side-effect import)
