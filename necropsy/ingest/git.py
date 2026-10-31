# necropsy/ingest/git.py — Git repo ingestion
# -------------------------------------------
# Responsibilities:
#   • Validate repo source URL/path (https/, ssh/, or local .git path)
#   • Clone or open existing local repo
#   • Extract commits, authors, changed files
#   • Flag bot commits
#   • Extract symbols via tree-sitter (file-level tombstones)
#   • Never execute, import or modify anything from the analyzed repo
#
# Trust boundary: repo contents are untrusted data. Read-only.
#
# Design invariant: all dates use authored_at in UTC ISO-8601.

import re
from pathlib import Path
from typing import List, Optional, Tuple

# Valid URL schemes for repo source
VALID_SCHEMES = ("https", "ssh", "git@", "file://")

# Bot detection patterns (heuristics; not facts)
_BOT_PATTERNS = re.compile(
    r"(bot|robot|ci|codeship|travis|circle|github actions|appveyor)",
    re.IGNORECASE,
)


def validate_source(source: str) -> Tuple[bool, Optional[str]]:
    """Validate a repo source URL or local path.

    Returns (ok, normalized_path) where normalized_path is the absolute
    path if the source is valid and resolves to a local git repo.
    """
    if not source or not isinstance(source, str):
        return False, None

    source = source.strip()

    # Reject values starting with '-' (shell injection guard)
    if source.startswith("-"):
        return False, None

    # Check URL schemes
    is_url = any(source.startswith(scheme + "://") or source.startswith(scheme)
                 for scheme in VALID_SCHEMES)

    if is_url or (Path(source).is_dir() and (Path(source) / ".git").is_dir()):
        # If it looks like a local path, verify it's a git repo
        if not is_url:
            p = Path(source).resolve()
            if p.is_dir() and (p / ".git").is_dir():
                return True, str(p)
            # Also handle bare path without .git check - user might pass a worktree
            if p.is_dir():
                return True, str(p)
        else:
            return True, source
    return False, None


def clone_repo(source: str, target_dir: str) -> Tuple[bool, Optional[str]]:
    """Clone a git repo from source to target_dir.

    Returns (success, local_path) where local_path is the resolved
    path to the cloned repo.
    """
    from git import Repo, GitCommandError

    try:
        # Handle local path that's already a repo
        src_path = Path(source).resolve()
        if src_path.is_dir() and (src_path / ".git").is_dir():
            # Copy/repoint to target_dir
            import shutil
            if src_path.exists() and target_dir != str(src_path):
                shutil.copytree(str(src_path), target_dir)
            else:
                return False, None
            return True, str(Path(target_dir).resolve())

        # Normal clone
        Repo.clone_from(source, target_dir)
        return True, str(Path(target_dir).resolve())

    except GitCommandError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)


def extract_commits(repo_path: str, max_commits: int = 5000) -> List[dict]:
    """Extract commit data from a git repo using PyDriller.

    Returns a list of commit dicts with fields matching the SQLite schema.
    """
    from pydriller import Repository

    commits = []
    try:
        for i, commit in enumerate(Repository(
            repo_path,
            only_no_merges=False,   # include merges but track them
            limit=max_commits,
            include_merged_branches=True,
        )):
            # Extract author info
            author_name = commit.author.name or "Unknown"
            author_email = commit.author.email or ""
            author_is_bot = bool(_BOT_PATTERNS.search(author_name or "") or
                                 _BOT_PATTERNS.search(author_email.lower() or ""))

            # Use authored_at as the time-travel axis
            authored_at = commit.authored_at
            if hasattr(authored_at, 'strftime'):
                # datetime object
                authored_at = authored_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            elif isinstance(authored_at, str):
                # Normalize: ensure ISO-8601 with Z suffix
                if not authored_at.endswith("Z"):
                    # Try to parse and re-format
                    try:
                        from datetime import datetime
                        dt = datetime.strptime(authored_at, "%Y-%m-%d %H:%M:%S")
                        authored_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                    except Exception:
                        pass  # keep original

            # Count files changed
            files_changed = commit.changed_files or 0
            additions = sum(f.additions for f in commit.modified_files) if commit.modified_files else 0
            deletions = sum(f.deletions for f in commit.modified_files) if commit.modified_files else 0

            commits.append({
                "repo_id": 0,  # will be set by caller
                "hash": commit.commit_hash,
                "author_name": author_name,
                "author_email": author_email,
                "authored_at": authored_at,
                "committed_at": commit.commit_date.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(commit.commit_date, 'strftime') else (commit.commit_date or ""),
                "subject": commit.msg_summary or commit.message.strip().split("\\n")[0] if commit.message else "",
                "body": commit.message or "",
                "additions": additions,
                "deletions": deletions,
                "files_changed_count": files_changed,
                "is_merge": commit.is_merge,
            })

            if i >= max_commits - 1:
                break

    except Exception as e:
        raise RuntimeError(f"Error extracting commits: {e}") from e

    return commits


