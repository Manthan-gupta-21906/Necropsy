# necropsy/ghost/__init__.py — Ghost package
# -----------------------------------------
# Public API:
#   - verify_claims(claims, repo_id, as_of, evidence_hashes)
#   - build_prompt(as_of, question, evidence)
#   - chat(...) -> schema-constrained Ollama response
#   - check_ollama() -> real Ollama status

from .llm import (
    chat,
    check_ollama,
    ensure_model_loaded,
    get_warmup_state,
    warmup_async,
    OllamaError,
    OllamaDownError,
    OllamaTimeoutError,
    ModelMissingError,
    JSONVerifyError,
    _extract_json_from_text,
)
from .prompt import build_prompt, SEANCE_SCHEMA
from .verify import verify_claims

__all__ = [
    "verify_claims",
    "build_prompt",
    "SEANCE_SCHEMA",
    "chat",
    "check_ollama",
    "ensure_model_loaded",
    "get_warmup_state",
    "warmup_async",
    "OllamaError",
    "OllamaDownError",
    "OllamaTimeoutError",
    "ModelMissingError",
    "JSONVerifyError",
]
