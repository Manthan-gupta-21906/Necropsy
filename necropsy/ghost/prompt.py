# necropsy/ghost/prompt.py — Build system prompt and evidence blocks
# ------------------------------------------------------------------
# Responsibilities:
#   • Construct the chat prompt for the ghost LLM
#   • Include the as_of date boundary explicitly
#   • Format evidence commits as citeable blocks
#   • Enforce that the ghost answers as the project, not as a named maintainer
#   • Include strict JSON schema constraint

from typing import List, Dict, Any, Optional

# JSON schema handed to Ollama so generation is constrained to this shape.
SEANCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "remembers": {"type": "string"},
        "answer": {"type": "string"},
        "claims": {
            "type": "array",
            "maxItems": 3,
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
    from ..config import LLM_MODEL as _lm

    if model_name is None:
        model_name = _lm

    # Build evidence block from retrieved commits, truncated to the budget.
    evidence_text = _format_evidence(fit_evidence(evidence))

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
- Phrase each "claim" with the commit's own words (the verifier compares your
  claim to the commit text, so paraphrase with the same words it uses).
- Be brief: keep "remembers" and "answer" under 25 words each, each claim
  under 15 words and each "support" under 10 words.
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
  At most 3 claims. If no claim is supported, set answer to "The dead do not remember that." and claims to []."""

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
    for i, ev in enumerate(evidence, 1):
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


def fit_evidence(
    evidence: List[Dict[str, Any]],
    budget_chars: Optional[int] = None,
    max_items: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Keep the highest-ranked evidence that fits the character budget.

    Retrieval is already ordered best-first and already date-filtered, so
    dropping the tail never removes an item dated after as_of. Each field is
    trimmed first, then whole items are dropped, then the last item's text is
    cut to size. At least one item is kept whenever evidence is non-empty.

    This matters on CPU: fewer prompt tokens means a much shorter prefill and
    a faster answer, and it also bounds what the model can cite.
    """
    from ..config import EVIDENCE_CHAR_BUDGET, EVIDENCE_ITEM_CHARS, RETRIEVAL_TOP_N

    budget = EVIDENCE_CHAR_BUDGET if budget_chars is None else budget_chars
    limit = RETRIEVAL_TOP_N if max_items is None else max_items

    fitted: List[Dict[str, Any]] = []
    used = 0
    for ev in list(evidence)[: max(0, limit)]:
        item = dict(ev)
        item["subject"] = (item.get("subject") or "")[:EVIDENCE_ITEM_CHARS]
        item["body"] = (item.get("body") or "")[:EVIDENCE_ITEM_CHARS]
        cost = len(_format_evidence([item]))
        if fitted and used + cost > budget:
            break
        if not fitted and cost > budget:
            # Always show something: trim the first item's text to the budget.
            overflow = cost - budget
            subject = item["subject"]
            item["subject"] = subject[: max(0, len(subject) - overflow)]
            item["body"] = ""
            cost = len(_format_evidence([item]))
        fitted.append(item)
        used += cost
    return fitted
