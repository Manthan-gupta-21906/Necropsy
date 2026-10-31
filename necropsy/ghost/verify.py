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

import logging
import re
import sqlite3
from typing import List, Dict, Any, Set

from .. import config
from ..db import commit_exists

logger = logging.getLogger(__name__)

# Threshold for keyword-match support: at least 30% of claim words must
# appear in the commit's subject+body text.
SUPPORT_RATIO = 0.3

_WORD_RE = re.compile(r"[a-z0-9_]+")

# Very common words that carry no evidential weight.
_STOPWORDS = {
    "the", "and", "that", "this", "with", "from", "were", "was", "have",
    "has", "had", "been", "into", "then", "than", "they", "their", "there",
    "which", "when", "what", "where", "while", "also", "just", "only",
    "some", "such", "more", "most", "very", "much", "will", "would",
    "could", "should", "about", "after", "before", "because", "commit",
}


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

    Returns the claims that passed every check, in original order.
    """
    if not claims:
        return []

    surviving: List[Dict[str, Any]] = []
    cutoff = config.as_of_end(as_of)

    conn = sqlite3.connect(config.db_path())
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            commit_hash = str(claim.get("commit_hash", "")).strip()
            claim_text = str(claim.get("claim", "")).strip()
            if not commit_hash:
                continue

            # Check 1: commit exists in this repo.
            if not commit_exists(repo_id, commit_hash):
                logger.debug("Claim dropped: commit %s not in repo %s", commit_hash, repo_id)
                continue

            # Check 2: commit dated <= as_of.
            row = conn.execute(
                "SELECT authored_at FROM commits WHERE repo_id = ? AND hash = ?",
                (repo_id, commit_hash),
            ).fetchone()
            if not row:
                continue
            authored_at = row[0] or ""
            if authored_at > cutoff:
                logger.debug("Claim dropped: commit %s dated after %s", commit_hash, as_of)
                continue

            # Check 3: commit was in the evidence set shown to the model.
            if commit_hash not in evidence_hashes:
                logger.debug("Claim dropped: commit %s not in evidence set", commit_hash)
                continue

            # Check 4: the commit text supports the claim.
            if _is_supported(claim_text, repo_id, commit_hash, conn):
                surviving.append(claim)
            else:
                logger.debug("Claim dropped: no textual support in %s", commit_hash)
    finally:
        conn.close()

    return surviving


def _tokens(text: str) -> Set[str]:
    """Lowercase word tokens, minus stopwords and very short words."""
    return {
        w for w in _WORD_RE.findall((text or "").lower())
        if len(w) > 3 and w not in _STOPWORDS
    }


def _is_supported(
    claim: str,
    repo_id: int,
    commit_hash: str,
    conn: sqlite3.Connection,
) -> bool:
    """True when the claim's words overlap the commit text enough.

    Heuristic keyword overlap; the primary verifier checks are hash, date and
    evidence membership. This only removes claims that talk about something
    the commit never mentions.
    """
    row = conn.execute(
        "SELECT subject, body FROM commits WHERE repo_id = ? AND hash = ?",
        (repo_id, commit_hash),
    ).fetchone()
    if not row:
        return False

    text_tokens = _tokens(f"{row[0] or ''} {row[1] or ''}")
    claim_tokens = _tokens(claim)
    if not claim_tokens:
        return False

    matched = claim_tokens & text_tokens
    return (len(matched) / len(claim_tokens)) > SUPPORT_RATIO
