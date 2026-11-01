# necropsy/api/server.py — FastAPI application with endpoints
# ----------------------------------------------------------
# Endpoints:
#   GET   /                     -> the static UI
#   GET   /health               -> real machine status (Ollama, embeddings, latency)
#   GET   /repos                -> indexed repos with real date bounds
#   GET   /repos/{id}/range     -> first/last commit dates for the slider
#   POST  /exhume               -> start a background indexing job
#   GET   /exhume/{job_id}      -> job status: stage, progress, error
#   GET   /autopsy              -> deterministic, as_of-filtered analytics
#   POST  /seance               -> question the ghost (real Ollama call)
#
# Invariants enforced here:
#   • Every statistic and every evidence item is filtered by authored_at <= as_of
#   • Eras are never used (they depend on future data)
#   • Errors are friendly JSON; no stack traces reach the client

import hashlib
import logging
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import config
from ..config import EMBED_MODEL, LLM_MODEL, RETRIEVAL_K
from ..db import (
    add_commits,
    add_file_changes,
    add_repo,
    add_tombstones,
    finalize_repo,
    get_commits_by_hashes,
    get_connection,
    get_repo,
    get_repo_by_source,
    init_db,
    list_repos,
    log_seance,
    repo_range,
    reset_repo,
    update_repo_status,
)
from ..ghost.llm import (
    OllamaError,
    check_ollama,
    chat,
    get_warmup_state,
    warmup_async as warmup_llm,
)
from ..ghost.prompt import SEANCE_SCHEMA, build_prompt, fit_evidence
from ..ghost.verify import verify_claims
from ..index import get_embed_state, retrieve, store_embeddings, warmup_async as warmup_embeddings
from ..ingest.git import (
    clone_repo,
    extract_commits,
    extract_files_changed,
    extract_tombstones,
    validate_source,
)

APP_TITLE = "Necropsy"
APP_DESCRIPTION = "Autopsy on abandoned Git repositories; séance with the ghost."
APP_VERSION = "0.1.0"
REFUSAL = "The dead do not remember that."

