# necropsy/index/__init__.py — Index package
# -------------------------------------------
# Public API:
#   • embed_text / compute_embeddings  -> local embedding model
#   • store_embeddings                 -> persist vectors
#   • get_embeddings_by_date           -> date-prefix slice (binary search)
#   • retrieve                         -> top-k commits dated <= as_of

from .embed import embed_text, compute_embeddings, get_embed_state, warmup_async
from .store import store_embeddings, get_embeddings_by_date
from .retrieve import retrieve

__all__ = [
    "embed_text",
    "compute_embeddings",
    "get_embed_state",
    "warmup_async",
    "store_embeddings",
    "get_embeddings_by_date",
    "retrieve",
]
