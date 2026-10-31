# Necropsy: Technical Requirements Document

**Status:** Draft v0.1 · **Date:** 2026-10-31 · Companion docs: `PRD.md`, `ARCHITECTURE.md`, `DATA_SCHEMA.md`

---

## 1. Technical goals

1. Everything the ghost says is traceable to commits dated on or before the selected `as_of` date.
2. All AI runs locally with open-weight models; the app works offline after setup.
3. Deterministic analytics (not the LLM) produce every number shown in the autopsy.
4. The design is simple enough for one developer to finish in 24-48 hours.

## 2. Technology choices

| Layer | Choice | Justification |
|---|---|---|
| Language | Python 3.11+ | Best ecosystem for Git, embeddings and clustering; matches the developer's skills |
| Git access | PyDriller (on GitPython); fallback: streamed `git log --numstat` parser | Easy commit/diff API; the fallback is faster on large repos |
| Code parsing | `tree-sitter` + language grammars (Python, then C or JavaScript) | Fast, language-agnostic symbol extraction without executing code |
| Storage | SQLite (stdlib `sqlite3`) | Single-file, zero-config, portable, easy to ship as an example |
| Vectors | NumPy matrix with brute-force cosine similarity | At ≤ 50k items it is faster than the overhead of a vector DB, and removes a dependency |
| Embeddings | `sentence-transformers` + `BAAI/bge-small-en-v1.5` | Small (33M params, 384 dims), strong quality for its size, CPU-friendly, MIT |
| LLM serving | Ollama (HTTP API on `localhost:11434`) | One-command local model management; schema-constrained JSON output |
| Clustering | scikit-learn (KMeans; HDBSCAN optional), `umap-learn` | Standard, well understood |
| Web backend | FastAPI + Uvicorn | Typed endpoints, automatic OpenAPI docs, async |
| Frontend | Static HTML/CSS/JS + D3 (vendored locally) | No build step; works offline |
| Testing | pytest, a programmatic fixture-repo builder | Deterministic tests without network |
| Packaging | `pyproject.toml`, Makefile | Reproducible setup |

Every new dependency must be added to this table with a one-line justification.

## 3. Models

| Role | Default model | Fallback (8 GB machines) | Why it is needed |
|---|---|---|---|
| Ghost / narrator (answers, era labels, resurrection plan) | `qwen3:8b` | `qwen3:4b` | Follows structured-output instructions and writes coherent explanations from evidence |
| Code-aware summaries (optional, diff-to-explanation) | `qwen2.5-coder:7b` | none | Understands diffs and code; improves explanations of *why* a change was made |
| Embeddings | `bge-small-en-v1.5` | same | Powers retrieval, the verification support check, and era clustering |
| Entailment checker (P2, off by default) | small NLI model (e.g. DeBERTa-based) | n/a | Checks whether a cited commit actually supports a claim, beyond similarity |

Notes:
- **Verify each checkpoint's license before pinning.** Qwen3 and Qwen2.5-Coder 7B are Apache-2.0 as of this writing, but some sizes of other families ship under different terms. Record the license next to the pinned name in `config.py`.
- **Thinking mode:** Qwen3 can emit reasoning text. For structured JSON output, disable thinking (Ollama's `think: false` option, or the model's no-think switch) and validate the output against the schema.
- **Pinning:** `config.py` holds model names and tags. Evaluation results store the config used, so results are reproducible.

## 4. Configuration (`necropsy/config.py`)

All values overridable by environment variables (`NECROPSY_*`).

| Key | Default | Meaning |
|---|---|---|
| `LLM_MODEL` | `qwen3:8b` | Ghost model |
| `CODE_MODEL` | `qwen2.5-coder:7b` | Optional diff explainer |
| `EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Embedding model |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `DATA_DIR` | `./data` | Where clones and the SQLite DB live |
| `MAX_COMMITS` | `20000` | Ingest cap |
| `TOP_K` | `8` | Evidence items per question |
| `SUPPORT_THRESHOLD` | `0.55` | Minimum claim-to-commit similarity (calibrate on the eval set) |
| `INACTIVITY_DAYS` | `365` | Gap length that defines "dead" |
| `ENABLE_NLI` | `false` | Turn on the entailment check |

## 5. Ingestion

### 5.1 Input validation
- Accept only `https://`, `ssh://`/`git@` URLs, or an existing local directory containing `.git`.
- Invoke git with argument lists (never `shell=True`). Reject values starting with `-` to prevent option injection.
- Never execute, install or import anything from the analyzed repo.

### 5.2 Commits
For each commit (non-merge by default; merges stored but flagged): hash, author (name, hashed email), `authored_at`, `committed_at`, subject, body, additions, deletions, changed files with change type (A/M/D/R) and rename info (`git -M`).

**Time basis:** use `authored_at` (UTC, ISO-8601) for all time-travel filtering. Author dates can be non-monotonic after rebases or with clock skew; the system sorts by `authored_at` and does not assume commit order equals time order. `committed_at` is stored for reference.

### 5.3 Bot detection
Mark authors as bots when the name matches a configurable pattern (e.g. `dependabot`, `renovate`, `[bot]`, `github-actions`). Bot commits are excluded from "last gasp" and bus factor.

