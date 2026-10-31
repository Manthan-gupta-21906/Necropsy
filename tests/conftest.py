"""Shared pytest fixtures.

Every test runs against a temporary SQLite database: NECROPSY_DB_PATH is set
before any necropsy module is imported, so nothing can touch
data/necropsy.db. No network and no Ollama are required.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Must happen before necropsy imports -----------------------------------
_TMP_ROOT = Path(tempfile.mkdtemp(prefix="necropsy_tests_"))
os.environ["NECROPSY_DB_PATH"] = str(_TMP_ROOT / "necropsy_test.db")
os.environ.setdefault("NECROPSY_LLM_TIMEOUT", "30")

from necropsy import config  # noqa: E402
from necropsy.db import (  # noqa: E402
    add_commits,
    add_file_changes,
    add_repo,
    add_tombstones,
    finalize_repo,
    init_db,
    reset_repo,
)
from necropsy.index.embed import compute_embeddings  # noqa: E402
from necropsy.index.store import store_embeddings  # noqa: E402
from necropsy.ingest.git import (  # noqa: E402
    extract_commits,
    extract_files_changed,
    extract_tombstones,
)

FIXTURE_COMMITS = [
    ("2024-01-15T10:00:00Z", "Jan initial commit",
     [("README.md", "# Initial\n"), ("lexer.py", "def lex():\n    pass\n")], []),
    ("2024-02-10T10:00:00Z", "Feb feature add",
     [("feature.py", "def feature():\n    pass\n")], []),
    ("2024-03-20T10:00:00Z", "Mar cleanup pass",
     [("cleanup.py", "# cleanup\n")], ["lexer.py"]),
]


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Remove the temp database directory after the run."""
    shutil.rmtree(_TMP_ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def temp_db_path() -> Path:
    """The temp database every test uses."""
    return Path(os.environ["NECROPSY_DB_PATH"])


@pytest.fixture()
def fresh_db(temp_db_path):
    """A clean, empty database for one test."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(temp_db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    init_db()
    return temp_db_path


@pytest.fixture(scope="session")
def fixture_repo() -> Path:
    """A real git repo with three commits on known dates."""
    tmp = Path(tempfile.mkdtemp(prefix="necropsy_fixture_"))

    def run(*args, **kwargs):
        return subprocess.run(["git", *args], cwd=str(tmp),
                              capture_output=True, text=True, **kwargs)

    run("init", "-q")
    run("config", "user.name", "Test Author")
    run("config", "user.email", "test@example.com")
    run("config", "commit.gpgsign", "false")

    for date, message, added, removed in FIXTURE_COMMITS:
        for name, content in added:
            (tmp / name).write_text(content, encoding="utf-8")
        if added:
            run("add", "--", *[n for n, _ in added])
        for name in removed:
            run("rm", "-q", "--", name)
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        result = subprocess.run(
            ["git", "commit", "-q", "-m", message],
            cwd=str(tmp), capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"fixture commit failed: {result.stderr}")

    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


def index_repo(repo_path: Path, name: str = "fixture") -> dict:
    """Ingest a repo into the current temp DB and store real embeddings."""
    repo_id = add_repo(source=str(repo_path), name=name, status="ingesting",
                       local_path=str(repo_path))
    reset_repo(repo_id)

    commits = extract_commits(str(repo_path))
    add_commits(repo_id, commits)
    add_file_changes(repo_id, extract_files_changed(str(repo_path), commits))
    add_tombstones(repo_id, extract_tombstones(str(repo_path), commits))

    items = []
    texts = []
    for commit in commits:
        text = f"{commit['subject']}\n{commit.get('body', '')}".strip()
        items.append({
            "item_type": "commit",
            "item_ref": commit["hash"],
            "item_at": commit["authored_at"],
        })
        texts.append(text)
    vectors = compute_embeddings(texts, config.EMBED_MODEL)
    stored = store_embeddings(repo_id, items, config.EMBED_MODEL, vectors)

    finalize_repo(
        repo_id=repo_id,
        head_sha=commits[0]["hash"],
        first_commit_at=min(c["authored_at"] for c in commits),
        last_commit_at=max(c["authored_at"] for c in commits),
        commit_count=len(commits),
        local_path=str(repo_path),
        status="ready",
    )
    return {
        "repo_id": repo_id,
        "commits": commits,
        "embeddings": stored,
        "by_subject": {c["subject"]: c for c in commits},
    }


@pytest.fixture()
def indexed_repo(fresh_db, fixture_repo):  # noqa: ARG001
    """A repo already ingested and embedded in the temp DB."""
    return index_repo(fixture_repo)
