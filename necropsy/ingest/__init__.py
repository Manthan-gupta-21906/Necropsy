# necropsy/ingest/__init__.py — Ingest package
# --------------------------------------------
# Public API:
#   • ingest_repo(source, repo_id) -> dict with status/progress
#   • RepoStatus enum: pending, ingesting, indexing, ready, failed

from .git import validate_source, clone_repo, extract_commits, extract_authors, extract_files_changed, extract_tombstones

__all__ = ["validate_source", "clone_repo", "extract_commits", "extract_authors",
           "extract_files_changed", "extract_tombstones"]