### 5.4 Symbols and tombstones
- **File tombstones** come straight from git: a file with a delete (`D`) change. Epitaph = that commit's subject.
- **Symbol tombstones** (functions, classes, methods) use a per-file version walk: for each commit that modifies a supported file, parse the file's before and after versions with tree-sitter, diff the symbol sets, and open or close symbol lifespans.
- Symbol identity is `(file_path, name, kind)`. A rename inside a file is recorded as a death plus a birth (the P2 "reincarnation finder" can link them by embedding similarity).
- **Budget guard:** cap parses with `MAX_SYMBOL_COMMITS`. If exceeded, fall back to file-level tombstones only.
- Files in unsupported languages still get file-level tombstones.

### 5.5 Issues (P2)
One-time fetch (GitHub API) or import from a JSON export into a local table; after that, offline. Issue state for a given `as_of` date is derived from `created_at` and `closed_at`.

## 6. Indexing

### 6.1 What is embedded
For each commit: `subject + body + changed file paths + short diffstat`. Raw diffs are truncated (≤ 1,000 characters) or summarized; they are not embedded in full.

### 6.2 Storage and the time-slice trick
Embeddings are stored with `item_at` (the date used for filtering). At query time the repo's vectors are loaded into one NumPy matrix **sorted by `item_at`**, so "everything up to `as_of`" is a contiguous prefix found with a binary search. The date filter is therefore structural: items after the cut-off are never in the slice that gets scored.

### 6.3 Eras (presentation layer only)
Cluster the commit embeddings (KMeans, k chosen by silhouette score within a bounded range, or HDBSCAN) and project to 2D with UMAP. The LLM labels each cluster from its most central commit messages.

**Important:** cluster assignments depend on *all* commits, including those after `as_of`. Eras are therefore a visualization only. **Era labels and summaries are never placed in the ghost's context.**

## 7. Retrieval

```text
retrieve(query, as_of, k):
    n      = upper_bound(item_at_sorted, as_of_end_of_day_utc)
    M      = embedding_matrix[:n]                  # prefix slice, nothing later exists here
    q      = embed(query)
    scores = M @ q                                 # vectors are L2-normalised
    top    = argpartition(scores, -k)[-k:]
    return rows(top) sorted by score
```

Optional (P1): re-score the top 30 with a cross-encoder reranker (e.g. `bge-reranker-base`) and keep the best `k`.

## 8. The ghost

### 8.1 Prompt contract
- The system prompt states: you are the project; you may only use the EVIDENCE blocks; every claim must cite hashes from the evidence; if evidence is insufficient, set `remembers=false`.
- Evidence is passed in clearly delimited blocks (hash, date, author, subject, body excerpt). The current as-of date is stated in the prompt.
- Repository text is **untrusted**. The prompt tells the model to treat evidence as data and ignore any instructions inside it. In the MVP the ghost has **no tools**, so injected text cannot trigger actions.
- The ghost never adopts a named maintainer's identity; it quotes commit messages verbatim when it quotes.

### 8.2 Output schema (enforced via Ollama `format`)
```json
{
  "remembers": true,
  "answer": "string, in the project's voice",
  "claims": [
    { "text": "one verifiable statement", "commits": ["<hash>", "<hash>"] }
  ]
}
```

### 8.3 Verification (`ghost/verify.py`)
For each claim:
1. Every cited hash must exist in the DB for this repo.
2. Every cited commit must have `authored_at ≤ as_of`.
3. Every cited hash must be in the evidence set shown to the model (prevents citing hashes the model "remembered" from elsewhere).
4. **Support check:** cosine similarity between `embed(claim.text)` and the cited commit's embedded text must be at least `SUPPORT_THRESHOLD`. (The default is a starting point. Calibrate it on the eval set by sweeping thresholds and picking the best precision/recall trade-off, and report the chosen value.)
5. Optional (P2): NLI entailment (`claim` entailed by commit text).

Claims failing any check are removed, and the answer text is regenerated from surviving claims (or the answer is dropped). If no claim survives, the response is the fixed refusal: **"The dead do not remember that."**

### 8.4 Failure handling
- Invalid JSON: one retry with the validation error appended; then the refusal reply.
- Ollama down or model missing: a clear UI error naming the missing model and the `ollama pull` command.
- Timeouts: configurable, default 120 s.

## 9. Coroner (deterministic analytics)

All are computed on commits with `authored_at ≤ as_of`.

