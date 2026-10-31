# necropsy/db.py — SQLite database operations
# ------------------------------------------
# Single connection to DATA_DIR/necropsy.db.
# All tables follow the DATA_SCHEMA.md DDL.
# Privacy: raw author emails stored only as sha256(lowercase(email)).
#
# Time-travel convention: as_of is the end of that day in UTC.
# Queries use authored_at <= :as_of_end.

import sqlite3
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

from . import config


def _db_path() -> Path:
    """Resolve the DB path at call time (honours NECROPSY_DB_PATH)."""
    return Path(config.db_path())


def _ensure_data_dir():
    """Ensure the data directory exists."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    """Get a connected SQLite instance with enforced FK and correct pragmas."""
    _ensure_data_dir()
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(schema_version: int = 1) -> None:
    """Initialize the database with the required schema.

    Creates all tables defined in DATA_SCHEMA.md if they don't exist.
    """
    conn = get_connection()
    try:
        _create_meta_table(conn)
        _create_repos_table(conn)
        _create_authors_table(conn)
        _create_commits_table(conn)
        _create_files_changed_table(conn)
        _create_symbols_table(conn)
        _create_tombstones_table(conn)
        _create_issues_table(conn)
        _create_embeddings_table(conn)
        _create_eras_table(conn)  # Also creates era_points table
        _create_seance_log_table(conn)
        _create_eval_runs_table(conn)
        _create_eval_results_table(conn)
        _update_schema_version(conn, schema_version)
        conn.commit()
    finally:
        conn.close()


def _create_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    val = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if not val:
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")


def _create_repos_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS repos (
            id              INTEGER PRIMARY KEY,
            name            TEXT NOT NULL,
            source          TEXT NOT NULL,
            local_path      TEXT NOT NULL,
            default_branch  TEXT,
            head_sha        TEXT NOT NULL,
            first_commit_at TEXT NOT NULL,
            last_commit_at  TEXT NOT NULL,
            commit_count    INTEGER NOT NULL,
            ingested_at     TEXT NOT NULL,
            status          TEXT NOT NULL
                CHECK (status IN ('pending','ingesting','indexing','ready','failed')),
            progress        REAL NOT NULL DEFAULT 0,
            error           TEXT,
            UNIQUE (source)
        )
    """)


