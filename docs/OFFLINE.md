# Necropsy Offline Operation

## After Initial Setup, No Network Required

Once the following steps are complete, Necropsy functions entirely offline:

1. **Clone a repository** (one-time, requires network)
2. **Pull the local models** (one-time, requires network):
   - `ollama pull qwen3:8b` (ghost/narrator model)
   - `ollama pull BAAI/bge-small-en-v1.5` (embedding model) — this is cached by sentence-transformers after download
3. **Run `make run`** (or `python -m necropsy.api`) — the server starts and serves the UI

## What Works Without Network

- **Exhume**: Index a previously cloned repository. No git operations or model downloads needed.
- **Autopsy**: Deterministic analytics from the local SQLite database.
- **Séance**: Ask the ghost and receive answers with verified citations. No LLM API calls leave your machine.
- **Time slider**: Rewind to any `as_of` date previously indexed. All data is in the SQLite database and in-memory vector matrix.

## What Requires Network

- **Initial clone**: `git clone <url>` — the only network step in the normal workflow.
- **Model downloads**: `ollama pull <model>` — first-time setup only.
- **Model changes**: Swapping `NECROPSY_LLM_MODEL` or `NECROPSY_EMBED_MODEL` requires re-pulling and re-indexing.

## Verifying Offline Mode

1. Start Necropsy: `make run`
2. Disconnect your machine from the network.
3. Index a repository that was previously cloned.
4. Ask the ghost questions — they should succeed without network access.
5. Check the Machine Status panel: `ollama_reachable` will report `false`, which is expected when the network is disabled.

## Model Cache Location

- `sentence-transformers` caches the embedding model in `~/.cache/huggingface/hub/`
- Ollama stores models in its configured data directory (typically `~/.ollama/`)

## Emergency: Model Missing

If the Ollama model is removed after indexing:

1. Re-pull: `ollama pull qwen3:8b`
2. The `/health` endpoint will report `model_missing` until the model is available again.
3. No database restructuring is needed — vectors remain in SQLite.