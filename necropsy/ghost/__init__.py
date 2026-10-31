# necropsy/ghost/__init__.py - Ghost package
# -----------------------------------------
# Public API:
#   - verify_claims(claims, repo_id, as_of) -> list of surviving claims
#   - build_prompt(context, question) -> prompt string
#   - chat(messages, model) -> LLM response

from .llm import chat, _extract_json_from_text
from .prompt import build_prompt
from .verify import verify_claims

__all__ = ["verify_claims", "build_prompt", "chat"]