def _create_authors_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            id           INTEGER PRIMARY KEY,
            repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            email_hash   TEXT NOT NULL,
            is_bot       INTEGER NOT NULL DEFAULT 0 CHECK (is_bot IN (0,1)),
            UNIQUE (repo_id, email_hash)
        )
    """)


def _create_commits_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commits (
            repo_id             INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            hash                TEXT NOT NULL,
            author_id           INTEGER NOT NULL REFERENCES authors(id),
            authored_at         TEXT NOT NULL,
            committed_at        TEXT NOT NULL,
            subject             TEXT NOT NULL,
            body                TEXT NOT NULL DEFAULT '',
            additions           INTEGER NOT NULL DEFAULT 0,
            deletions           INTEGER NOT NULL DEFAULT 0,
            files_changed_count INTEGER NOT NULL DEFAULT 0,
            is_merge            INTEGER NOT NULL DEFAULT 0 CHECK (is_merge IN (0,1)),
            parents             TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (repo_id, hash)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commits_time ON commits (repo_id, authored_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_commits_author ON commits (repo_id, author_id, authored_at)")


def _create_files_changed_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files_changed (
            id          INTEGER PRIMARY KEY,
            repo_id     INTEGER NOT NULL,
            commit_hash TEXT NOT NULL,
            path        TEXT NOT NULL,
            old_path    TEXT,
            change_type TEXT NOT NULL CHECK (change_type IN ('A','M','D','R','C')),
            additions   INTEGER NOT NULL DEFAULT 0,
            deletions   INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (repo_id, commit_hash) REFERENCES commits(repo_id, hash) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_commit ON files_changed (repo_id, commit_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_path ON files_changed (repo_id, path)")


def _create_symbols_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            id           INTEGER PRIMARY KEY,
            repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            file_path    TEXT NOT NULL,
            name         TEXT NOT NULL,
            kind         TEXT NOT NULL CHECK (kind IN ('function','class','method')),
            language     TEXT NOT NULL,
            signature    TEXT,
            born_commit  TEXT NOT NULL,
            born_at      TEXT NOT NULL,
            died_at      TEXT,
            UNIQUE (repo_id, file_path, name, kind, born_commit)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_life ON symbols (repo_id, born_at, died_at)")


def _create_tombstones_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tombstones (
            id             INTEGER PRIMARY KEY,
            repo_id        INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            kind           TEXT NOT NULL CHECK (kind IN ('file','symbol')),
            path           TEXT NOT NULL,
            name           TEXT,
            symbol_id      INTEGER REFERENCES symbols(id),
            born_commit    TEXT NOT NULL,
            born_at        TEXT NOT NULL,
            died_commit    TEXT NOT NULL,
            died_at        TEXT NOT NULL,
            died_author_id INTEGER REFERENCES authors(id),
            epitaph        TEXT NOT NULL,
            lifespan_days  INTEGER NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tomb_died ON tombstones (repo_id, died_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tomb_born ON tombstones (repo_id, born_at)")


def _create_issues_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS issues (
            id               INTEGER PRIMARY KEY,
            repo_id          INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            number           INTEGER NOT NULL,
            title            TEXT NOT NULL,
            body             TEXT NOT NULL DEFAULT '',
            author_login     TEXT,
            created_at       TEXT NOT NULL,
            closed_at        TEXT,
            last_activity_at TEXT,
            comment_count    INTEGER NOT NULL DEFAULT 0,
            UNIQUE (repo_id, number)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_time ON issues (repo_id, created_at)")


def _create_embeddings_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id        INTEGER PRIMARY KEY,
            repo_id   INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            item_type TEXT NOT NULL CHECK (item_type IN ('commit','issue','doc')),
            item_ref  TEXT NOT NULL,
            item_at   TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            model     TEXT NOT NULL,
            dim       INTEGER NOT NULL,
            vector    BLOB NOT NULL,
            UNIQUE (repo_id, item_type, item_ref, model)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emb_time ON embeddings (repo_id, model, item_at)")


def _create_eras_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eras (
            id           INTEGER PRIMARY KEY,
            repo_id      INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            idx          INTEGER NOT NULL,
            label        TEXT,
            summary      TEXT,
            start_at     TEXT NOT NULL,
            end_at       TEXT NOT NULL,
            commit_count INTEGER NOT NULL,
            algo         TEXT NOT NULL,
            params       TEXT NOT NULL DEFAULT '{}',
            model        TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS era_points (
            repo_id     INTEGER NOT NULL,
            commit_hash TEXT NOT NULL,
            era_id      INTEGER NOT NULL REFERENCES eras(id) ON DELETE CASCADE,
            x           REAL NOT NULL,
            y           REAL NOT NULL,
            PRIMARY KEY (repo_id, commit_hash),
            FOREIGN KEY (repo_id, commit_hash) REFERENCES commits(repo_id, hash) ON DELETE CASCADE
        )
    """)


