# necropsy/config.py — pinned model names, overridable via NECROPSY_* env vars
# ------------------------------------------------------------------
# Open-source AI is load-bearing: local LLM via Ollama + local embeddings.
# No closed API calls, no API keys in code.

import os

# Embedding model: BAAI/bge-small-en-v1.5 (small, fast CPU, strong retrieval)
EMBED_MODEL = os.environ.get("NECROPSY_EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# Ghost / narrator model: qwen3:8b (follows structured-output instructions)
# Fallback for 8 GB RAM: qwen3:4b
LLM_MODEL = os.environ.get("NECROPSY_LLM_MODEL", "qwen3:8b")
LLM_FALLBACK = os.environ.get("NECROPSY_LLM_FALLBACK", "qwen3:4b")

# Ollama endpoint (local only)
OLLAMA_BASE_URL = os.environ.get("NECROPSY_OLLAMA_URL", "http://127.0.0.1:11434")

# Retrieval settings
# Fewer, tighter evidence blocks mean far fewer prompt tokens on a CPU-only
# laptop, which is the dominant cost of a séance (see docs/DECISIONS.md D-013).
RETRIEVAL_K = int(os.environ.get("NECROPSY_RETRIEVAL_K", "6"))
RETRIEVAL_TOP_N = int(os.environ.get("NECROPSY_RETRIEVAL_TOP_N", "4"))

# Evidence budget handed to the model. The prompt is truncated to this many
# characters of evidence text; nothing over budget is shown (and therefore
# nothing over budget can be cited).
EVIDENCE_CHAR_BUDGET = int(os.environ.get("NECROPSY_EVIDENCE_CHARS", "1400"))
EVIDENCE_ITEM_CHARS = int(os.environ.get("NECROPSY_EVIDENCE_ITEM_CHARS", "200"))

# Date semantics: as_of is the end of that day in UTC
# Items use authored_at for filtering; committed_at stored for reference only
AS_OF_DAY_END = True   # treat as_of as end-of-day UTC


# Database path.
# Read from the environment on every call so tests can point at a temp DB
# (NECROPSY_DB_PATH) without importing order mattering.
def db_path() -> str:
    """Return the SQLite database path, honouring NECROPSY_DB_PATH."""
    return os.environ.get("NECROPSY_DB_PATH", os.path.join("data", "necropsy.db"))


# Legacy constant; prefer db_path() so runtime overrides apply.
DATA_DIR = os.environ.get("NECROPSY_DATA_DIR", "data")
DB_PATH = db_path()


def as_of_end(as_of: str) -> str:
    """Normalize an as_of date to the end of that day in UTC.

    The whole app treats as_of as "end of that day", so a bare date
    ("2024-02-01") becomes "2024-02-01T23:59:59Z" and commits made during
    that day are included. Strings that already carry a time are kept.
    """
    if not as_of:
        return "9999-12-31T23:59:59Z"
    value = str(as_of).strip()
    if "T" not in value and " " not in value:
        return f"{value[:10]}T23:59:59Z"
    if not value.endswith("Z"):
        value = value + "Z"
    return value

# Max commits to ingest (prevents accidental huge repos)
MAX_COMMITS = 5000

# Vector dimensionality (match embedding model output)
VECTOR_DIM = 384

# LLM generation config
# Cold-loading qwen3:8b on this machine takes ~85 s and a CPU-only
# generation can run to ~3 minutes, so the timeout is generous.
LLM_TIMEOUT = int(os.environ.get("NECROPSY_LLM_TIMEOUT", "300"))
LLM_RETRY = True       # retry once on transient Ollama errors
LLM_THINKING_DISABLED = True  # force thinking=disabled in Ollama chat
# Keep the model resident between requests (5 minutes by default).
LLM_KEEP_ALIVE = os.environ.get("NECROPSY_LLM_KEEP_ALIVE", "30m")
# Generation limits. A complete séance answer measured 253 tokens on qwen3:8b,
# so 448 leaves headroom without letting a runaway generation run forever.
LLM_NUM_PREDICT = int(os.environ.get("NECROPSY_LLM_NUM_PREDICT", "448"))
# Context window: the prompt is a system line plus a small evidence block, so
# 2048 tokens is ample. A smaller num_ctx means a smaller KV cache and a
# noticeably faster prefill on CPU.
LLM_NUM_CTX = int(os.environ.get("NECROPSY_LLM_NUM_CTX", "2048"))

# All model names live only here. Swap via:
#   $env:NECROPSY_EMBED_MODEL="..."; $env:NECROPSY_LLM_MODEL="..."
