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
import hashlib
import numpy as np
from pathlib import Path
from typing import List, Tuple, Optional, Any

from .. import config


def _db_path() -> Path:
    """Resolve the DB path at call time (honours NECROPSY_DB_PATH)."""
    return Path(config.db_path())


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
        conn = sqlite3.connect(str(_db_path()))
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
                INSERT OR REPLACE INTO embeddings
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
        conn = sqlite3.connect(str(_db_path()))
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

        # Items [0:lo] survive the date filter. Slice every list so indices
        # stay aligned and no post-as_of date can be read downstream.
        surviving_refs = item_refs[:lo]
        surviving_vecs = vectors[:lo]
        surviving_ats = item_ats[:lo]

        return surviving_refs, surviving_vecs, surviving_ats
    finally:
        if close:
            conn.close()