def _create_seance_log_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seance_log (
            id              INTEGER PRIMARY KEY,
            repo_id         INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            as_of           TEXT NOT NULL,
            question        TEXT NOT NULL,
            config_hash     TEXT NOT NULL,
            llm_model       TEXT NOT NULL,
            evidence_hashes TEXT NOT NULL,
            raw_response    TEXT,
            final_response  TEXT NOT NULL,
            claims_total    INTEGER NOT NULL,
            claims_kept     INTEGER NOT NULL,
            latency_ms      INTEGER,
            created_at      TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seance_cache ON seance_log (repo_id, as_of, config_hash)")


def _create_eval_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_runs (
            id         INTEGER PRIMARY KEY,
            repo_id    INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
            llm_model  TEXT NOT NULL,
            config     TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
    """)


def _create_eval_results_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_results (
            id              INTEGER PRIMARY KEY,
            run_id          INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
            category        TEXT NOT NULL,
            as_of           TEXT NOT NULL,
            question        TEXT NOT NULL,
            expected        TEXT NOT NULL,
            got             TEXT,
            answer_correct  INTEGER CHECK (answer_correct IN (0,1)),
            citations_valid_before_verify REAL,
            citations_valid_after_verify  REAL,
            latency_ms      INTEGER
        )
    """)


def _update_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute("""
        INSERT INTO meta (key, value)
        VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (str(version),))


def repo_exists(source: str) -> bool:
    """Check if a repo source is already in the database."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM repos WHERE source = ?",
            (source.strip(),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_repo(
    source: str,
    name: str,
    status: str = "pending",
    local_path: Optional[str] = None
) -> int:
    """Add a new repo row and return its id."""
    conn = get_connection()
    try:
        source_hash = hashlib.sha256(source.strip().lower().encode()).hexdigest()[:8]
        local = local_path or f"clones/{source_hash}"
        
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        conn.execute("""
            INSERT INTO repos (name, source, local_path, status, ingested_at, last_commit_at, first_commit_at, commit_count, head_sha)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            source.strip(),
            local,
            status,
            now,
            now,
            now,
            0,
            hashlib.sha256(source.strip().encode()).hexdigest()[:8]
        ))
        
        repo_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        return repo_id
    finally:
        conn.close()


def update_repo_status(repo_id: int, status: str, error: Optional[str] = None, progress: float = 0.0) -> None:
    """Update a repo's status row."""
    conn = get_connection()
    try:
        updates = ["status = ?, progress = ?"]
        params = [status, progress]
        if error is not None:
            updates.append("error = ?")
            params.append(error)
        params.append(repo_id)
        
        query = f"UPDATE repos SET {', '.join(updates)} WHERE id = ?"
        conn.execute(query, params)
        conn.commit()
    finally:
        conn.close()


def get_repo(repo_id: int) -> Optional[dict]:
    """Get repo data by id."""
    conn = get_connection()
    try:
        cur = conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def get_repo_by_source(source: str) -> Optional[dict]:
    """Get repo data by its (unique) source."""
    conn = get_connection()
    try:
        cur = conn.execute("SELECT * FROM repos WHERE source = ?", (source.strip(),))
        row = cur.fetchone()
        if not row:
            return None
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))
    finally:
        conn.close()


def reset_repo(repo_id: int) -> None:
    """Delete all ingested data for a repo so it can be re-indexed in place."""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM embeddings WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM tombstones WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM symbols WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM era_points WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM eras WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM commits WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM authors WHERE repo_id = ?", (repo_id,))
        conn.commit()
    finally:
        conn.close()


def finalize_repo(
    repo_id: int,
    head_sha: str,
    first_commit_at: str,
    last_commit_at: str,
    commit_count: int,
    local_path: str,
    status: str = "ready",
) -> None:
    """Write the final, real metadata for a repo after indexing."""
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE repos
               SET head_sha = ?, first_commit_at = ?, last_commit_at = ?,
                   commit_count = ?, local_path = ?, status = ?, progress = 1.0,
                   error = NULL
             WHERE id = ?
            """,
            (
                head_sha,
                first_commit_at,
                last_commit_at,
                commit_count,
                local_path,
                status,
                repo_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def repo_range(repo_id: int) -> dict:
    """Return the real commit-date bounds for a repo (for the time slider)."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT MIN(authored_at), MAX(authored_at), COUNT(*) "
            "FROM commits WHERE repo_id = ?",
            (repo_id,),
        ).fetchone()
        return {
            "repo_id": repo_id,
            "first_commit_at": row[0] if row else None,
            "last_commit_at": row[1] if row else None,
            "commit_count": row[2] if row else 0,
        }
    finally:
        conn.close()


def get_commits_by_hashes(repo_id: int, hashes: List[str]) -> Dict[str, dict]:
    """Return commit text/date for the given hashes, keyed by hash."""
    if not hashes:
        return {}
    conn = get_connection()
    try:
        placeholders = ",".join("?" for _ in hashes)
        rows = conn.execute(
            f"SELECT hash, subject, body, authored_at FROM commits "
            f"WHERE repo_id = ? AND hash IN ({placeholders})",
            (repo_id, *hashes),
        ).fetchall()
        return {
            r[0]: {"subject": r[1], "body": r[2], "at": r[3]}
            for r in rows
        }
    finally:
        conn.close()


def list_repos() -> List[dict]:
    """List all repos with the date bounds the time slider needs."""
    conn = get_connection()
    try:
        cur = conn.execute(
            "SELECT id, name, source, status, commit_count, ingested_at, "
            "       first_commit_at, last_commit_at, progress, error "
            "FROM repos ORDER BY id"
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def commit_exists(repo_id: int, commit_hash: str) -> bool:
    """Check if a commit exists in the database for a given repo."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT 1 FROM commits WHERE repo_id = ? AND hash = ?",
            (repo_id, commit_hash)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def add_commits(repo_id: int, commits: List[dict]) -> int:
    """Add commits to the database. Returns number added."""
    conn = get_connection()
    added = 0
    try:
        for c in commits:
            author_hash = hashlib.sha256(c.get("author_email", "").lower().encode()).hexdigest() if c.get("author_email") else ""
            
            author_row = conn.execute(
                "SELECT id FROM authors WHERE repo_id = ? AND email_hash = ?",
                (repo_id, author_hash)
            ).fetchone()
            if not author_row:
                conn.execute("""
                    INSERT INTO authors (repo_id, display_name, email_hash, is_bot)
                    VALUES (?, ?, ?, ?)
                """, (repo_id, c.get("author_name", "Unknown"), author_hash, c.get("author_is_bot", 0)))
            
            author_id = conn.execute(
                "SELECT id FROM authors WHERE repo_id = ? AND email_hash = ?",
                (repo_id, author_hash)
            ).fetchone()[0]
            
            authored_at = c.get("authored_at", "")
            committed_at = c.get("committed_at", "")
            
            conn.execute("""
                INSERT OR IGNORE INTO commits (repo_id, hash, author_id, authored_at, committed_at, subject, body, additions, deletions, files_changed_count, is_merge, parents)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                repo_id,
                c.get("hash", ""),
                author_id,
                authored_at,
                committed_at,
                c.get("subject", "")[:200],
                c.get("body", "")[:10000],
                c.get("additions", 0),
                c.get("deletions", 0),
                c.get("files_changed_count", 0),
                c.get("is_merge", 0),
                json.dumps(c.get("parents", []))
            ))
            added += 1
        
        conn.commit()
        return added
    finally:
        conn.close()


