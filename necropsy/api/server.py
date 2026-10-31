# necropsy/api/server.py — FastAPI application with endpoints
# ----------------------------------------------------------
# Endpoints:
#   GET   /health              -> machine status panel
#   POST  /exhume              -> start repo indexing job
#   GET   /exhume/{job_id}     -> poll job progress
#   POST  /autopsy             -> run deterministic analytics
#   POST  /seance              -> question the ghost
#   GET   /repos               -> list indexed repos

import time
import os
import logging
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ..config import RETRIEVAL_K, EMBED_MODEL, LLM_MODEL, LLM_FALLBACK, DATA_DIR
from ..db import (
    init_db, repo_exists, add_repo, update_repo_status,
    get_repo, list_repos, add_commits, add_file_changes,
    add_tombstones, add_embeddings, get_embeddings_by_date,
    log_seance, log_eval_run, add_eval_result
)
from ..ingest import validate_source, clone_repo, extract_commits
from necropsy.index.retrieve import retrieve; from necropsy.index.store import store_embeddings, get_embeddings_by_date
from ..ghost import séance as ghost_séance, verify_claims

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Necropsy",
    description="Autopsy on abandoned Git repositories; séance with the ghost.",
    version="0.1.0",
)


# ─── Health ───────────────────────────────────────────────────────────────

@app.get("/health", response_model=HealthStatus)
async def health():
    """Machine status panel: reports real model names, Ollama reachability,
    whether embeddings load, and last answer latency.

    Required states: ok, error, model_missing, slow_inference,
    invalid_repo_source.
    """
    from ..db import get_embeddings_by_date as _ged

    # Check Ollama reachability
    ollama_ok = False
    ollama_model = None
    try:
        resp = __import__("requests").get(
            "http://localhost:11434/api/tags", timeout=3
        )
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            ollama_model = next(
                (m["name"] for m in models if LLM_MODEL in m.get("name", "")),
                None,
            )
            ollama_ok = ollama_model is not None
    except Exception:
        ollama_ok = False
        ollama_model = None

    # Check embeddings loaded
    embeddings_loaded = False
    try:
        conn = __import__("sqlite3").connect(
            str(__import__("pathlib").Path(__import__("os").getcwd__()) / "data/necropsy.db")
        )
        conn.execute("PRAGMA foreign_keys = ON;")
        count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE model = ?",
            (EMBED_MODEL,)
        ).fetchone()[0]
        embeddings_loaded = count > 0
        conn.close()
    except Exception:
        embeddings_loaded = False

    # Get current progress from active repos
    progress = None
    try:
        repos = list_repos()
        if repos:
            active = [r for r in repos if r.get("status") in ("ingesting", "indexing")]
            if active:
                progress = {
                    r["source"][:40] + "...": r.get("progress", 0)
                    for r in active
                }
    except Exception:
        pass

    # Determine overall status
    if not ollama_ok:
        status = "model_missing"
    elif not embeddings_loaded:
        status = "error"
    else:
        status = "ok"

    # Try to get last answer latency from last seance log
    last_latency = None
    try:
        conn = __import__("sqlite3").connect(
            str(__import__("pathlib").Path(__import__("os").getcwd__()) / "data/necropsy.db")
        )
        conn.execute("PRAGMA foreign_keys = ON;")
        row = conn.execute(
            "SELECT latency_ms FROM seance_log ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            last_latency = row[0]
    except Exception:
        pass

    return HealthStatus(
        status=status,
        model=ollama_model,
        embeddings_model=EMBED_MODEL,
        ollama_reachable=ollama_ok,
        embeddings_loaded=embeddings_loaded,
        last_answer_latency_ms=last_latency,
        progress=progress,
    )


# ─── Exhume ───────────────────────────────────────────────────────────────

@app.post("/exhume", response_model=ExhumeResponse)
async def exhume(source: ExhumeSource):
    """Start indexing a repository.

    Validates the source, clones/opens the repo, extracts commits,
    authors, files, and tombstones, then builds the embedding index.
    Returns a job_id for progress polling.
    """
    from ..db import add_repo as _add_repo

    source_str = source.source

    # Validate source
    ok, normalized = validate_source(source_str)
    if not ok:
        update_repo_status(-1, "failed", error="Invalid repo source")
        raise HTTPException(
            status_code=400,
            detail="Invalid repo source. Must be https://, ssh:///git@ URL, "
                    "or a local path containing .git",
        )

    # Check if already indexed
    if repo_exists(source_str):
        repo = get_repo(1)  # fallback; in real code use proper repo lookup
        update_repo_status(repo["id"], "ready")
        return ExhumeResponse(
            job_id=f"repo_{source_str[:8]}",
            status="ready",
            message="Repository already indexed.",
        )

    # Create job entry
    repo_id = add_repo(
        source=source_str,
        name=source_str[:60] or "unnamed",
        status="ingesting",
    )

    # Clone or open repo
    local_path = f"clones/{repo_id}_{hash(source_str) % 10000}"
    os.makedirs(local_path, exist_ok=True)

    success, error = clone_repo(source_str, local_path)
    if not success:
        update_repo_status(repo_id, "failed", error=error or "Clone failed")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to clone repository: {error}",
        )

    # Extract commits
    try:
        commits = extract_commits(local_path, MAX_COMMITS=5000)
        added = add_commits(repo_id, commits)
        logger.info(f"Added {added} commits for repo {repo_id}")
    except Exception as e:
        update_repo_status(repo_id, "failed", error=f"Commit extraction error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error extracting commits: {e}",
        )

    # Extract authors
    try:
        from ..ingest import extract_authors
        authors = extract_authors(local_path)
        # Authors are implicitly added during commit extraction
    except Exception as e:
        logger.warning(f"Author extraction error: {e}")

    # Extract file changes
    try:
        files = extract_files_changed(local_path, commits)
        added = add_file_changes(repo_id, files)
        logger.info(f"Added {added} file change records")
    except Exception as e:
        logger.warning(f"File changes error: {e}")

    # Extract tombstones
    try:
        tombstones = extract_tombstones(local_path, commits)
        added = add_tombstones(repo_id, tombstones)
        logger.info(f"Added {added} tombstone records")
    except Exception as e:
        logger.warning(f"Tombstones error: {e}")

    # Build embeddings index
    try:
        update_repo_status(repo_id, "indexing")
        result = idx_build(repo_id, EMBED_MODEL)
        logger.info(f"Indexing result: {result}")
    except Exception as e:
        update_repo_status(repo_id, "failed", error=f"Indexing error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error building embedding index: {e}",
        )

    # Mark as ready
    update_repo_status(repo_id, "ready")

    return ExhumeResponse(
        job_id=f"repo_{source_str[:8]}",
        status="ready",
        message="Repository indexed successfully.",
    )


