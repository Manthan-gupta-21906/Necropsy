# necropsy/index/retrieve.py — Date-prefix retrieval over embeddings
# -----------------------------------------------------------------
# Implements the retrieval step of the séance pipeline:
#   retrieve(query, as_of, k) -> top-k evidence commits dated <= as_of
#
# Design invariant: the in-memory matrix is sorted by item_at.
# The as_of filter is a prefix slice found by binary search,
# so later commits are never in the slice that is scored.
# (Filtering after ranking would let future items influence results.)

import numpy as np
from typing import List
from .embed import embed_text
from .store import get_embeddings_by_date
from ..config import RETRIEVAL_K, EMBED_MODEL


def retrieve(
    repo_id: int,
    query: str,
    as_of: str,
    k: int = RETRIEVAL_K,
    model_name: str = EMBED_MODEL,
) -> List[dict]:
    """Retrieve top-k commits relevant to query, dated <= as_of.

    Returns list of dicts with:
        hash   : commit hash (item_ref)
        ref    : commit hash (alias)
        at     : authored_at date string
        similarity : cosine similarity (dot product, vectors are normalized)

    The date filter uses binary search on the sorted item_at list:
    only items with item_at <= as_of_end are in the scored set.
    """
    # Use store-level date-filter + similarity
    # First, get the date-filtered set
    surviving_refs, surviving_vecs, item_ats = get_embeddings_by_date(
        repo_id, model_name, as_of
    )

    if len(surviving_vecs) == 0:
        return []

    # Encode query
    query_vec = np.array(embed_text(query, model_name), dtype=np.float32)
    norm = np.linalg.norm(query_vec)
    if norm > 0:
        query_vec = query_vec / norm

    # Cosine similarity = dot product (vectors are L2-normalized)
    sims = surviving_vecs @ query_vec  # shape (n,)

    # Top-k
    n = len(surviving_vecs)
    k = min(k, n)
    top_indices = np.argsort(-sims)[:k]

    results = []
    for i, idx in enumerate(top_indices):
        results.append({
            "hash": surviving_refs[idx],
            "ref": surviving_refs[idx],
            "at": item_ats[idx] if idx < len(item_ats) else "",
            "similarity": float(sims[idx]),
        })

    return results