def add_file_changes(repo_id: int, file_records: List[dict]) -> int:
    """Add file change records. Returns number added."""
    conn = get_connection()
    added = 0
    try:
        for f in file_records:
            conn.execute("""
                INSERT OR IGNORE INTO files_changed (repo_id, commit_hash, path, old_path, change_type, additions, deletions)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                repo_id,
                f.get("commit_hash", ""),
                f.get("path", ""),
                f.get("old_path", ""),
                f.get("change_type", "M"),
                f.get("additions", 0),
                f.get("deletions", 0),
            ))
            added += 1
        conn.commit()
        return added
    finally:
        conn.close()


def add_tombstones(repo_id: int, tombstones: List[dict]) -> int:
    """Add tombstone records. Returns number added."""
    conn = get_connection()
    added = 0
    try:
        for t in tombstones:
            conn.execute("""
                INSERT OR IGNORE INTO tombstones (repo_id, kind, path, name, symbol_id, born_commit, born_at, died_commit, died_at, died_author_id, epitaph, lifespan_days)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                repo_id,
                t.get("kind", "file"),
                t.get("path", ""),
                t.get("name", ""),
                t.get("symbol_id"),
                t.get("born_commit", ""),
                t.get("born_at", ""),
                t.get("died_commit", ""),
                t.get("died_at", ""),
                t.get("died_author_id"),
                t.get("epitaph", ""),
                t.get("lifespan_days", 0),
            ))
            added += 1
        conn.commit()
        return added
    finally:
        conn.close()