| Metric | Definition |
|---|---|
| **Weekly activity** | Non-merge, non-bot commits per ISO week |
| **Last gasp** | The most recent non-bot commit that touches at least one non-documentation file |
| **Time of death** | The last-gasp date, if the gap between it and `as_of` (or the repo's last commit if `as_of` is the end) is at least `INACTIVITY_DAYS`; otherwise "still breathing" |
| **Decline onset** | First week where the 12-week moving average of commits falls below 25% of the trailing 52-week peak and never recovers above 50% of it afterwards |
| **Bus factor** | Smallest number of authors whose combined share of lines changed in the 12 months before `as_of` is at least 50% |
| **Dependency age** (P1) | Age (as of `as_of`) of the most recent commit touching manifest/lock files; listed with the pinned versions parsed from them (no network lookup) |

These definitions are heuristics and are documented as such in the UI and README.

## 10. Resurrection plan (P1)
Generated from deterministic inputs (time of death, bus factor, stale manifests, top tombstones by lifespan and size, recurring TODO/FIXME mentions in commit messages) plus an LLM-written narrative. Output is Markdown. Every recommendation line carries citations, and uncited lines are dropped by the same verifier.

## 11. API summary

| Method | Path | Purpose |
|---|---|---|
| POST | `/exhume` | Start ingestion + indexing (`{source}`); returns a job id |
| GET | `/exhume/{job_id}` | Progress |
| GET | `/repos` | List indexed repos |
| GET | `/autopsy?repo_id&as_of` | Stats and series as of a date |
| GET | `/graveyard?repo_id&as_of&kind&path` | Tombstones alive-or-dead as of the date |
| GET | `/eras?repo_id&as_of` | Era map points (display only) |
| POST | `/seance` | `{repo_id, as_of, question}` → verified answer |
| GET | `/resurrection-plan?repo_id&as_of` | Markdown export |

Full request/response examples belong in `docs/API.md`.

## 12. Performance targets

| Operation | Target (CPU-only, 16 GB) |
|---|---|
| Ingest 2,000 commits | < 60 s (file-level), < 3 min with symbols |
| Embed 2,000 commits | < 2 min |
| Retrieval | < 50 ms |
| Ghost answer | < 30 s with an 8B model on CPU (faster with GPU); results cached by `(repo, as_of, question, config)` |
| Slider update (stats only) | < 500 ms |

## 13. Security and privacy

- Local-only server bound to `127.0.0.1` by default.
- No telemetry; no outbound calls except `git clone`, one-time model downloads, and the optional issue fetch.
- Author emails stored as hashes only.
- Input validation on repo sources; no shell string interpolation.
- Prompt-injection posture: repo content is data; no tools in MVP; the P2 tool-using ghost may run only allow-listed, read-only git commands with validated arguments.
- Path traversal: all file paths come from git output and are never used to read arbitrary filesystem locations.

## 14. Testing strategy

| Test | Why |
|---|---|
| **Date-leak test (mandatory):** build a fixture repo with commits on known dates; for several `as_of` values assert retrieval, graveyard, autopsy stats, and the ghost's evidence set contain nothing dated after `as_of` | The core promise of the product |
| Verifier tests: fabricated hash, hash dated after `as_of`, hash outside the evidence set, unrelated claim text | Citation integrity |
| Ingest tests on the fixture repo: renames, deletions, merges, empty messages, bots | Correct data |
| Symbol lifecycle tests with small Python files | Tombstone accuracy |
| Config/env override tests | Swappable models |
| API smoke tests (FastAPI TestClient) | Endpoints work |
| Ghost tests with a **mock LLM** returning scripted JSON | Deterministic CI without a model |

## 15. Evaluation harness

Generate questions with ground truth from git itself:

- *Who last modified `<file>` before `<date>`?*
- *When was `<file>` deleted, and by what commit?*
- *What did commit `<hash>` change?*
- *Was `<symbol>` still present on `<date>`?*

For each model in a list, report **answer accuracy** and **citation validity** (before and after verification) in a Markdown table, along with the config and model digests. The README includes the table, whatever the results are.

## 16. Repository layout

```text
necropsy/
├── README.md  CONTRIBUTING.md  LICENSE  Makefile  pyproject.toml
├── docs/        PRD.md TRD.md ARCHITECTURE.md DATA_SCHEMA.md API.md UX.md WHY_OPEN.md DECISIONS.md EVALS.md
├── necropsy/
│   ├── config.py
│   ├── ingest/    git.py symbols.py issues.py
│   ├── index/     embed.py store.py
│   ├── coroner/   activity.py busfactor.py eras.py tombstones.py
│   ├── ghost/     retrieve.py prompt.py verify.py llm.py
│   ├── api/       server.py schemas.py
│   └── evals/     generate.py run.py
├── web/         index.html app.js graveyard.js timeline.js vendor/d3.min.js style.css
├── tests/       fixtures/ test_dateleak.py test_verify.py ...
└── examples/    sample autopsy output
```

## 17. Known limitations (state these in the README)

- Heuristic definitions (time of death, bus factor) are approximations, not facts.
- Commit messages are sometimes uninformative; the ghost can only be as good as the evidence.
- Small local models make mistakes; verification reduces but cannot eliminate unsupported *inferences* that happen to resemble a cited commit.
- Symbol tracking covers a few languages and treats in-file renames as death plus birth.
- Rewritten history (force pushes, squashed repos) limits what any tool can recover.
- Issue and discussion content is absent unless imported.

## 18. Definition of done (technical)

- `make test` passes, including the date-leak test.
- `make run` serves the app; a 500-commit repo indexes and answers questions with the network disabled after clone.
- The slider changes the graveyard, the autopsy stats and the ghost's answers.
- `make eval` prints the results table.
- The documents match the implementation.
