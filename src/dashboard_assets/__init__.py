"""The dashboard bundle staged into release artifacts.

The directory is deliberately populated by ``scripts/build_release_artifact.py``
rather than at import time. End-user wheels therefore contain an already-built
SPA and do not need Node.js, npm, or an AQ source checkout to use it.
"""