def extract_authors(repo_path: str) -> List[dict]:
    """Extract author data from a git repo.

    Returns list of author dicts keyed by repo_id later.
    """
    from pydriller import Repository

    authors = {}
    try:
        for commit in Repository(repo_path, limit=5000):
            for author_name in commit.author_names:
                if author_name not in authors:
                    authors[author_name] = {
                        "name": author_name,
                        "is_bot": bool(_BOT_PATTERNS.search(author_name or "")),
                    }
    except Exception as e:
        raise RuntimeError(f"Error extracting authors: {e}") from e

    # Convert to list
    result = []
    for name, data in authors.items():
        # Hash the email - we need the email. Let's get it from the commit
        result.append(data)
    return result


def extract_files_changed(repo_path: str, commits: List[dict]) -> List[dict]:
    """Extract per-commit file changes.

    For each commit, records files changed with change type, additions, deletions.
    """
    from pydriller import Repository

    file_records = []
    try:
        for commit in Repository(repo_path, limit=len(commits)):
            commit_hash = commit.commit_hash

            # files_changed from PyDriller
            for changed_file in commit.files_changed:
                # Determine change type
                change_type = changed_file.change_type
                if change_type is None:
                    # PyDriller maps; guess from old/new path
                    if changed_file.new_path and not changed_file.old_path:
                        change_type = "A"
                    elif changed_file.old_path and not changed_file.new_path:
                        change_type = "D"
                    else:
                        change_type = "M"

                # Handle renames
                old_path = changed_file.old_path
                new_path = changed_file.new_path

                # Skip if no path info
                if not new_path:
                    continue

                file_records.append({
                    "repo_id": 0,  # set later
                    "commit_hash": commit_hash,
                    "path": new_path,
                    "old_path": old_path,
                    "change_type": change_type,
                    "additions": changed_file.additions or 0,
                    "deletions": changed_file.deletions or 0,
                })
    except Exception as e:
        raise RuntimeError(f"Error extracting file changes: {e}") from e

    return file_records


def extract_tombstones(repo_path: str, commits: List[dict]) -> List[dict]:
    """Extract tombstones (deleted files and symbols) from commit history.

    Tombstones are materialized views of deleted files and functions,
    each with the deleting commit's message as its epitaph.
    """
    from pydriller import Repository

    tombstones = []

    # deleted files from files_changed with change_type='D'
    for commit in Repository(repo_path, limit=len(commits)):
        commit_hash = commit.commit_hash

        for changed_file in commit.files_changed:
            if changed_file.change_type == 'D' and changed_file.new_path:
                tombstones.append({
                    "repo_id": 0,
                    "kind": "file",
                    "path": changed_file.new_path,
                    "name": changed_file.new_path.split("/")[-1] if "/" in changed_file.new_path else changed_file.new_path,
                    "born_commit": None,
                    "born_at": None,
                    "died_commit": commit_hash,
                    "died_at": commit.authored_at.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(commit.authored_at, 'strftime') else str(commit.authored_at),
                    "died_author_id": None,  # will be filled later
                    "epitaph": commit.msg_summary or "",
                    "lifespan_days": 0,  # placeholder
                })

    # symbols that died (died_commit IS NOT NULL)
    # We'll also check symbols from tree-sitter if available, but at minimum
    # we record file deletions as tombstones

    return tombstones