WEB_DIR = Path(__file__).resolve().parents[2] / "web"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize the DB, then warm up the models in background threads."""
    init_db()
    warmup_embeddings(EMBED_MODEL)
    warmup_llm(LLM_MODEL)
    # The status snapshot is refreshed off the request path, so /health can
    # answer instantly even while the model is generating.
    start_health_refresher()
    logger.info(
        "Necropsy started: db=%s embed=%s llm=%s",
        config.db_path(), EMBED_MODEL, LLM_MODEL,
    )
    yield


app = FastAPI(
    title=APP_TITLE,
    description=APP_DESCRIPTION,
    version=APP_VERSION,
    lifespan=lifespan,
)

if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# --- Job registry ----------------------------------------------------------

_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()

STAGES = ("queued", "cloning", "reading commits", "tombstones", "embedding", "ready")
STAGE_PROGRESS = {
    "queued": 0.0,
    "cloning": 0.15,
    "reading commits": 0.45,
    "tombstones": 0.60,
    "embedding": 0.85,
    "ready": 1.0,
    "failed": 0.0,
}


def _job_update(job_id: str, **fields: Any) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def _job_get(job_id: str) -> Optional[Dict[str, Any]]:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


# --- App lifecycle ---------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> FileResponse:
    index = WEB_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=500, detail="web/index.html is missing")
    return FileResponse(str(index))


# --- Health ----------------------------------------------------------------
#
# /health must answer in milliseconds even while a slow CPU generation is in
# flight. It therefore NEVER talks to Ollama or SQLite: a background thread
# refreshes a cached snapshot every few seconds and the endpoint just returns
# it. While a séance is generating, a probe that times out is not treated as
# "Ollama is down" — the last known good values are kept and the state is
# reported as busy.

HEALTH_REFRESH_S = 3.0


def _empty_ollama_state() -> Dict[str, Any]:
    """Neutral starting point for the snapshot (before the first probe)."""
    return {
        "reachable": False,
        "model": LLM_MODEL,
        "model_installed": False,
        "models": [],
        "error": None,
        "error_kind": None,
        "base_url": config.OLLAMA_BASE_URL,
    }


_HEALTH_LOCK = threading.Lock()
_HEALTH: Dict[str, Any] = {
    "ollama": _empty_ollama_state(),
    "embeddings": {"state": "not_loaded", "model": EMBED_MODEL, "message": None},
    "warmup": {"state": "not_started", "message": None},
    "last_answer_ms": None,
    "checked_at": 0.0,
    "probe_failures": 0,
    "probed": False,
}
_HEALTH_THREAD: Optional[threading.Thread] = None

# Séances currently generating. Set by the /seance route, read by /health.
_BUSY_LOCK = threading.Lock()
_BUSY = 0


def _busy_state() -> Dict[str, Any]:
    """Whether a generation is in flight right now."""
    with _BUSY_LOCK:
        busy = _BUSY > 0
    return {"busy": busy, "busy_reason": "answering" if busy else None}


def _seance_started() -> None:
    global _BUSY
    with _BUSY_LOCK:
        _BUSY += 1


def _seance_finished() -> None:
    global _BUSY
    with _BUSY_LOCK:
        _BUSY = max(0, _BUSY - 1)


def _last_answer_ms() -> Optional[int]:
    """Most recent séance latency from the database, if any."""
    try:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT latency_ms FROM seance_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row and row[0] is not None else None
    except Exception:  # noqa: BLE001 - health must never 500
        return None


def _snapshot_locked() -> Dict[str, Any]:
    """Copy the cached snapshot. Caller holds _HEALTH_LOCK."""
    return {
        "ollama": dict(_HEALTH["ollama"]),
        "embeddings": dict(_HEALTH["embeddings"]),
        "warmup": dict(_HEALTH["warmup"]),
        "last_answer_ms": _HEALTH["last_answer_ms"],
        "checked_at": _HEALTH["checked_at"],
        "probe_failures": _HEALTH["probe_failures"],
        "probed": _HEALTH["probed"],
    }


def _health_snapshot() -> Dict[str, Any]:
    with _HEALTH_LOCK:
        return _snapshot_locked()


def _note_answer_latency(latency_ms: int) -> None:
    """Record a fresh latency in the snapshot (no SQLite on the request path)."""
    with _HEALTH_LOCK:
        _HEALTH["last_answer_ms"] = int(latency_ms)


def refresh_health_once() -> Dict[str, Any]:
    """Probe Ollama once and update the cached snapshot.

    This is the only place that calls check_ollama() for status purposes. It
    runs in the background refresher thread (and in tests), never inside the
    /health request handler.
    """
    busy = _busy_state()["busy"]

    # All slow work happens before the lock so /health is never made to wait.
    probe = check_ollama(LLM_MODEL)
    embeddings = get_embed_state()
    warmup = get_warmup_state()
    last_ms = _last_answer_ms()

    with _HEALTH_LOCK:
        if busy and not probe["reachable"]:
            # Generating on CPU can make /api/tags time out. That is not an
            # outage: keep the last known good values and report "busy".
            kept = dict(_HEALTH["ollama"])
            kept["error_kind"] = probe["error_kind"]
            probe = kept
        else:
            _HEALTH["probe_failures"] = (
                0 if probe["reachable"] else _HEALTH["probe_failures"] + 1
            )
        _HEALTH["ollama"] = probe
        _HEALTH["embeddings"] = embeddings
        _HEALTH["warmup"] = warmup
        _HEALTH["last_answer_ms"] = last_ms
        _HEALTH["checked_at"] = time.time()
        _HEALTH["probed"] = True
        return _snapshot_locked()


def _health_refresher() -> None:
    """Background loop: keep the snapshot a few seconds fresh, forever."""
    while True:
        try:
            refresh_health_once()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            logger.exception("Health refresh failed")
        time.sleep(HEALTH_REFRESH_S)


def start_health_refresher() -> None:
    """Start the snapshot refresher once (idempotent)."""
    global _HEALTH_THREAD
    if _HEALTH_THREAD is not None and _HEALTH_THREAD.is_alive():
        return
    # Seed the cheap, in-memory parts so the very first /health is already
    # truthful about the embeddings, without waiting for a probe.
    with _HEALTH_LOCK:
        _HEALTH["embeddings"] = get_embed_state()
        _HEALTH["warmup"] = get_warmup_state()
    _HEALTH_THREAD = threading.Thread(
        target=_health_refresher, name="health-refresh", daemon=True
    )
    _HEALTH_THREAD.start()


@app.get("/health", tags=["health"])
def health() -> JSONResponse:
    """Cached machine status. Nothing here is hardcoded and nothing blocks."""
    snapshot = _health_snapshot()
    busy = _busy_state()

    ollama = snapshot["ollama"]
    embed = snapshot["embeddings"]
    llm_warm = snapshot["warmup"]
    last_ms = snapshot["last_answer_ms"]
    embeddings_ready = embed.get("state") == "ready"

    if not snapshot["probed"]:
        # No probe has completed yet: do not pretend Ollama is down.
        status = "checking"
    elif busy["busy"] and ollama["reachable"]:
        # Working is not failing.
        status = "busy"
    elif ollama["error_kind"] == "timeout" and llm_warm.get("state") == "warming":
        # Ollama is busy loading the model: it is up, just slow.
        status = "warming"
    elif not ollama["reachable"] or embed.get("state") == "error":
        status = "error"
    elif ollama["model_installed"] and embeddings_ready:
        status = "ok"
    else:
        status = "warming"

    checked_at = snapshot["checked_at"]
    age_ms = int((time.time() - checked_at) * 1000) if checked_at else None

    return JSONResponse({
        "status": status,
        "version": APP_VERSION,
        "busy": busy["busy"],
        "busy_reason": busy["busy_reason"],
        "checked_at": checked_at or None,
        "age_ms": age_ms,
        "probe_failures": snapshot["probe_failures"],        "ollama": {
            "reachable": ollama["reachable"],
            "model": ollama["model"],
            "model_installed": ollama["model_installed"],
            "models": ollama["models"],
            "base_url": ollama["base_url"],
            "error": ollama["error"],
            "warmup": llm_warm,
        },
        "embeddings": {
            "state": embed.get("state"),
            "model": embed.get("model") or EMBED_MODEL,
            "message": embed.get("message"),
        },
        "last_answer_ms": last_ms,
        # Flat aliases kept for older clients.
        "model": ollama["model"],
        "embeddings_model": EMBED_MODEL,
        "ollama_reachable": ollama["reachable"],
        "embeddings_loaded": embeddings_ready,
        "last_answer_latency_ms": last_ms,
    })



# --- Repos -----------------------------------------------------------------

@app.get("/repos", tags=["repos"])
def list_repos_endpoint() -> Dict[str, Any]:
    """List indexed repositories, including real slider bounds."""
    repos = list_repos()
    for repo in repos:
        rng = repo_range(repo["id"])
        repo["first_commit_at"] = rng["first_commit_at"] or repo.get("first_commit_at")
        repo["last_commit_at"] = rng["last_commit_at"] or repo.get("last_commit_at")
        repo["commit_count"] = rng["commit_count"]
    return {"repos": repos}


@app.get("/repos/{repo_id}/range", tags=["repos"])
def repo_range_endpoint(repo_id: int) -> Dict[str, Any]:
    """First and last commit dates for the time slider."""
    if get_repo(repo_id) is None:
        raise HTTPException(status_code=404, detail=f"Repo {repo_id} is not indexed")
    rng = repo_range(repo_id)
    if not rng["first_commit_at"]:
        raise HTTPException(status_code=409, detail="Repo has no commits yet")
    return rng


# --- Exhume (background job) ----------------------------------------------

def _embedding_text(commit: dict) -> str:
    """Text embedded for a commit: subject plus body."""
    subject = (commit.get("subject") or "").strip()
    body = (commit.get("body") or "").strip()
    return f"{subject}\n{body}".strip()


def _repo_name(source: str) -> str:
    """Human-readable repo name from a URL or a Windows/POSIX local path."""
    cleaned = source.strip().rstrip("/\\")
    if cleaned.endswith(".git"):
        cleaned = cleaned[: -len(".git")]
    name = re.split(r"[\\/]", cleaned)[-1]
    # scp-style remotes: git@host:owner/repo
    if ":" in name:
        name = name.rsplit(":", 1)[-1]
    return name or "repo"


def _index_repo(job_id: str, source: str) -> None:
    """Background worker: clone (if needed), ingest, embed, finalize."""
    clone_dir: Optional[Path] = None
    try:
        _job_update(job_id, stage="cloning", progress=STAGE_PROGRESS["cloning"])

        is_local = validate_source(source)[0] and not (
            source.startswith("https://")
            or source.startswith("ssh://")
            or ("@" in source and ":" in source and not Path(source).exists())
        )

        if is_local:
            repo_path = source
        else:
            clone_dir = Path(config.db_path()).parent / "repos" / hashlib.sha256(
                source.encode("utf-8")
            ).hexdigest()[:12]
            clone_dir.mkdir(parents=True, exist_ok=True)
            if (clone_dir / ".git").exists():
                # Re-use the existing clone: fetch nothing, index what we have.
                repo_path = str(clone_dir)
            else:
                ok, error = clone_repo(source, str(clone_dir), timeout=300)
                if not ok:
                    raise RuntimeError(error or "Clone failed")
                repo_path = str(clone_dir)

        # Re-index in place: same source updates the existing row.
        existing = get_repo_by_source(source)
        if existing:
            repo_id = existing["id"]
            reset_repo(repo_id)
            update_repo_status(repo_id, "ingesting", None, 0.15)
        else:
            repo_id = add_repo(
                source=source,
                name=_repo_name(source),
                status="ingesting",
                local_path=repo_path,
            )

        _job_update(job_id, repo_id=repo_id, stage="reading commits",
                    progress=STAGE_PROGRESS["reading commits"])
        update_repo_status(repo_id, "ingesting", None, 0.45)

        commits = extract_commits(repo_path, max_commits=config.MAX_COMMITS)
        if not commits:
            raise RuntimeError("No commits found in the repository.")
        add_commits(repo_id, commits)

        files = extract_files_changed(repo_path, commits)
        add_file_changes(repo_id, files)

        _job_update(job_id, stage="tombstones", progress=STAGE_PROGRESS["tombstones"],
                    commit_count=len(commits))
        update_repo_status(repo_id, "indexing", None, 0.60)
        tombstones = extract_tombstones(repo_path, commits)
        add_tombstones(repo_id, tombstones)

        _job_update(job_id, stage="embedding", progress=STAGE_PROGRESS["embedding"])
        update_repo_status(repo_id, "indexing", None, 0.85)

        items: List[dict] = []
        texts: List[str] = []
        for commit in commits:
            text = _embedding_text(commit)
            items.append({
                "item_type": "commit",
                "item_ref": commit["hash"],
                "item_at": commit["authored_at"],
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
            texts.append(text)

        from ..index.embed import compute_embeddings

        vectors = compute_embeddings(texts, EMBED_MODEL)
        stored = store_embeddings(repo_id, items, EMBED_MODEL, vectors)

        first_at = min(c["authored_at"] for c in commits)
        last_at = max(c["authored_at"] for c in commits)
        finalize_repo(
            repo_id=repo_id,
            head_sha=commits[0]["hash"],
            first_commit_at=first_at,
            last_commit_at=last_at,
            commit_count=len(commits),
            local_path=repo_path,
            status="ready",
        )

        _job_update(
            job_id,
            stage="ready",
            progress=1.0,
            status="ready",
            repo_id=repo_id,
            commit_count=len(commits),
            embeddings=stored,
            first_commit_at=first_at,
            last_commit_at=last_at,
            error=None,
        )
        logger.info("Indexed repo %s: %s commits, %s embeddings", repo_id, len(commits), stored)

    except Exception as exc:  # noqa: BLE001 - report the real reason to the client
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("Indexing failed for %s", source)
        job = _job_get(job_id) or {}
        repo_id = job.get("repo_id")
        if repo_id:
            update_repo_status(repo_id, "failed", message, 0.0)
        _job_update(
            job_id,
            stage="failed",
            status="failed",
            error=message,
            progress=0.0,
        )
    finally:
        # The clone stays on disk so a re-index does not re-download the repo.
        if clone_dir is not None:
            logger.info("Clone for %s kept at %s", source, clone_dir)


@app.post("/exhume", tags=["exhume"])
async def exhume(request: Request, source: Optional[str] = Query(default=None)) -> Dict[str, Any]:
    """Validate a source and start a background indexing job.

    Accepts either `?source=...` or a JSON body `{"source": "..."}`.
    """
    if source is None:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - empty or non-JSON body
            payload = {}
        if isinstance(payload, dict):
            source = payload.get("source")

    ok, reason = validate_source(source or "")
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "job_id": job_id,
            "source": source,
            "status": "running",
            "stage": "queued",
            "progress": 0.0,
            "error": None,
            "repo_id": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    thread = threading.Thread(
        target=_index_repo, args=(job_id, source), name=f"exhume-{job_id}", daemon=True
    )
    thread.start()

    return {"job_id": job_id, "status": "running", "stage": "queued", "progress": 0.0}


@app.get("/exhume/{job_id}", tags=["exhume"])
def exhume_status(job_id: str) -> Dict[str, Any]:
    """Poll a background indexing job."""
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id")
    return job


# --- Autopsy ---------------------------------------------------------------

def _autopsy(repo_id: int, as_of: str) -> Dict[str, Any]:
    """Deterministic analytics, every value filtered by authored_at <= as_of."""
    if get_repo(repo_id) is None:
        raise HTTPException(status_code=404, detail=f"Repo {repo_id} is not indexed")

    cutoff = config.as_of_end(as_of)
    conn = get_connection()
    try:
        totals = conn.execute(
            """
            SELECT COUNT(*), MIN(authored_at), MAX(authored_at),
                   COALESCE(SUM(additions), 0), COALESCE(SUM(deletions), 0),
                   COALESCE(SUM(files_changed_count), 0)
              FROM commits
             WHERE repo_id = ? AND authored_at <= ?
            """,
            (repo_id, cutoff),
        ).fetchone()

        commit_count = totals[0] or 0
        authors = conn.execute(
            """
            SELECT COUNT(DISTINCT author_id) FROM commits
             WHERE repo_id = ? AND authored_at <= ?
            """,
            (repo_id, cutoff),
        ).fetchone()[0] or 0

        top_author = conn.execute(
            """
            SELECT a.display_name, COUNT(*) AS commits,
                   COALESCE(SUM(c.additions + c.deletions), 0) AS churn
              FROM commits c JOIN authors a ON a.id = c.author_id
             WHERE c.repo_id = ? AND c.authored_at <= ?
             GROUP BY a.id
             ORDER BY commits DESC, churn DESC
             LIMIT 1
            """,
            (repo_id, cutoff),
        ).fetchone()

        monthly = conn.execute(
            """
            SELECT substr(authored_at, 1, 7) AS month, COUNT(*) AS commits
              FROM commits
             WHERE repo_id = ? AND authored_at <= ? AND is_merge = 0
             GROUP BY month
             ORDER BY month
            """,
            (repo_id, cutoff),
        ).fetchall()

        recent = conn.execute(
            """
            SELECT hash, subject, authored_at
              FROM commits
             WHERE repo_id = ? AND authored_at <= ?
             ORDER BY authored_at DESC
             LIMIT 5
            """,
            (repo_id, cutoff),
        ).fetchall()

        tombstones = conn.execute(
            """
            SELECT path, died_at, lifespan_days, epitaph
              FROM tombstones
             WHERE repo_id = ? AND died_at <= ?
             ORDER BY died_at DESC
             LIMIT 50
            """,
            (repo_id, cutoff),
        ).fetchall()

        last_gasp = conn.execute(
            """
            SELECT hash, subject, authored_at
              FROM commits
             WHERE repo_id = ? AND authored_at <= ? AND is_merge = 0
             ORDER BY authored_at DESC
             LIMIT 1
            """,
            (repo_id, cutoff),
        ).fetchone()

        return {
            "repo_id": repo_id,
            "as_of": as_of,
            "as_of_end": cutoff,
            "commit_count": commit_count,
            "author_count": authors,
            "first_commit_at": totals[1],
            "last_commit_at": totals[2],
            "time_of_death": totals[2],
            "additions": totals[3],
            "deletions": totals[4],
            "files_changed": totals[5],
            "bus_factor": (
                {
                    "top_author": top_author[0],
                    "commits": top_author[1],
                    "churn": top_author[2],
                    "share": round(top_author[1] / commit_count, 3) if commit_count else 0,
                }
                if top_author
                else None
            ),
            "monthly_activity": [
                {"month": row[0], "commits": row[1]} for row in monthly
            ],
            "recent_commits": [
                {"hash": row[0], "subject": row[1], "at": row[2]} for row in recent
            ],
            "last_gasp_commit": (
                {"hash": last_gasp[0], "subject": last_gasp[1], "at": last_gasp[2]}
                if last_gasp
                else None
            ),
            "tombstones": [
                {"path": row[0], "died_at": row[1], "lifespan_days": row[2], "epitaph": row[3]}
                for row in tombstones
            ],
            "tombstone_count": len(tombstones),
            # Eras are deliberately absent: they are computed from the full
            # history and would leak post-as_of information.
        }
    finally:
        conn.close()


@app.get("/autopsy", tags=["autopsy"])
def autopsy(repo_id: int, as_of: str) -> Dict[str, Any]:
    """Run deterministic analytics as of a date."""
    return _autopsy(repo_id, as_of)


@app.post("/autopsy", tags=["autopsy"], include_in_schema=False)
def autopsy_post(repo_id: int, as_of: str) -> Dict[str, Any]:
    """Backwards-compatible POST alias for /autopsy."""
    return _autopsy(repo_id, as_of)


# --- Séance ----------------------------------------------------------------

def _seance(repo_id: int, as_of: str, question: str) -> Dict[str, Any]:
    """Retrieve evidence, ask the real model, verify citations in code."""
    if get_repo(repo_id) is None:
        raise HTTPException(status_code=404, detail=f"Repo {repo_id} is not indexed")

    question = (question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty")

    started = time.time()

    # Retrieval is date-prefix filtered: nothing after as_of is scored.
    hits = retrieve(repo_id, question, config.as_of_end(as_of), k=RETRIEVAL_K)
    texts = get_commits_by_hashes(repo_id, [hit["hash"] for hit in hits])

    evidence: List[dict] = []
    for hit in hits:
        meta = texts.get(hit["hash"], {})
        evidence.append({
            "hash": hit["hash"],
            "at": meta.get("at") or hit.get("at"),
            "subject": meta.get("subject", ""),
            "body": meta.get("body", ""),
            "similarity": hit.get("similarity"),
        })

    # Keep only the evidence that fits the character budget. Fewer prompt
    # tokens is the difference between a 25 s and a 90 s CPU answer, and the
    # verifier below only accepts hashes the model was actually shown.
    evidence = fit_evidence(evidence)
    evidence_hashes = {ev["hash"] for ev in evidence}

    if not evidence:
        latency_ms = int((time.time() - started) * 1000)
        _note_answer_latency(latency_ms)
        return {
            "answer": REFUSAL,
            "remembers": "",
            "claims": [],
            "evidence": [],
            "model": LLM_MODEL,
            "latency_ms": latency_ms,
            "verified": 0,
            "reason": "no evidence dated on or before as_of",
        }

    messages = build_prompt(as_of, question, evidence)
    evidence_chars = sum(
        len(ev.get("subject") or "") + len(ev.get("body") or "") for ev in evidence
    )
    prompt_chars = sum(len(m.get("content") or "") for m in messages)
    logger.info(
        "seance: %d evidence commits, %d chars of evidence, %d chars of prompt, "
        "num_predict=%d num_ctx=%d",
        len(evidence), evidence_chars, prompt_chars,
        config.LLM_NUM_PREDICT, config.LLM_NUM_CTX,
    )

    try:
        result = chat(model=LLM_MODEL, messages=messages, schema=SEANCE_SCHEMA)
    except OllamaError as exc:
        # Friendly, actionable error. No stack trace.
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    usage = result.get("usage") or {}
    logger.info(
        "seance: tokens prompt=%s completion=%s total=%.1fs",
        usage.get("prompt_tokens"), usage.get("completion_tokens"),
        (time.time() - started),
    )

    parsed = result["parsed"]
    raw_claims = parsed.get("claims") or []
    if not isinstance(raw_claims, list):
        raw_claims = []

    surviving = verify_claims(raw_claims, repo_id, as_of, evidence_hashes)

    by_hash = {ev["hash"]: ev for ev in evidence}
    claims = []
    for claim in surviving:
        h = str(claim.get("commit_hash", "")).strip()
        meta = by_hash.get(h, {})
        claims.append({
            "claim": claim.get("claim", ""),
            "commit_hash": h,
            "subject": meta.get("subject", ""),
            "at": meta.get("at", ""),
            "support": claim.get("support", ""),
        })

    answer = str(parsed.get("answer") or "").strip()
    if not claims:
        answer = REFUSAL
    elif not answer:
        answer = "; ".join(c["claim"] for c in claims)

    latency_ms = int((time.time() - started) * 1000)
    _note_answer_latency(latency_ms)

    try:
        log_seance(
            repo_id=repo_id,
            as_of=as_of,
            question=question,
            config_hash=hashlib.sha256(
                f"{LLM_MODEL}|{EMBED_MODEL}|{RETRIEVAL_K}".encode()
            ).hexdigest()[:16],
            llm_model=LLM_MODEL,
            evidence_hashes=sorted(evidence_hashes),
            raw_response=result.get("content", "")[:20000],
            final_response=answer,
            claims_total=len(raw_claims),
            claims_kept=len(claims),
            latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001 - logging must not break an answer
        logger.warning("Could not write seance_log row", exc_info=True)

    return {
        "answer": answer,
        "remembers": parsed.get("remembers", ""),
        "claims": claims,
        "evidence": [
            {"hash": ev["hash"], "subject": ev["subject"], "at": ev["at"]}
            for ev in evidence
        ],
        "model": LLM_MODEL,
        "latency_ms": latency_ms,
        "verified": len(claims),
        "claims_total": len(raw_claims),
        "usage": usage,
    }


@app.post("/seance", tags=["seance"])
async def seance(request: Request) -> Dict[str, Any]:
    """Question the ghost. Body: {repo_id, as_of, question}."""
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Body must be JSON") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    try:
        repo_id = int(payload.get("repo_id"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="repo_id must be an integer") from exc

    as_of = payload.get("as_of")
    question = payload.get("question")
    if not as_of or not isinstance(as_of, str):
        raise HTTPException(status_code=400, detail="as_of must be a date string")

    # Run the blocking retrieval + Ollama call off the event loop so /health
    # and the rest of the API stay responsive during a slow generation.
    from starlette.concurrency import run_in_threadpool

    # Mark the machine busy for /health before any blocking work starts, and
    # clear it even if the model errors, times out or the client disconnects.
    _seance_started()
    try:
        return await run_in_threadpool(_seance, repo_id, as_of, question or "")
    finally:
        _seance_finished()


# --- Error handling --------------------------------------------------------

@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """Never leak a stack trace to the client."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}},
    )
