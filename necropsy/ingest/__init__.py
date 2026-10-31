# necropsy/ingest/__init__.py — Ingest package
# --------------------------------------------
# Public API:
#   • validate_source(source) -> (ok, reason)
#   • clone_repo(source, target) -> (ok, error)
#   • extract_commits / extract_files_changed / extract_tombstones

from .git import (
    validate_source,
    clone_repo,
    extract_commits,
    extract_files_changed,
    extract_tombstones,
)

__all__ = [
    "validate_source",
    "clone_repo",
    "extract_commits",
    "extract_files_changed",
    "extract_tombstones",
]
