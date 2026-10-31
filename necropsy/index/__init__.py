# necropsy/index/__init__.py — Index package
# -------------------------------------------
# Public API:
#   • build_index(repo_id, model_name) -> dict with progress
#   • retrieve(query, as_of, k) -> list of (commit_hash, similarity, item_at)
#   • get_sorted_items(repo_id, model) -> (item_refs, item_ats, vectors)

from .embed import embed_text, compute_embeddings
from .store import store_embeddings, get_embeddings_by_date

# retrieve is imported directly from the module when needed
# from . import retrieve  # disabled to avoid import issues

__all__ = ["store_embeddings", "get_embeddings_by_date"]