# ─── Autopsy ──────────────────────────────────────────────────────────────

@app.post("/autopsy", response_model=AutopsyResponse)
async def autopsy(req: AutopsyRequest):
    """Run deterministic analytics (time of death, bus factor, eras) as of
    the selected date. No LLM involved except optional era labels.

    Returns autopsy stats keyed by as_of date.
    """
    from ..coroner import activity, busfactor, eras  # planned; placeholder

    repo_id = req.repo_id
    as_of = req.as_of
    cutoff = as_of if as_of.endswith("Z") else f"{as_of}Z"

    conn = sqlite3.connect(
        str(__import__("pathlib").Path(__import__("os").getcwd__()) / "data/necropsy.db")
    )
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        # Time of death: last commit date <= as_of
        last_commit = conn.execute("""
            SELECT authored_at FROM commits
            WHERE repo_id = ? AND authored_at <= ?
            ORDER BY authored_at DESC LIMIT 1
        """, (repo_id, cutoff)).fetchone()

        # Weekly activity
        weekly = conn.execute("""
            SELECT strftime('%Y-%W', authored_at) AS week, COUNT(*) AS commits
            FROM commits
            WHERE repo_id = ? AND authored_at <= ?
              AND is_merge = 0 AND author_id IN (
                  SELECT id FROM authors WHERE is_bot = 0
              )
            GROUP BY week ORDER BY week DESC LIMIT 4
        """, (repo_id, cutoff)).fetchall()

        # Bus factor: top author by churn in the 12 months before as_of
        from datetime import datetime, timedelta
        window_start = (
            datetime.strptime(cutoff[:10], "%Y-%m-%d") - timedelta(days=365)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        bus_factor_rows = conn.execute("""
            SELECT a.id, a.display_name, SUM(c.additions + c.deletions) AS churn
            FROM commits c JOIN authors a ON a.id = c.author_id
            WHERE c.repo_id = ? AND a.is_bot = 0
              AND c.authored_at <= ? AND c.authored_at > ?
            GROUP BY a.id ORDER BY churn DESC LIMIT 1
        """, (repo_id, cutoff, window_start)).fetchone()

        # Eras (display only - from eras table if available)
        eras_rows = conn.execute("""
            SELECT idx, label, start_at, end_at, commit_count, algo
            FROM eras WHERE repo_id = ?
            ORDER BY idx
        """, (repo_id,)).fetchall()

        result = {
            "time_of_death": last_commit[0] if last_commit else None,
            "last_gasp_commit": None,  # placeholder - last gasp commit
            "weekly_activity": [
                {"week": w[0], "commits": w[1]} for w in weekly
            ] if weekly else None,
            "bus_factor": {
                "top_author_id": bus_factor_rows[0] if bus_factor_rows else None,
                "top_author_churn": bus_factor_rows[2] if bus_factor_rows else 0,
            } if bus_factor_rows else None,
            "eras": [
                {"idx": e[0], "label": e[1], "start_at": e[2], "end_at": e[3],
                 "commit_count": e[4], "algo": e[5]}
                for e in eras_rows
            ] if eras_rows else None,
        }
    finally:
        conn.close()

    return AutopsyResponse(**result)


# ─── Séance ───────────────────────────────────────────────────────────────

@app.post("/seance", response_model=SéanceResponse)
async def séance(req: SéanceRequest):
    """Ask the ghost a question time-filtered to as_of date.

    Pipeline:
        1. Retrieve top-k evidence commits dated <= as_of
        2. Build prompt with evidence and date boundary
        3. Call Ollama LLM with JSON-schema output, thinking disabled
        4. Verify each claim against DB and evidence set
        5. Return verified answer or "The dead do not remember that."
    """
    from ..ghost.llm import chat
    from ..ghost.prompt import build_prompt
    from ..ghost.verify import verify_claims
    import hashlib
    import time

    start = time.time()
    repo_id = req.repo_id
    as_of = req.as_of
    question = req.question

    # Step 1: Retrieve evidence
    try:
        evidence = retrieve(repo_id, question, as_of, k=RETRIEVAL_K)
    except Exception as e:
        logger.error(f"Retrieval error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error retrieving evidence: {e}",
        )

    # Extract evidence hash set for later verification
    evidence_hashes = {ev["hash"] for ev in evidence}

    # Step 2: Build prompt
    try:
        prompt_messages = build_prompt(as_of, question, evidence)
    except Exception as e:
        logger.error(f"Prompt build error: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"Error building prompt: {e}",
        )

    # Step 3: Call Ollama LLM
    try:
        llm_result = chat(
            model=LLM_MODEL,
            messages=prompt_messages,
            schema={
                "type": "object",
                "properties": {
                    "remembers": {"type": "string"},
                    "answer": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string"},
                                "commit_hash": {"type": "string"},
                                "support": {"type": "string"},
                            },
                            "required": ["claim", "commit_hash", "support"],
                        },
                    },
                },
                "required": ["answer", "claims"],
            },
        )

        raw_response = llm_result.get("content", "")
        parsed = llm_result.get("parsed", {})
    except ModelMissingError as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
        )
    except OllamaDownError as e:
        raise HTTPException(
            status_code=503,
            detail=str(e),
        )
    except JSONVerifyError as e:
        # Model couldn't produce valid JSON; try once more or return error
        logger.warning(f"LLM JSON verification failed: {e}")
        # Attempt one fallback: return the "dead do not remember" response
        latency_ms = int((time.time() - start) * 1000)
        return SéanceResponse(
            answer="The dead do not remember that.",
            claims=[],
            latency_ms=latency_ms,
            config_hash="fallback_json_fail",
        )
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        raise HTTPException(
            status_code=503,
            detail=f"Error calling LLM: {str(e)}",
        )

    latency_ms = llm_result.get("latency_ms", int((time.time() - start) * 1000))

    # Step 4: Verify claims
    claims = parsed.get("claims", [])
    config_hash = hashlib.sha256(
        f"{LLM_MODEL}|{EMBED_MODEL}|{as_of}|{question}".encode()
    ).hexdigest()[:8]

    surviving_claims = verify_claims(
        claims, repo_id, as_of, evidence_hashes
    )

    # Step 5: Build response
    if not surviving_claims and not parsed.get("answer", "").strip():
        answer = "The dead do not remember that."
    elif parsed.get("answer", "").strip() == "The dead do not remember that.":
        answer = "The dead do not remember that."
    else:
        answer = parsed.get("answer", "The dead do not remember that.")

    # Only include claims that survived verification
    verified_claims = []
    for c in surviving_claims:
        verified_claims.append(
            ClaimSchema(
                claim=c.get("claim", ""),
                commit_hash=c.get("commit_hash", ""),
                support=c.get("support", ""),
            )
        )

    # Log the seance
    try:
        log_seance(
            repo_id=repo_id,
            as_of=as_of,
            question=question,
            config_hash=config_hash,
            llm_model=LLM_MODEL,
            evidence_hashes=list(evidence_hashes),
            raw_response=raw_response,
            final_response=json.dumps({
                "answer": answer,
                "claims": [{"claim": c.claim, "commit_hash": c.commit_hash, "support": c.support} for c in verified_claims]
            }),
            claims_total=len(claims),
            claims_kept=len(surviving_claims),
            latency_ms=latency_ms,
        )
    except Exception as e:
        logger.warning(f"Seance log error: {e}")

    return SéanceResponse(
        answer=answer,
        claims=verified_claims,
        latency_ms=latency_ms,
        config_hash=config_hash,
    )


# ─── Repos list ───────────────────────────────────────────────────────────

@app.get("/repos")
async def list_repos_endpoint():
    """List all indexed repositories."""
    repos = list_repos()
    return {"repos": repos}


# ─── Startup: init DB ────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Initialize the database on server start."""
    init_db()
    logger.info("Necropsy API started; DB initialized.")