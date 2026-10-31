# necropsy/api/schemas.py — Pydantic-like JSON schemas for API validation
# --------------------------------------------------------------------
# Input/output schemas for the FastAPI endpoints.
# No external deps beyond stdlib; used with FastAPI's annotation system.

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, validator


# --- Exhume request/response ---

class ExhumeSource(BaseModel):
    """A repo source URL or local path."""
    source: str = Field(..., description="Git repo URL (https/ssh) or local path containing .git")

    @validator("source")
    def reject_shell_injection(cls, v):
        if v.startswith("-"):
            raise ValueError("Source must not start with '-'")
        return v


class ExhumeResponse(BaseModel):
    """Response from POST /exhume."""
    job_id: str
    status: str  # pending, ingesting, indexing, ready, failed
    message: Optional[str] = None


# --- Séance request/response ---

class SéanceRequest(BaseModel):
    """Request to POST /seance."""
    repo_id: int = Field(..., description="Indexed repo identifier")
    as_of: str = Field(..., description="Date boundary, end-of-day UTC (ISO-8601)")
    question: str = Field(..., description="User question for the ghost")


class ClaimSchema(BaseModel):
    """A single claim with citation."""
    claim: str = Field(..., description="The claim being supported")
    commit_hash: str = Field(..., description="Commit hash supporting the claim")
    support: str = Field(..., description="How the commit supports the claim")


class SéanceResponse(BaseModel):
    """Response from POST /seance."""
    answer: str
    claims: List[ClaimSchema] = []
    latency_ms: int
    config_hash: str


# --- Autopsy request/response ---

class AutopsyRequest(BaseModel):
    """Request to POST /autopsy."""
    repo_id: int = Field(..., description="Indexed repo identifier")
    as_of: str = Field(..., description="Date boundary, end-of-day UTC")


class AutopsyResponse(BaseModel):
    """Response from POST /autopsy."""
    time_of_death: Optional[str] = None
    last_gasp_commit: Optional[str] = None
    weekly_activity: Optional[list] = None
    bus_factor: Optional[dict] = None
    eras: Optional[list] = None


# --- Health response ---

class HealthStatus(BaseModel):
    """Status report from GET /health."""
    status: str  # ok, error, model_missing, slow_inference, etc.
    model: Optional[str] = None
    embeddings_model: Optional[str] = None
    ollama_reachable: bool = False
    embeddings_loaded: bool = False
    last_answer_latency_ms: Optional[int] = None
    progress: Optional[dict] = None  # current indexing repo progress


# --- Evaluation harness schemas ---

class EvalQuestion(BaseModel):
    """A ground-truth evaluation question."""
    category: str  # last_modified | deleted_when | commit_change | symbol_alive
    as_of: str
    question: str
    expected: str