# necropsy/index/embed.py — Embedding generation with sentence-transformers
# ----------------------------------------------------------------------
# Responsibilities:
#   • Encode commit texts into dense vectors (384 dim, L2-normalized)
#   • Batch encoding for efficiency
#   • Text hash computation for deduplication
#   • Report a REAL loading state so /health never shows a hardcoded value
#
# Model: BAAI/bge-small-en-v1.5 (download once, cache thereafter)
# After initial download, app works offline.

import hashlib
import logging
import threading
from typing import List, Tuple, Optional
import numpy as np

logger = logging.getLogger(__name__)

try:
    from sentence_transformers import SentenceTransformer
except ImportError:  # pragma: no cover - depends on environment
    SentenceTransformer = None  # type: ignore

_MODEL = None
_MODEL_NAME = None
_MODEL_LOCK = threading.Lock()

# Loading state, read by /health. One of:
#   not_loaded | loading | ready | error
_STATE_LOCK = threading.Lock()
_STATE = {
    "state": "not_loaded",
    "message": None,
    "model": None,
}
_WARMUP_THREAD = None


def _set_state(state: str, message: Optional[str] = None, model: Optional[str] = None) -> None:
    with _STATE_LOCK:
        _STATE["state"] = state
        _STATE["message"] = message
        if model is not None:
            _STATE["model"] = model


def get_embed_state() -> dict:
    """Return a copy of the current embedding-model loading state."""
    with _STATE_LOCK:
        return dict(_STATE)


def _get_model(model_name: str = None) -> "SentenceTransformer":
    """Load or return the cached SentenceTransformer model.

    Updates the shared state to loading/ready/error around the load so
    /health can report the truth without issuing a request.
    """
    global _MODEL, _MODEL_NAME

    if SentenceTransformer is None:
        raise ImportError(
            "sentence-transformers not installed. "
            "Install with: pip install sentence-transformers"
        )

    model_name = model_name or "BAAI/bge-small-en-v1.5"

    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_NAME == model_name:
            return _MODEL

        _set_state("loading", None, model_name)
        try:
            _MODEL = SentenceTransformer(model_name)
            _MODEL_NAME = model_name
        except Exception as exc:  # noqa: BLE001 - surface the real reason
            _MODEL = None
            _MODEL_NAME = None
            _set_state("error", f"{type(exc).__name__}: {exc}", model_name)
            logger.exception("Failed to load embedding model %s", model_name)
            raise

        _set_state("ready", None, model_name)
        logger.info("Embedding model %s loaded", model_name)
        return _MODEL


def warmup_async(model_name: str = None) -> None:
    """Start loading the embedding model in a background thread.

    Idempotent: repeated calls while a warm-up is running are ignored.
    """
    global _WARMUP_THREAD

    with _STATE_LOCK:
        if _WARMUP_THREAD is not None and _WARMUP_THREAD.is_alive():
            return

    def _run() -> None:
        try:
            _get_model(model_name)
        except Exception:  # noqa: BLE001 - state already recorded
            pass

    _set_state("loading", None, model_name or "BAAI/bge-small-en-v1.5")
    _WARMUP_THREAD = threading.Thread(target=_run, name="embed-warmup", daemon=True)
    _WARMUP_THREAD.start()


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
    # encode returns numpy array; flatten defensively then convert to list
    vec = model.encode(text, normalize_embeddings=True)
    if hasattr(vec, "flatten"):
        vec = vec.flatten()
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
    vecs = model.encode(
        texts,
        normalize_embeddings=True,
        batch_size=batch_size,
        show_progress_bar=False,
    )
    return [np.asarray(v).flatten().tolist() for v in vecs]


def text_hash(text: str) -> str:
    """Compute SHA-256 hash of text for deduplication check."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
