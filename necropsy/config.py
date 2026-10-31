# necropsy/config.py — pinned model names, overridable via NECROPSY_* env vars
# ------------------------------------------------------------------
# Open-source AI is load-bearing: local LLM via Ollama + local embeddings.
# No closed API calls, no API keys in code.

# Embedding model: BAAI/bge-small-en-v1.5 (small, fast CPU, strong retrieval)
EMBED_MODEL = "BAAI/bge-small-en-v1.5"

# Ghost / narrator model: qwen3:8b (follows structured-output instructions)
# Fallback for 8 GB RAM: qwen3:4b
LLM_MODEL = "qwen3:8b"
LLM_FALLBACK = "qwen3:4b"

# Retrieval settings
RETRIEVAL_K = 10       # number of top commits to retrieve before re-ranking
RETRIEVAL_TOP_N = 5    # number of top commits to use in the prompt

# Date semantics: as_of is the end of that day in UTC
# Items use authored_at for filtering; committed_at stored for reference only
AS_OF_DAY_END = True   # treat as_of as end-of-day UTC

# Database path
DATA_DIR = "data"
DB_PATH = f"{DATA_DIR}/necropsy.db"

# Max commits to ingest (prevents accidental huge repos)
MAX_COMMITS = 5000

# Vector dimensionality (match embedding model output)
VECTOR_DIM = 384

# LLM generation config
LLM_TIMEOUT = 120      # seconds before Ollama generation timeout
LLM_RETRY = True       # retry once on transient Ollama errors
LLM_THINKING_DISABLED = True  # force thinking=disabled in Ollama chat

# All model names live only here. Swap via:
#   NECROPSY_EMBED_MODEL=... NECROPSY_LLM_MODEL=... make run