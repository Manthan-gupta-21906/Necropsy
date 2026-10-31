# necropsy/index/store.py — Persist and retrieve embeddings from SQLite
# -------------------------------------------------------------------
# Responsibilities:
#   • Store embeddings (vector, metadata) in SQLite embeddings table
#   • Load embeddings sorted by date for prefix-slicing retrieval
#   • Binary search for as_of date cutoff
#
# Design invariant: vectors are stored with item_at; retrieval uses
#   binary search over the sorted item_at list to find the date prefix.
#   Later commits are never in the scored set.

import json
import sqlite3
import numpy as np
from pathlib import Path
from typing import List, Tuple, Optional, Any

DATA_DIR = Path("data")
DB_PATH = Path("data/necropsy.db")


def _ensure_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def store_embeddings(
    repo_id: int,
    items: List[dict],
    model: str,
    vectors: List[List[float]],
    conn: sqlite3.Connection = None
) -> int:
    """Store embeddings for a repo in the embeddings table.

    Args:
        repo_id: database repo identifier
        items: list of dicts with 'item_ref', 'item_at', and optionally 'text_hash'
        model: embedding model name + version
        vectors: list of float lists (will be L2-normalized to 384 dim)
        conn: optional existing sqlite3.Connection

    Returns:
        Number of embeddings stored.
    """
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close = True
    else:
        close = False

    try:
        _ensure_conn(conn)
        added = 0
        for item, vector in zip(items, vectors):
            import numpy as np
            vec_arr = np.array(vector, dtype=np.float32)

            # L2-normalize
            norm = np.linalg.norm(vec_arr)
            if norm > 0:
                vec_arr = vec_arr / norm
            else:
                # zero vector - fill with small values
                vec_arr = np.zeros(384, dtype=np.float32)

            # Ensure 384 dimensions
            if vec_arr.shape[0] != 384:
                # resize/pad
                if vec_arr.shape[0] < 384:
                    padded = np.zeros(384, dtype=np.float32)
                    padded[:vec_arr.shape[0]] = vec_arr
                    vec_arr = padded
                else:
                    vec_arr = vec_arr[:384]

            vector_bytes = vec_arr.tobytes()

            # text_hash if not provided
            text_hash = item.get("text_hash")
            if not text_hash:
                combined = f"{item.get('item_ref', '')}|{item.get('item_at', '')}"
                text_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()

            item_type = item.get("item_type", "commit")
            item_ref = item.get("item_ref", "")
            item_at = item.get("item_at", "")

            conn.execute("""
                INSERT OR IGNORE INTO embeddings
                (repo_id, item_type, item_ref, item_at, text_hash, model, dim, vector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                repo_id,
                item_type,
                item_ref,
                item_at,
                text_hash,
                model,
                len(vec_arr),
                sqlite3.Binary(vector_bytes),
            ))
            added += 1

        conn.commit()
        return added
    finally:
        if close:
            conn.close()


def get_embeddings_by_date(
    repo_id: int,
    model: str,
    as_of_end: str,
    conn: sqlite3.Connection = None
) -> Tuple[List[str], List[str], np.ndarray]:
    """Retrieve embeddings dated <= as_of_end, sorted by item_at.

    Uses binary search to find the prefix slice of items whose
    item_at <= as_of_end. Items after the cutoff are excluded.

    Returns:
        (item_refs, item_ats, vectors) where vectors is a numpy array
        of shape (n_surviving, dim) and item_refs/ats are lists of the
        corresponding references and dates.
    """
    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close = True
    else:
        close = False

    try:
        _ensure_conn(conn)
        rows = conn.execute("""
            SELECT item_ref, item_at, vector
            FROM embeddings
            WHERE repo_id = ? AND model = ?
            ORDER BY item_at
        """, (repo_id, model)).fetchall()

        if not rows:
            return [], [], np.array([], dtype=np.float32)

        item_refs = [row[0] for row in rows]
        item_ats = [row[1] for row in rows]
        # vectors stored as BLOB float32; load each
        vectors = []
        for row in rows:
            arr = np.frombuffer(row[2], dtype=np.float32)
            vectors.append(arr)
        vectors = np.array(vectors, dtype=np.float32)  # shape (n, dim)

        # Binary search: find last index where item_at <= as_of_end
        # ISO-8601 text sorts correctly as-is for UTC dates
        cutoff = as_of_end if as_of_end.endswith("Z") else f"{as_of_end}Z"

        lo, hi = 0, len(item_ats)
        while lo < hi:
            mid = (lo + hi) // 2
            if item_ats[mid] <= cutoff:
                lo = mid + 1
            else:
                hi = mid

        # Items [0:lo] survive the date filter
        surviving_refs = item_refs[:lo]
        surviving_vecs = vectors[:lo]

        return surviving_refs, surviving_vecs, item_ats
    finally:
        if close:
            conn.close()


def retrieve(
    repo_id: int,
    query: str,
    as_of: str,
    model_name: str = None,
    conn: sqlite3.Connection = None,
) -> List[dict]:
    """Retrieve top-k relevant commits dated <= as_of for a query.

    Pipeline:
        1. Encode query text
        2. Get date-filtered embeddings (prefix slice by binary search)
        3. Compute cosine similarities (brute-force dot product)
        4. Return top-k with refs, dates, similarities

    Args:
        repo_id: repo database id
        query: user question text
        as_of: date string (end-of-day UTC)
        k: number of results to return
        model_name: embedding model; defaults to config EMBED_MODEL
        conn: optional existing sqlite3.Connection

    Returns:
        List of dicts: {hash, ref, at, similarity}
        sorted by similarity descending, all dated <= as_of.
    """
    import numpy as np
    from .embed import embed_text, compute_embeddings

    if model_name is None:
        from .config import EMBED_MODEL
        model_name = EMBED_MODEL

    if conn is None:
        conn = sqlite3.connect(str(DB_PATH))
        close = True
    else:
        close = False

    try:
        _ensure_conn(conn)

        # Step 1: encode query
        query_vec = np.array(embed_text(query, model_name), dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec) if np.linalg.norm(query_vec) > 0 else query_vec

        # Step 2: get date-filtered embeddings
        surviving_refs, surviving_vecs, all_ats = get_embeddings_by_date(
            repo_id, model_name, as_of, conn
        )

        if len(surviving_vecs) == 0:
            return []

        # Step 3: brute-force cosine similarity via dot product
        # Both query_vec and surviving_vecs are L2-normalized
        sims = surviving_vecs @ query_vec  # shape (n,)

        # Get top-k indices
        n = len(surviving_vecs)
        k = min(k, n)
        top_indices = np.argsort(-sims)[:k]  # descending

        # Step 4: build result list
        results = []
        for idx in top_indices:
            hash_val = surviving_refs[idx]  # item_ref is the commit hash
            at = all_ats[idx] if idx < len(all_ats) else ""
            similarity = float(sims[idx])
            results.append({
                "hash": hash_val,
                "ref": hash_val,
                "at": at,
                "similarity": similarity,
            })

        return results
    finally:
        if close:
            conn.close()