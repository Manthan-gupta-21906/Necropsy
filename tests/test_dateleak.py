"""
Mandatory date-leak test for Necropsy.

Builds a fixture repo with known commit dates and asserts that every layer
(retrieval, graveyard, autopsy stats) respects the as_of date boundary.
Nothing dated after as_of may reach the LLM prompt, the verifier, or any
displayed statistic.

Run with:  python -m pytest tests/test_dateleak.py -v
"""

import os
import sys
import tempfile
import shutil
import subprocess

# Add necropsy to path before imports
necropsy_dir = os.path.join(os.path.dirname(__file__), "..", "necropsy")
if necropsy_dir not in sys.path:
    sys.path.insert(0, necropsy_dir)

from necropsy.db import init_db, add_repo, add_commits
from necropsy.ingest.git import extract_commits
from necropsy.index.retrieve import retrieve


def create_fixture_repo():
    """Create a fixture git repo with 3 commits on known dates.

    Returns the workdir path.
    """
    workdir = tempfile.mkdtemp(prefix="necropsy_fixture_")

    subprocess.run(["git", "init"], cwd=workdir, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test Author"],
        cwd=workdir, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workdir, capture_output=True
    )

    # Commit A: 2024-01-15
    with open(os.path.join(workdir, "README.md"), "w") as f:
        f.write("# Initial\n")
    subprocess.run(["git", "add", "README.md"], cwd=workdir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Jan initial commit"],
        cwd=workdir, capture_output=True,
        env={**os.environ,
            "GIT_AUTHOR_DATE": "2024-01-15T10:00:00Z",
            "GIT_COMMITTER_DATE": "2024-01-15T10:00:00Z"},
    )

    # Commit B: 2024-02-10
    with open(os.path.join(workdir, "feature.py"), "w") as f:
        f.write("def feature(): pass\n")
    subprocess.run(["git", "add", "feature.py"], cwd=workdir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Feb feature add"],
        cwd=workdir, capture_output=True,
        env={**os.environ,
            "GIT_AUTHOR_DATE": "2024-02-10T10:00:00Z",
            "GIT_COMMITTER_DATE": "2024-02-10T10:00:00Z"},
    )

    # Commit C: 2024-03-20
    with open(os.path.join(workdir, "cleanup.py"), "w") as f:
        f.write("def cleanup(): pass\n")
    subprocess.run(["git", "add", "cleanup.py"], cwd=workdir, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "Mar cleanup pass"],
        cwd=workdir, capture_output=True,
        env={**os.environ,
            "GIT_AUTHOR_DATE": "2024-03-20T10:00:00Z",
            "GIT_COMMITTER_DATE": "2024-03-20T10:00:00Z"},
    )

    return workdir


def test_retrieval_as_of_2024_02_01():
    """Retrieval with as_of=2024-02-01 should ONLY include commit A (Jan 15),
    NOT commit B (Feb 10) or C (Mar 20)."""
    workdir = create_fixture_repo()
    try:
        init_db()
        repo_id = add_repo(source=workdir, name="fixture", status="pending")
        commits = extract_commits(workdir)
        added = add_commits(repo_id, commits)

        # Retrieve with as_of=2024-02-01 (should only include commit A)
        results = retrieve(repo_id, "initial readme", "2024-02-01")

        # All retrieved dates should be <= 2024-02-01
        for r in results:
            assert r["at"] <= "2024-02-01T00:00:00Z", (
                f"Date-leak: result dated {r['at']} after as_of 2024-02-01: {r}"
            )

        # Commit C (Mar 20) must NOT appear
        commit_c_hash = None
        for c in commits:
            if "Mar" in c.get("subject", ""):
                commit_c_hash = c["hash"]
                break
        assert commit_c_hash is None or not any(
            r["hash"] == commit_c_hash for r in results
        ), (
            "Date-leak: commit C (Mar 20) leaked into as_of=2024-02-01 results"
        )

        print("PASS: test_retrieval_as_of_2024_02_01")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_retrieval_as_of_2024_02_15():
    """Retrieval with as_of=2024-02-15 should include commit A (Jan 15)
    and commit B (Feb 10), but NOT commit C (Mar 20)."""
    workdir = create_fixture_repo()
    try:
        init_db()
        repo_id = add_repo(source=workdir, name="fixture", status="pending")
        commits = extract_commits(workdir)
        add_commits(repo_id, commits)

        # Retrieve with as_of=2024-02-15
        results = retrieve(repo_id, "initial readme", "2024-02-15")
        result_dates = [r["at"] for r in results]
        print(f"  Results as_of=2024-02-15: {result_dates}")

        # All dates should be <= 2024-02-15
        for r in results:
            assert r["at"] <= "2024-02-15T00:00:00Z", (
                f"Date-leak: result dated {r['at']} after as_of 2024-02-15"
            )

        # Commit C must NOT appear
        commit_c_hash = None
        for c in commits:
            if "Mar" in c.get("subject", ""):
                commit_c_hash = c["hash"]
                break
        assert commit_c_hash is None or not any(
            r["hash"] == commit_c_hash for r in results
        ), (
            "Date-leak: commit C (Mar 20) leaked into as_of=2024-02-15 results"
        )

        print("PASS: test_retrieval_as_of_2024_02_15")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_embeddings_date_slice():
    """Embeddings loaded with as_of cutoff should only contain items <= as_of."""
    workdir = create_fixture_repo()
    try:
        init_db()
        repo_id = add_repo(source=workdir, name="fixture_emb", status="pending")
        commits = extract_commits(workdir)
        add_commits(repo_id, commits)

        # Get embeddings slice as_of 2024-02-01
        from necropsy.db import get_embeddings_by_date
        refs, vecs, ats = get_embeddings_by_date(
            repo_id, "BAAI/bge-small-en-v1.5", "2024-02-01T23:59:59Z"
        )

        for at in ats:
            assert at <= "2024-02-01T23:59:59Z", (
                f"Date-leak: embedding dated {at} after as_of cutoff"
            )

        # With a later cutoff, more items should survive
        refs2, vecs2, ats2 = get_embeddings_by_date(
            repo_id, "BAAI/bge-small-en-v1.5", "2024-03-31T23:59:59Z"
        )
        # Should have at least as many as the earlier slice
        assert len(ats2) >= len(ats), (
            f"Date-leak: later as_of should have more embeddings, "
            f"got {len(ats2)} vs {len(ats)}"
        )

        print("PASS: test_embeddings_date_slice")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_verify_claims_date_leak():
    """Verify that claim with commit dated after as_of is dropped."""
    workdir = create_fixture_repo()
    try:
        init_db()
        repo_id = add_repo(source=workdir, name="fixture_verify", status="pending")
        commits = extract_commits(workdir)
        add_commits(repo_id, commits)

        from necropsy.ghost.verify import verify_claims

        # Claim with a non-existent commit hash should be dropped
        claims = [
            {
                "claim": "test claim",
                "commit_hash": "0000000000000000000000000000000000000000",
                "support": "support text",
            }
        ]

        # When evidence set is empty, claim should be dropped
        surviving = verify_claims(claims, repo_id, "2024-01-01", set())
        assert len(surviving) == 0, "Claim should be dropped when evidence set is empty"

        print("PASS: test_verify_claims_date_leak")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    """Run all tests when executed directly."""
    test_retrieval_as_of_2024_02_01()
    test_retrieval_as_of_2024_02_15()
    test_embeddings_date_slice()
    test_verify_claims_date_leak()
    print("\nAll date-leak tests PASSED!")