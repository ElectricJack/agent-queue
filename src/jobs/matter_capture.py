"""Execute the trusted ME-1 adapter against the pinned author's source tree.

ME-1 derives ROOT from its script path. Loading the operator-selected adapter
and rebinding ROOT keeps evaluator code outside the author's write scope while
evaluating the author's source closure. All arguments are built by JobService.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys


def main(argv):
    adapter, root, editor, bundle, output, timeout, expected_hash = argv
    path = Path(adapter)
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise ValueError("configured capture adapter changed after submission")
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("aq_matter_capture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = Path(root)
    return module.main(["capture", bundle, output, "--editor", editor, "--timeout", timeout])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
