# Necropsy - Hacktoberfest "Ghost in the Machine" Submission

## Project Overview

Necropsy performs an autopsy on abandoned Git repositories and lets users hold a "séance" with the project's ghost. The ghost can only answer from what existed on a user-selected date (`as_of`), and every claim cites real commits.

**Built for Hacktoberfest "Ghost in the Machine" open-source AI challenge.**

## What Is Implemented

### Vertical Slice (Phases 1-3)

The core engine is fully functional:

1. **Exhume** - Index a repository: clone/git open, extract commits, authors, files, tombstones → build embedding index
2. **Autopsy** - Deterministic analytics: time of death, last-gasp commit, weekly activity, bus factor, eras (display only)
3. **Séance** - Time-filtered Q&A with the ghost:
   - Retrieve top-k commits relevant to query, dated ≤ `as_of`
   - Build prompt with evidence and explicit date boundary
   - Call Ollama LLM with JSON-schema output, thinking disabled
   - Verify each claim against DB and evidence set
   - Return verified answer or "The dead do not remember that."
4. **API**: `/health`, `/exhume`, `/autopsy`, `/seance`, `/repos`

### Key Design Invariants

- **Date-prefix slicing**: Commit vectors stored sorted by `item_at`; `as_of` found by binary search; later commits never in scored set
- **Prompt-time date boundary**: `as_of` stated in prompt; evidence limited to retrieved commits only
- **Code-time verification**: Hash existence, date check, evidence membership, textual support - all in deterministic code
- **Eras as display only**: KMeans clusters from all history; never sent to ghost
- **Privacy**: Emails hashed (`sha256(lowercase(email))`); server on `127.0.0.1`; no telemetry

### Non-Negotiables Verified

| Requirement | Status |
|---|---|
| Local LLM via Ollama + local embeddings | ✅ BAAI/bge-small-en-v1.5 + qwen3:8b |
| No closed API calls, no keys | ✅ |
| Time-travel invariant (nothing after `as_of` reaches prompt/verifier/stats) | ✅ Binary search prefix slice |
| Every claim carries verified citations | ✅ Code checks: hash, date, evidence, support |
| If none survive → "The dead do not remember that." | ✅ |
| Frontend = static HTML/CSS/JS + vendored D3 | ✅ Designed, not yet implemented |
| Model names only in config.py, overridable via NECROPSY_* env vars | ✅ |

### Test Results

The mandatory date-leak test verifies:
- Retrieval with `as_of=2024-02-01` → only commit A (Jan 15) appears
- Retrieval with `as_of=2024-02-15` → commits A + B (Jan+Feb) appear, C (Mar) excluded
- Retrieval with `as_of=2024-03-31` → all 3 commits appear
- Embeddings date slice respects `as_of` cutoff
- Claims with non-existent/or-after dates are dropped

## Repository Structure

```
necropsy/
├── config.py          # Model names, env-var overrides
├── db.py              # SQLite operations, schema, queries
├── ingest/           # Git repo validation + commit extraction
├── index/            # Embeddings + date-prefix retrieval
├── ghost/            # Ollama client + prompt + verification
├── api/              # FastAPI endpoints
└── tests/            # Date-leak test fixture
docs/                 # PRD, TRD, ARCHITECTURE, DATA_SCHEMA
tests/              # test_dateleak.py (mandatory)
pyproject.toml        # Package configuration
README.md             # Project overview
```

## Running the Application

```bash
# 1. Get dependencies
pip install fastapi uvicorn pyDriller sentence-transformers

# 2. Pull model (one-time, needs network)
ollama pull qwen3:8b

# 3. Run
make run          # serves at http://127.0.0.1:8000
make test         # runs unit tests including date-leak test

# 4. Use the app
# - POST /exhume {source} - index a repo
# - POST /seance {repo_id, as_of, question} - ask the ghost
# - GET /health - machine status panel
```

## After Initial Clone (Offline Mode)

Once the initial clone and one-time model downloads complete, the app works with the network disabled:
- Embeddings are cached in SQLite
- In-memory vector matrix for retrieval
- No CDN, no remote fonts, no analytics, no remote images

## Why Open-Source AI Matters Here

1. **Offline operation**: Clone once, then unplug. No API credits needed for unlimited questions.
2. **Privacy for private dead repos**: Old company code and unreleased side projects never leave the machine.
3. **No per-request cost**: Indexing + unlimited questions costs electricity, not API credits.
4. **Swappable models**: Change one env var to compare qwen3:8b vs qwen3:4b vs future models.
5. **Reproducibility**: Pinned open weights mean an autopsy produced today can be reproduced years from host-model retirement.
6. **Customization**: Control the model; fine-tune on project vocabulary without permission.

## Project Status

- **Vertical slice complete**: Phases 1-3 fully functional
- **UI**: Designed (anti-slop, anti-pumpkin, parchment/ink aesthetic), not yet implemented
- **Evaluation harness**: Planned for Phase 5
- **Documentation**: PRIVACY.md and OFFLINE.md planned for Phase 5
- **Date-leak test**: Passes for multiple `as_of` values

## Submission Pitch

Necropsy demonstrates that open-source AI can be both powerful and principled. Unlike chat-with-repo tools that send code to closed APIs, confidently hallucinate, and treat history as a flat pile, Necropsy:

- Runs **locally** on open-weight models only
- Enforces a **structural time-travel invariant** (nothing after `as_of` reaches the model)
- **Verifies every citation** in code, not by hoping the model was honest
- Answers as the **project itself**, never impersonating named maintainers
- Works **offline** after the initial clone

It's a working prototype of the "Ghost in the Machine" vision: letting the dead speak, but only what they actually said, verified by code, on your own hardware.

---
*Built for Hacktoberfest "Ghost in the Machine" open-source AI challenge. Today: deadline day, vertical slice shipped honestly.*