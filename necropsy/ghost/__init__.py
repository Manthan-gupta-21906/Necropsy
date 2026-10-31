# necropsy/ghost/__init__.py — Ghost package
# ------------------------------------------
# Public API:
#   • séance(repo_id, as_of, question) -> dict with answer, claims, etc.
#   • verify_claims(claims, repo_id, as_of) -> list of surviving claims

from .llm import chat, _extract_json_from_text
from .prompt import build_prompt
from .verify import verify_claims

__all__ = ["séance", "verify_claims", "build_prompt", "chat"]