def add_embeddings(
    repo_id: int,
    items: List[dict],
    model: str,
    vectors: List[List[float]]
) -> int:
    """Add embeddings to the database."""
    import numpy as np
    conn = get_connection()
    added = 0
    try:
        for item, vector in zip(items, vectors):
            vec_arr = np.array(vector, dtype=np.float32)
            norm = np.linalg.norm(vec_arr)
            if norm > 0:
                vec_arr = vec_arr / norm
            else:
                vec_arr = np.zeros(384, dtype=np.float32)
            
            if vec_arr.shape[0] != 384:
                if vec_arr.shape[0] < 384:
                    padded = np.zeros(384, dtype=np.float32)
                    padded[:vec_arr.shape[0]] = vec_arr
                    vec_arr = padded
                else:
                    vec_arr = vec_arr[:384]
            
            vector_bytes = vec_arr.tobytes()
            
            text_hash = item.get("text_hash")
            if not text_hash:
                combined = f"{item.get('item_ref', '')}|{item.get('item_at', '')}"
                text_hash = hashlib.sha256(combined.encode("utf-8")).hexdigest()
            
            item_type = item.get("item_type", "commit")
            item_ref = item.get("item_ref", "")
            item_at = item.get("item_at", "")
            
            conn.execute("""
                INSERT OR IGNORE INTO embeddings (repo_id, item_type, item_ref, item_at, text_hash, model, dim, vector)
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
        conn.close()


def get_embeddings_by_date(
    repo_id: int,
    model: str,
    as_of_end: str,
    conn: sqlite3.Connection = None
) -> tuple:
    """Retrieve embeddings dated <= as_of_end, sorted by item_at."""
    import numpy as np
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
            return [], [], []
        
        item_refs = [row[0] for row in rows]
        item_ats = [row[1] for row in rows]
        vectors = []
        for row in rows:
            arr = np.frombuffer(row[2], dtype=np.float32)
            vectors.append(arr)
        vectors = np.array(vectors, dtype=np.float32)
        
        cutoff = as_of_end if as_of_end.endswith("Z") else f"{as_of_end}Z"
        
        lo, hi = 0, len(item_ats)
        while lo < hi:
            mid = (lo + hi) // 2
            if item_ats[mid] <= cutoff:
                lo = mid + 1
            else:
                hi = mid
        
        surviving_refs = item_refs[:lo]
        surviving_vecs = vectors[:lo]
        
        return surviving_refs, surviving_vecs, item_ats
    finally:
        if close:
            conn.close()


def _ensure_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def retrieve(
    repo_id: int,
    query: str,
    as_of: str,
    k: int = 10,
    model_name: str = None,
    conn: sqlite3.Connection = None,
) -> List[dict]:
    """Retrieve top-k commits relevant to query, dated <= as_of."""
    import numpy as np
    from .embed import embed_text
    
    if model_name is None:
        from .config import EMBED_MODEL
        model_name = EMBED_MODEL
    
    if conn is None:
        conn = sqlite3.connect(str(_db_path()))
        close = True
    else:
        close = False
    
    try:
        _ensure_conn(conn)
        
        query_vec = np.array(embed_text(query, model_name), dtype=np.float32)
        norm = np.linalg.norm(query_vec)
        if norm > 0:
            query_vec = query_vec / norm
        
        surviving_refs, surviving_vecs, all_ats = get_embeddings_by_date(
            repo_id, model_name, as_of, conn
        )
        
        if len(surviving_vecs) == 0:
            return []
        
        sims = surviving_vecs @ query_vec
        
        n = len(surviving_vecs)
        k = min(k, n)
        top_indices = np.argsort(-sims)[:k]
        
        results = []
        for i, idx in enumerate(top_indices):
            results.append({
                "hash": surviving_refs[idx],
                "ref": surviving_refs[idx],
                "at": all_ats[idx] if idx < len(all_ats) else "",
                "similarity": float(sims[idx]),
            })
        
        return results
    finally:
        if close:
            conn.close()


def log_seance(
    repo_id: int,
    as_of: str,
    question: str,
    config_hash: str,
    llm_model: str,
    evidence_hashes: List[str],
    raw_response: str,
    final_response: str,
    claims_total: int,
    claims_kept: int,
    latency_ms: int
) -> int:
    """Log a seance to the database. Returns the log id."""
    conn = get_connection()
    try:
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        evidence_json = json.dumps(evidence_hashes)
        
        row = conn.execute("""
            INSERT INTO seance_log (repo_id, as_of, question, config_hash, llm_model, evidence_hashes, raw_response, final_response, claims_total, claims_kept, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            repo_id,
            as_of,
            question,
            config_hash,
            llm_model,
            evidence_json,
            raw_response,
            final_response,
            claims_total,
            claims_kept,
            latency_ms,
            created,
        ))
        
        seance_id = row.lastrowid
        conn.commit()
        return seance_id
    finally:
        conn.close()


def log_eval_run(
    repo_id: int,
    llm_model: str,
    config: dict
) -> int:
    """Log an evaluation run. Returns the run id."""
    conn = get_connection()
    try:
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        config_json = json.dumps(config)
        
        row = conn.execute("""
            INSERT INTO eval_runs (repo_id, llm_model, config, started_at)
            VALUES (?, ?, ?, ?)
        """, (repo_id, llm_model, config_json, started))
        
        run_id = row.lastrowid
        conn.commit()
        return run_id
    finally:
        conn.close()


def add_eval_result(
    run_id: int,
    category: str,
    as_of: str,
    question: str,
    expected: str,
    got: Optional[str],
    answer_correct: int,
    citations_valid_before_verify: float,
    citations_valid_after_verify: float,
    latency_ms: int
) -> int:
    """Add an evaluation result."""
    conn = get_connection()
    try:
        row = conn.execute("""
            INSERT INTO eval_results (run_id, category, as_of, question, expected, got, answer_correct, citations_valid_before_verify, citations_valid_after_verify, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            category,
            as_of,
            question,
            expected,
            got,
            answer_correct,
            citations_valid_before_verify,
            citations_valid_after_verify,
            latency_ms,
        ))
        
        result_id = row.lastrowid
        conn.commit()
        return result_id
    finally:
        conn.close()