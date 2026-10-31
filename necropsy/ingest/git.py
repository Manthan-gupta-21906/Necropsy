# necropsy/ingest/git.py â€” Git repository ingestion
# ----------------------------------------------
# Extracts commits, authors, files changed, and tombstones from a Git repository.
#
# Read-only toward the analyzed repo: we never execute or import anything from it.

import hashlib
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

# PyDriller change types are an IntEnum; map them to our single-letter codes.
# Imported at module load so the (slow) import cost is paid at startup, not
# inside a background indexing thread where it would stall the event loop.
try:
    from pydriller import Repository
    from pydriller.domain.commit import ModificationType

    _CHANGE_MAP = {
        ModificationType.ADD: "A",
        ModificationType.MODIFY: "M",
        ModificationType.DELETE: "D",
        ModificationType.RENAME: "R",
        ModificationType.COPY: "C",
    }
except Exception:  # pragma: no cover - depends on environment
    Repository = None  # type: ignore
    ModificationType = None  # type: ignore
    _CHANGE_MAP = {}


def validate_source(source: str) -> tuple[bool, str]:
    """Validate a repo source URL or local path.

    Accepts only:
      â€¢ https:// URLs
      â€¢ ssh:// URLs or git@host:path scp-style
      â€¢ an existing local directory containing .git
    Rejects anything starting with '-' (argument injection).
    """
    if not source or not isinstance(source, str):
        return False, "Source must be a non-empty string."

    source = source.strip()
    if not source:
        return False, "Source must be a non-empty string."
    if source.startswith("-"):
        return False, "Source must not start with '-'."

    if source.startswith("https://") or source.startswith("ssh://"):
        return True, ""
    if re.match(r"^[\w.\-]+@[\w.\-]+:.+$", source):  # git@github.com:user/repo.git
        return True, ""

    local = Path(source)
    if local.exists() and local.is_dir():
        if (local / ".git").exists():
            return True, ""
        return False, f"Local path has no .git directory: {source}"

    return False, (
        "Source must be an https:// or ssh:///git@ URL, or an existing "
        "local directory containing .git."
    )


