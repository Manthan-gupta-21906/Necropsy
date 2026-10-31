# necropsy/ingest/git.py — Git repository ingestion
# ----------------------------------------------
# Extracts commits, authors, files changed, and tombstones from a Git repository.

from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
import hashlib
import os
import re

import subprocess
import tempfile
import shutil

from necropsy.db import get_connection, init_db, add_repo, add_commits, add_file_changes, add_tombstones
from necropsy.index.store import store_embeddings
from necropsy.index.retrieve import retrieve
from necropsy.index.embed import embed_text
from necropsy.config import EMBED_MODEL


def validate_source(source: str) -> bool:
    """Validate a repo source URL or local path."""
    if not source or not isinstance(source, str):
        return False
    source = source.strip()
    if source.startswith("-"):
        return False
    return True


def clone_repo(source: str, target: str) -> bool:
    """Clone a repository from a URL to a target directory."""
    try:
        subprocess.run(
            ["git", "clone", source, target],
            capture_output=True,
            env={**os.environ, "GIT_AUTHOR_DATE": "2024-01-15T10:00:00Z", "GIT_COMMITTER_DATE": "2024-01-15T10:00:00Z"}
        )
        return os.path.exists(os.path.join(target, ".git"))
    except Exception:
        return False


def create_fixture_repo() -> str:
    """Create a temporary fixture repo for testing."""
    tmpdir = tempfile.mkdtemp(prefix="necropsy_fixture_")
    subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmpdir, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=tmpdir, capture_output=True
    )

    # Create initial commit
    with open(os.path.join(tmpdir, "README.md"), "w") as f:
        f.write("# Initial Commit\n")
    subprocess.run(["git", "add", "."], cwd=tmpdir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"], cwd=tmpdir, capture_output=True
    )

    # Create commit A: Jan 15, 2024
    with open(os.path.join(tmpdir, "a.txt"), "w") as f:
        f.write("A content\n")
    subprocess.run(
        ["git", "commit", "--date", "2024-01-15 10:00:00", "-m", "Add A"], cwd=tmpdir,
        capture_output=True
    )

    # Create commit B: Feb 10, 2024
    with open(os.path.join(tmpdir, "b.txt"), "w") as f:
        f.write("B content\n")
    subprocess.run(
        ["git", "commit", "--date", "2024-02-10 10:00:00", "-m", "Add B"], cwd=tmpdir,
        capture_output=True
    )

    # Create commit C: Mar 20, 2024
    with open(os.path.join(tmpdir, "c.txt"), "w") as f:
        f.write("C content\n")
    subprocess.run(
        ["git", "commit", "--date", "2024-03-20 10:00:00", "-m", "Add C"], cwd=tmpdir,
        capture_output=True
    )

    return tmpdir


def extract_commits(repo_path: str, max_commits: int = 5000) -> List[dict]:
    """Extract commit data from a git repo using PyDriller.

    Returns a list of commit dicts with fields matching the SQLite schema.
    """
    from pydriller import Repository

    commits = []
    try:
        repo = Repository(repo_path, only_no_merge=False)
        for i, commit in enumerate(repo.traverse_commits()):
            # Extract author info
            author_name = commit.author.name or "Unknown"
            author_email = commit.author.email or ""
            author_is_bot = _is_bot(author_name, author_email)

            # Use author_date as the time-travel axis
            authored_at = commit.author_date
            if hasattr(authored_at, "strftime"):
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
            modified_files = commit.modified_files or []
            files_changed = len(modified_files)
            additions = sum(f.added_lines for f in modified_files) if modified_files else 0
            deletions = sum(f.deleted_lines for f in modified_files) if modified_files else 0

            commits.append({
                "repo_id": 0,  # will be set by caller
                "hash": commit.hash,
                "author_name": author_name,
                "author_email": author_email,
                "authored_at": authored_at,
                "committed_at": commit.committer_date.strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(commit.committer_date, 'strftime') else (commit.committer_date or ""),
                "subject": commit.msg or commit.msg.strip().split("\\n")[0] if commit.msg else "",
                "body": commit.msg or "",
                "additions": additions,
                "deletions": deletions,
                "files_changed_count": files_changed,
                "is_merge": commit.merge,
            })

            if i >= max_commits - 1:
                break

    except Exception as e:
        raise RuntimeError(f"Error extracting commits: {e}") from e

    return commits


