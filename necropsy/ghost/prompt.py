# necropsy/ghost/prompt.py — Build system prompt and evidence blocks
# ------------------------------------------------------------------
# Responsibilities:
#   • Construct the chat prompt for the ghost LLM
#   • Include the as_of date boundary explicitly
#   • Format evidence commits as citeable blocks
#   • Enforce that the ghost answers as the project, not as a named maintainer
#   • Include strict JSON schema constraint

from typing import List, Dict, Any

# JSON schema handed to Ollama so generation is constrained to this shape.
SEANCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "remembers": {"type": "string"},
        "answer": {"type": "string"},
        "claims": {
            "type": "array",
            "maxItems": 5,
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
    "required": ["remembers", "answer", "claims"],
}


def build_prompt(
    as_of: str,
    question: str,
    evidence: List[Dict[str, Any]],
    model_name: str = None,
) -> List[Dict[str, Any]]:
    """Build the chat prompt for the ghost LLM.

    Args:
        as_of: the selected date (end-of-day UTC)
        question: the user's question
        evidence: list of evidence dicts from retrieval, each with:
            - hash: commit hash
            - subject: commit subject/first line
            - body: commit body (may be empty)
            - at: authored_at date
        model_name: optional model name for context

    Returns:
        A list of message dicts [system, user] ready for Ollama chat.
    """
    from ..config import EMBED_MODEL

    if model_name is None:
        from ..config import LLM_MODEL as _lm
        model_name = _lm

    # Build evidence block from retrieved commits
    evidence_text = _format_evidence(evidence)

    # Construct the prompt
    system_content = _system_prompt(as_of, model_name)

    user_content = f"""Time boundary: only commits authored on or before {as_of} (UTC) are relevant. Nothing newer may be used.

Question: {question}

Evidence from the repository (dated <= {as_of}):
{evidence_text}

Instructions:
- Answer the question using ONLY the evidence above.
- If the evidence cannot answer the question, say: "The dead do not remember that."
- Quote commit messages verbatim when supporting your answer.
- Provide your answer in strict JSON matching this schema:
  {{
    "remembers": "<what the project remembers/knows, or empty string>",
    "answer": "<your answer, or 'The dead do not remember that.'>",
    "claims": [
      {{
        "claim": "<the claim being supported>",
        "commit_hash": "<hash of a supporting commit>",
        "support": "<how the commit supports the claim, quoted text or message excerpt>"
      }}
    ]
  }}
  Only include claims that are genuinely supported by the evidence.
  At most 5 claims. If no claim is supported, set answer to "The dead do not remember that." and claims to []."""

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]


def _system_prompt(as_of: str, model_name: str = None) -> str:
    """The system-level instruction for the ghost."""
    return f"""You are the ghost of an abandoned software project.
You speak AS THE PROJECT, not as a named maintainer.
You can only use knowledge from commits authored on or before {as_of} (UTC).
You must cite commit hashes for every claim.
You must respond in strict JSON as specified by the user.
Do not impersonate real people. Do not use knowledge from after {as_of}.
Do not hallucinate commits. If evidence is insufficient, say "The dead do not remember that."""


def _format_evidence(evidence: List[Dict[str, Any]]) -> str:
    """Format evidence commits into a citeable block.

    Full hashes are shown so the model can cite them verbatim; the verifier
    matches on the exact hash.
    """
    if not evidence:
        return "[No evidence retrieved]"

    parts = []
    for i, ev in enumerate(evidence[:10], 1):  # max 10 evidence items
        hash_val = ev.get("hash", "unknown")
        at = ev.get("at", "unknown date")
        subject = (ev.get("subject") or "")[:200]
        body = (ev.get("body") or "").strip()
        body_excerpt = body.split("\n")[0][:200] if body else ""

        line = f"[{i}] Commit {hash_val} ({at}): {subject}"
        if body_excerpt and body_excerpt != subject:
            line += f" · {body_excerpt}"
        parts.append(line)

    return "\n".join(parts)