def clone_repo(source: str, target: str, timeout: int = 300) -> tuple[bool, Optional[str]]:
    """Clone a repository from a URL to a target directory.

    Returns (ok, error_message). Never prompts for credentials, so a private
    or unreachable repo fails fast with a clear message instead of hanging.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "echo",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=no",
    }
    try:
        proc = subprocess.run(
            ["git", "clone", "--quiet", source, target],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Cloning timed out after {timeout}s. Is the repository reachable?"
    except FileNotFoundError:
        return False, "git executable not found on PATH."

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"git exited with {proc.returncode}"
        return False, (
            f"Could not clone {source}: {reason}. "
            "Private or unreachable repositories are not supported without credentials."
        )

    if not os.path.exists(os.path.join(target, ".git")):
        return False, "Clone finished but no .git directory was produced."

    return True, None


def create_fixture_repo() -> str:
    """Create a temporary fixture repo with commits on known dates."""
    tmpdir = tempfile.mkdtemp(prefix="necropsy_fixture_")
    subprocess.run(["git", "init"], cwd=tmpdir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmpdir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmpdir, capture_output=True)

    for i, (name, date, msg) in enumerate(
        [
            ("README.md", "2024-01-15T10:00:00Z", "Jan initial commit"),
            ("feature.py", "2024-02-10T10:00:00Z", "Feb feature add"),
            ("cleanup.py", "2024-03-20T10:00:00Z", "Mar cleanup pass"),
        ]
    ):
        with open(os.path.join(tmpdir, name), "w", encoding="utf-8") as f:
            f.write(f"# {name}\n")
        subprocess.run(["git", "add", name], cwd=tmpdir, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=tmpdir,
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            },
        )

    return tmpdir


def _iso(dt) -> str:
    """Format a datetime as an ISO-8601 UTC string with a Z suffix."""
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt if dt.endswith("Z") else dt
    if getattr(dt, "tzinfo", None) is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_pydriller() -> None:
    """Fail with a clear message when PyDriller is unavailable."""
    if Repository is None:
        raise RuntimeError(
            "PyDriller is not installed. Install it with: pip install PyDriller"
        )


def extract_commits(repo_path: str, max_commits: int = 5000) -> List[dict]:
    """Extract commit data from a git repo using PyDriller.

    Returns a list of commit dicts with fields matching the SQLite schema.
    """
    _require_pydriller()

    commits: List[dict] = []
    try:
        repo = Repository(repo_path, only_no_merge=False)
        for i, commit in enumerate(repo.traverse_commits()):
            author_name = (commit.author.name or "Unknown") if commit.author else "Unknown"
            author_email = (commit.author.email or "") if commit.author else ""
            author_is_bot = 1 if _is_bot(author_name, author_email) else 0

            message = (commit.msg or "").strip()
            subject = message.splitlines()[0] if message else ""
            body = message

            modified_files = commit.modified_files or []
            additions = sum(int(f.added_lines or 0) for f in modified_files)
            deletions = sum(int(f.deleted_lines or 0) for f in modified_files)

            commits.append(
                {
                    "hash": commit.hash,
                    "author_name": author_name,
                    "author_email": author_email,
                    "author_is_bot": author_is_bot,
                    "authored_at": _iso(commit.author_date),
                    "committed_at": _iso(commit.committer_date),
                    "subject": subject,
                    "body": body,
                    "additions": additions,
                    "deletions": deletions,
                    "files_changed_count": len(modified_files),
                    "is_merge": 1 if commit.merge else 0,
                    "parents": list(commit.parents or []),
                }
            )

            if i >= max_commits - 1:
                break
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error extracting commits: {e}") from e

    return commits


def _is_bot(author_name: str, author_email: str) -> bool:
    """Check if an author is a bot."""
    patterns = [
        re.compile(r"\[bot\]", re.IGNORECASE),
        re.compile(r"^(bot|robot|automated|ci|continuous|build)", re.IGNORECASE),
    ]
    name = author_name or ""
    email = (author_email or "").lower()
    return any(p.search(name) or p.search(email) for p in patterns)


def extract_files_changed(repo_path: str, commits: List[dict]) -> List[dict]:
    """Extract file changes for the given commits."""
    _require_pydriller()

    files: List[dict] = []
    try:
        repo = Repository(repo_path, only_no_merge=False)
        for commit in repo.traverse_commits():
            for f in commit.modified_files or []:
                code = _CHANGE_MAP.get(f.change_type)
                if code is None:
                    continue
                files.append(
                    {
                        "commit_hash": commit.hash,
                        "path": f.new_path or f.filename,
                        "old_path": f.old_path,
                        "change_type": code,
                        "additions": int(f.added_lines or 0),
                        "deletions": int(f.deleted_lines or 0),
                    }
                )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error extracting file changes: {e}") from e

    return files


def extract_tombstones(repo_path: str, commits: List[dict]) -> List[dict]:
    """Extract tombstones for files deleted in a commit.

    Born date is the commit that added the path (if we can see it in history),
    otherwise the deletion commit date.
    """

    by_hash = {c["hash"]: c for c in commits}
    first_seen: Dict[str, tuple] = {}
    tombstones: List[dict] = []
    _require_pydriller()
    try:
        repo = Repository(repo_path, only_no_merge=False)
        all_commits = list(repo.traverse_commits())
        # Sort explicitly: PyDriller's traversal order is not guaranteed.
        all_commits.sort(key=lambda c: c.author_date)

        # Oldest first: the first time we see a path is when it was born.
        for commit in all_commits:
            at = _iso(commit.author_date)
            for f in commit.modified_files or []:
                path = f.new_path or f.filename
                if path and path not in first_seen:
                    first_seen[path] = (at, commit.hash)

        for commit in all_commits:
            at = _iso(commit.author_date)
            for f in commit.modified_files or []:
                code = _CHANGE_MAP.get(f.change_type)
                if code != "D":
                    continue
                path = f.old_path or f.new_path or f.filename
                born_at, born_commit = first_seen.get(path, (at, commit.hash))
                try:
                    born_dt = datetime.strptime(born_at, "%Y-%m-%dT%H:%M:%SZ")
                    died_dt = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ")
                    lifespan = max((died_dt - born_dt).days, 0)
                except Exception:  # noqa: BLE001
                    lifespan = 0
                tombstones.append(
                    {
                        "kind": "file",
                        "path": path,
                        "name": None,
                        "symbol_id": None,
                        "born_commit": born_commit,
                        "born_at": born_at,
                        "died_commit": commit.hash,
                        "died_at": at,
                        "died_author_id": None,
                        "epitaph": f"{path} was deleted here.",
                        "lifespan_days": lifespan,
                    }
                )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Error extracting tombstones: {e}") from e

    return tombstones