def _is_bot(author_name: str, author_email: str) -> bool:
    """Check if an author is a bot."""
    patterns = [
        re.compile(r"^bot", re.IGNORECASE),
        re.compile(r"^robot", re.IGNORECASE),
        re.compile(r"^automated", re.IGNORECASE),
        re.compile(r"^ci", re.IGNORECASE),
        re.compile(r"^continuous", re.IGNORECASE),
        re.compile(r"^build", re.IGNORECASE),
    ]
    for pattern in patterns:
        if pattern.search(author_name or "") or pattern.search(author_email.lower() or ""):
            return True
    return False


def extract_authors(repo_path: str) -> List[dict]:
    """Extract author data from a git repo.

    Returns list of author dicts keyed by repo_id later.
    """
    from pydriller import Repository

    authors = []
    try:
        repo = Repository(repo_path, only_no_merge=False)
        for commit in repo.traverse_commits():
            for author in commit.authors:
                author_name = author.name or "Unknown"
                author_email = author.email or ""
                author_is_bot = _is_bot(author_name, author_email)

                authors.append({
                    "repo_id": 0,  # will be set by caller
                    "display_name": author_name,
                    "email_hash": hashlib.sha256(author_email.strip().lower().encode()).hexdigest()[:8],
                    "is_bot": 1 if author_is_bot else 0,
                })
    except Exception as e:
        raise RuntimeError(f"Error extracting authors: {e}") from e

    return authors


def extract_files_changed(repo_path: str, commits: List[dict]) -> List[dict]:
    """Extract file changes for the given commits."""
    from pydriller import Repository

    files = []
    try:
        repo = Repository(repo_path, only_no_merge=False)
        for commit in repo.traverse_commits():
            for file in commit.modified_files:
                change_type = file.change_type
                if change_type in ('A', 'M', 'D', 'R', 'C'):
                    files.append({
                        "repo_id": 0,  # will be set by caller
                        "commit_hash": commit.hash,
                        "path": file.filename,
                        "old_path": file.old_path,
                        "change_type": change_type,
                        "additions": file.added_lines if hasattr(file, 'added_lines') else 0,
                        "deletions": file.deleted_lines if hasattr(file, 'deleted_lines') else 0,
                    })
    except Exception as e:
        raise RuntimeError(f"Error extracting file changes: {e}") from e

    return files


def extract_tombstones(repo_path: str, commits: List[dict]) -> List[dict]:
    """Extract tombstone data for deleted files/functions."""
    from pydriller import Repository

    tombstones = []
    try:
        repo = Repository(repo_path, only_no_merge=False)
        for commit in repo.traverse_commits():
            # Check for deleted files
            for file in commit.deleted_files or []:
                tombstones.append({
                    "repo_id": 0,  # will be set by caller
                    "commit_hash": commit.hash,
                    "kind": "file",
                    "path": file.filename,
                    "born_commit": commit.hash,
                    "born_at": file.author_date if hasattr(file, 'author_date') else "",
                    "died_commit": commit.hash,
                    "died_at": commit.author_date,
                    "died_author_id": 0,
                    "epitaph": f"Deleted in {commit.hash}",
                    "lifespan_days": 0,
                })
            # Check for deleted symbols
            for symbol in commit.deleted_symbols or []:
                tombstones.append({
                    "repo_id": 0,  # will be set by caller
                    "commit_hash": commit.hash,
                    "kind": "symbol",
                    "name": symbol.name,
                    "symbol_id": 0,
                    "born_commit": commit.hash,
                    "born_at": commit.author_date,
                    "died_commit": commit.hash,
                    "died_at": commit.author_date,
                    "died_author_id": 0,
                    "epitaph": f"Deleted in {commit.hash}",
                    "lifespan_days": 0,
                })
    except Exception as e:
        raise RuntimeError(f"Error extracting tombstones: {e}") from e

    return tombstones