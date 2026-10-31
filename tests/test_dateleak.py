"""Mandatory date-leak test for Necropsy.

Builds a real fixture repo with known commit dates, stores real embeddings in
a temporary database, and asserts that retrieval, the embedding slice, the
prompt and the verifier all stop at as_of.

Nothing dated after as_of may reach the LLM prompt, the verifier, or any
displayed statistic.
"""

import pytest

from necropsy import config
from necropsy.ghost.prompt import build_prompt
from necropsy.ghost.verify import verify_claims
from necropsy.index.retrieve import retrieve
from necropsy.index.store import get_embeddings_by_date

AS_OF_CASES = [
    # as_of, expected surviving subjects
    ("2024-01-01", set()),
    ("2024-01-15", {"Jan initial commit"}),
    ("2024-02-01", {"Jan initial commit"}),
    ("2024-02-10", {"Jan initial commit", "Feb feature add"}),
    ("2024-02-15", {"Jan initial commit", "Feb feature add"}),
    ("2024-03-20", {"Jan initial commit", "Feb feature add", "Mar cleanup pass"}),
]


def test_embeddings_are_really_stored(indexed_repo):
    """The test must not pass vacuously: embeddings have to exist."""
    assert indexed_repo["embeddings"] == 3
    refs, vecs, ats = get_embeddings_by_date(
        indexed_repo["repo_id"], config.EMBED_MODEL, "2024-03-31T23:59:59Z"
    )
    assert len(refs) == 3
    assert vecs.shape == (3, config.VECTOR_DIM)


@pytest.mark.parametrize("as_of,expected", AS_OF_CASES)
def test_retrieval_never_returns_future_commits(indexed_repo, as_of, expected):
    """Retrieval with as_of returns exactly the commits dated <= as_of."""
    results = retrieve(indexed_repo["repo_id"], "parser cleanup feature", as_of, k=10)
    subjects = {r["hash"] for r in results}
    expected_hashes = {
        indexed_repo["by_subject"][s]["hash"] for s in expected
    }
    assert subjects == expected_hashes

    cutoff = config.as_of_end(as_of)
    for result in results:
        assert result["at"] <= cutoff, (
            f"Date-leak: {result['at']} after as_of {as_of}"
        )


@pytest.mark.parametrize("as_of,expected_count", [
    ("2024-01-01", 0),
    ("2024-02-01", 1),
    ("2024-02-15", 2),
    ("2024-03-31", 3),
])
def test_embedding_slice_stops_at_as_of(indexed_repo, as_of, expected_count):
    """The binary-search prefix slice contains only items dated <= as_of."""
    refs, vecs, ats = get_embeddings_by_date(
        indexed_repo["repo_id"], config.EMBED_MODEL, config.as_of_end(as_of)
    )
    assert len(refs) == expected_count
    assert len(ats) == expected_count
    assert vecs.shape[0] == expected_count
    for at in ats:
        assert at <= config.as_of_end(as_of)


def test_prompt_contains_no_future_evidence(indexed_repo):
    """Evidence handed to the model never includes a post-as_of commit."""
    as_of = "2024-02-01"
    results = retrieve(indexed_repo["repo_id"], "feature", as_of, k=10)
    by_hash = {c["hash"]: c for c in indexed_repo["commits"]}
    evidence = [
        {
            "hash": r["hash"],
            "at": r["at"],
            "subject": by_hash[r["hash"]]["subject"],
            "body": by_hash[r["hash"]].get("body", ""),
        }
        for r in results
    ]
    messages = build_prompt(as_of, "What changed?", evidence)
    prompt_text = "\n".join(m["content"] for m in messages)

    mar_hash = indexed_repo["by_subject"]["Mar cleanup pass"]["hash"]
    feb_hash = indexed_repo["by_subject"]["Feb feature add"]["hash"]
    assert mar_hash not in prompt_text
    assert feb_hash not in prompt_text
    assert "2024-03-20" not in prompt_text
    assert "2024-02-10" not in prompt_text


def test_verifier_drops_a_future_commit_even_if_it_leaks(indexed_repo):
    """A claim citing a post-as_of commit is removed even if it is in evidence."""
    jan = indexed_repo["by_subject"]["Jan initial commit"]
    mar = indexed_repo["by_subject"]["Mar cleanup pass"]

    claims = [
        {"claim": "initial readme added", "commit_hash": jan["hash"], "support": "x"},
        {"claim": "cleanup pass", "commit_hash": mar["hash"], "support": "y"},
    ]
    # Simulate the worst case: both hashes are in the evidence set.
    evidence = {jan["hash"], mar["hash"]}
    surviving = verify_claims(claims, indexed_repo["repo_id"], "2024-02-01", evidence)

    surviving_hashes = {c["commit_hash"] for c in surviving}
    assert mar["hash"] not in surviving_hashes
    assert jan["hash"] in surviving_hashes


def test_verifier_drops_claims_outside_the_evidence_set(indexed_repo):
    """A claim citing a commit that was never shown to the model is removed."""
    jan = indexed_repo["by_subject"]["Jan initial commit"]
    claims = [{"claim": "initial readme", "commit_hash": jan["hash"], "support": "x"}]
    assert verify_claims(claims, indexed_repo["repo_id"], "2024-03-31", set()) == []


def test_verifier_drops_unsupported_text(indexed_repo):
    """A claim whose words never appear in the commit is removed."""
    jan = indexed_repo["by_subject"]["Jan initial commit"]
    claims = [{
        "claim": "kubernetes autoscaling ingress controller",
        "commit_hash": jan["hash"],
        "support": "x",
    }]
    surviving = verify_claims(
        claims, indexed_repo["repo_id"], "2024-03-31", {jan["hash"]}
    )
    assert surviving == []
