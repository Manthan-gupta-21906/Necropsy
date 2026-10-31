# necropsy/ghost/verify.py — Citation verification and claim pruning
# ------------------------------------------------------------------
# Responsibilities:
#   • Verify each cited commit: exists, dated <= as_of, in evidence set
#   • Check that each claim is supported by the commit text
#   • Remove unsupported claims
#   • If no claims survive, return empty list
#
# Design invariant: verification is deterministic code, not the model.
# The verifier runs after generation and prunes any claim that fails checks.

import json
import logging
import os
import sqlite3
from typing import List, Dict, Any, Set

from ..db import commit_exists

logger = logging.getLogger(__name__)

# Threshold for keyword-match support: at least 30% of claim words must
# appear in the commit's subject+body text.
SUPPORT_RATIO = 0.3


def verify_claims(
    claims: List[Dict[str, Any]],
    repo_id: int,
    as_of: str,
    evidence_hashes: Set[str],
) -> List[Dict[str, Any]]:
    """Verify and prune claims against the database and evidence set.

    For each claim, checks:
        1. The commit_hash exists in the commits table for this repo
        2. The commit's authored_at <= as_of (end-of-day UTC)
        3. The commit_hash is in the evidence set shown to the model
        4. The commit's subject/body actually supports the claim

    Args:
        claims: list of claim dicts from the LLM output
        repo_id: the repo database id
        as_of: the selected date (end-of-day UTC)
        evidence_hashes: set of commit hashes that were shown to the model

    Returns:
        List of claims that passed all verification checks, in original order.
        If no claims survive, returns [].
    """
    if not claims:
        return []

    surviving = []
    cutoff = as_of if as_of.endswith("Z") else f"{as_of}Z"

    conn = sqlite3.connect(
        str(__import__("pathlib").Path(os.getcwd()) / "data/necropsy.db")
    )
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        for claim in claims:
            commit_hash = claim.get("commit_hash", "").strip()
            claim_text = claim.get("claim", "").strip()

            # Check 1: commit exists in repo
            if not commit_exists(repo_id, commit_hash):
                logger.debug(
                    f"Claim dropped: commit {commit_hash} not in repo {repo_id}"
                )
                continue

            # Check 2: commit dated <= as_of
            row = conn.execute(
                "SELECT authored_at FROM commits WHERE repo_id = ? AND hash = ?",
                (repo_id, commit_hash)
            ).fetchone()
            if not row:
                continue
            authored_at = row[0]

            if authored_at > cutoff:
                logger.debug(
                    f"Claim dropped: commit {commit_hash} dated after {as_of}"
                )
                continue

            # Check 3: commit is in the evidence set shown to the model
            if commit_hash not in evidence_hashes:
                logger.debug(
                    f"Claim dropped: commit {commit_hash} not in evidence set"
                )
                continue

            # Check 4: claim is supported by the commit text
            if _is_supported(claim_text, commit_hash, conn):
                surviving.append(claim)
            else:
                logger.debug(
                    f"Claim dropped: no textual support in commit {commit_hash}"
                )
                continue
    finally:
        conn.close()

    return surviving


def _is_supported(claim: str, commit_hash: str, conn: sqlite3.Connection) -> bool:
    """Check if a claim is supported by a commit's text.

    Uses simple keyword overlap between the claim and the commit's
    subject+body. This is a heuristic; the verification step's primary
    role is to check date/hash/evidence membership.

    Returns True if the claim appears to be supported.
    """
    row = conn.execute(
        "SELECT subject, body FROM commits WHERE hash = ?",
        (commit_hash,)
    ).fetchone()

    if not row:
        return False

    subject, body = row[0] or "", row[1] or ""

    # Combine subject and body for matching
    text = f"{subject} {body}".lower()

    # Tokenize claim into words (simple split)
    claim_words = set(w for w in claim.lower().split() if len(w) > 3)

    if not claim_words:
        return False

    # Check if SUPPORT_RATIO of claim words appear in the commit text
    matched = claim_words & set(text.split())
    ratio = len(matched) / len(claim_words) if claim_words else 0

    return ratio > SUPPORT_RATIO