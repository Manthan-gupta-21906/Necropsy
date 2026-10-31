# necropsy/index/embed.py — Embedding generation with sentence-transformers
# ----------------------------------------------------------------------
# Responsibilities:
#   • Encode commit texts into dense vectors (384 dim, L2-normalized)
#   • Batch encoding for efficiency
#   • Text hash computation for deduplication
#
# Model: BAAI/bge-small-en-v1.5 (download once, cache thereafter)
# After initial download, app works offline.

import hashlib
from typing import List, Tuple, Optional
import numpy as np

# Sentence-transformers is a dependency (per TRD §2)
# Model name from config: BAAI/bge-small-en-v1.5

try:
    from sentence_transformers import SentenceTransformer
    _MODEL = None
    _MODEL_NAME = None

    def _get_model(model_name: str = None) -> SentenceTransformer:
        """Load or return the cached SentenceTransformer model."""
        global _MODEL, _MODEL_NAME
        model_name = model_name or "BAAI/bge-small-en-v1.5"
        if _MODEL is None or _MODEL_NAME != model_name:
            _MODEL = SentenceTransformer(model_name)
            _MODEL_NAME = model_name
        return _MODEL
except ImportError:
    # sentence-transformers not available; will be installed via pip install -e .
    SentenceTransformer = None  # type: ignore


def embed_text(text: str, model_name: str = None) -> List[float]:
    """Encode a single text string into a 384-dimensional embedding vector.

    Returns list of floats (L2-normalized).
    """
    if SentenceTransformer is None:
        raise ImportError(
            "sentence-transformers not installed. "
            "Install with: pip install sentence-transformers"
        )
    model = _get_model(model_name)
    # encode returns numpy array; convert to list
    vec = model.encode(text, normalize_embeddings=True)[0]
    return vec.tolist()


def compute_embeddings(
    texts: List[str],
    model_name: str = None,
    batch_size: int = 32,
) -> List[List[float]]:
    """Batch-encode a list of texts into embeddings.

    Returns list of L2-normalized float lists (one per text).
    """
    if SentenceTransformer is None:
        raise ImportError(
            "sentence-transformers not installed."
        )
    model = _get_model(model_name)
    # batch encode with normalization
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=batch_size,
        show_progress_bar=False,
    )
    # Convert to list of lists
    return [v.tolist() for v in vecs]


def text_hash(text: str) -> str:
    """Compute SHA-256 hash of text for deduplication check."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()