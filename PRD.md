# Necropsy: Product Requirements Document

**Status:** Draft v0.1 · **Date:** 2026-10-31 · **Event:** Hacktoberfest "Ghost in the Machine" (open-source AI challenge)

> *Hold a séance with any dead repository.*

---

## 1. Summary

Necropsy ingests any Git repository and performs an **autopsy**: when it died, how it declined, who was left, and which pieces of code were lost. It then opens a **séance**: a chat with the project's "ghost" that can only answer from evidence dated on or before a date the user selects with a **time slider**. Every claim the ghost makes cites real commits, and claims that cannot be verified are removed.

All AI runs locally on open-weight models. After the initial clone, the app works offline.

## 2. Problem

Abandoned repositories hold years of decisions that no one can recover easily:

- *Why* was a feature removed? Was it a design choice or a burnout casualty?
- What did the project look like at its peak, and what has been forgotten since?
- Is it worth reviving, and where should a reviver start?

Today the answers are buried in thousands of commits and stale issues. Existing "chat with your repo" tools treat the repo as a flat pile of text: they have no sense of time, they hallucinate confidently, and most depend on a closed API, which rules out private or proprietary dead projects.

## 3. Vision and positioning

Necropsy treats a repository as something that **lived and died over time**. The time axis is the product, not a chat window bolted on top.

| Typical "chat with repo" | Necropsy |
|---|---|
| Flat retrieval over all history | Retrieval filtered by selected date *before* ranking |
| Free-form answers | Answers with verified commit citations; "I do not remember" when evidence is missing |
| LLM does everything | Deterministic forensics (decay, bus factor, tombstones) + LLM for explanation |
| Usually a closed API | Local open models, offline, swappable, reproducible |

## 4. Target users

| Persona | Need | How Necropsy helps |
|---|---|---|
| **Arjun, student/hobbyist** with abandoned side projects | Understand what he built and why he stopped; decide whether to revive | Autopsy of his own repo, the graveyard of deleted code, a resurrection plan |
| **Maya, maintainer/contributor** considering reviving a dead open-source project | Judge viability and find where to start | Cause-of-death analytics, outdated dependency list, good-first-revival tasks |
| **Dr. Lena, archivist / OSS historian** | Preserve and explain a project's history | Time-travel Q&A with citations, reproducible with pinned models |
| **Judges / reviewers** (event audience) | Quickly see a distinctive, working, open-source AI project | A 2-3 minute demo and a README with measured results |

Primary persona for the MVP: **Arjun**. The demo uses the author's own abandoned project.

## 5. Goals and non-goals

### Goals
1. **Open-source AI is load-bearing.** Embeddings and a local LLM are required for the core experience.
2. **Trustworthy answers.** Every claim carries a verified citation, and the ghost refuses when evidence is absent.
3. **Time travel is correct.** Nothing dated after the selected date can reach the model's context.
4. **Runs offline** after the initial clone, on a typical 16 GB laptop (CPU is acceptable, GPU optional).
5. **Demonstrable in under 3 minutes** and understandable from the README alone.
6. **Measurable.** An evaluation harness reports answer accuracy and citation validity per model.

### Non-goals
- Not a code-generation or auto-PR tool. Necropsy never opens pull requests and never modifies the analyzed repo.
- Not a general document chatbot. It understands Git history specifically.
- Not multi-user, hosted, or authenticated. It is a local single-user tool.
- Not impersonation of real people. The ghost speaks as the **project** and quotes real commit messages verbatim.
- Not a predictor of "why developers burn out." Analytics describe activity, not people's mental state.

## 6. User stories

**Exhume**
- As a user, I paste a repo URL or local path and see indexing progress so I know it is working.
- As a user, I can re-open a previously indexed repo without re-indexing.

**Autopsy**
- As a user, I see the project's time of death, last-gasp commit, activity curve and bus factor.
- As a user, I see the project's "eras" (phases of focus) on a map.

**Graveyard**
- As a user, I browse tombstones: deleted files and functions with the final commit message as the epitaph.
- As a user, I filter the graveyard by date, author, or file path.

**Séance**
- As a user, I drag a slider to a past date and everything (graveyard, stats, ghost) reflects that date.
- As a user, I ask the ghost questions and click each citation to read the underlying commit.
- As a user, I get an honest "The dead do not remember that." when there is no evidence.

**Resurrection**
- As a user, I export a Markdown resurrection plan: what to fix first, outdated dependencies, and suggested first tasks.

**Trust and openness**
- As a user, I can see which models are used and swap them by editing one config file.

## 7. Functional requirements

Priority: **P0** = MVP (24-hour cut), **P1** = full build (48 hours), **P2** = stretch.

| ID | Requirement | Priority |
|---|---|---|
| FR-1 | Ingest a Git repo from an https/ssh URL or an existing local path into SQLite (commits, authors, files changed) | P0 |
| FR-2 | Embed commit text locally and store vectors | P0 |
| FR-3 | Date-filtered retrieval: filter by `as_of` date first, then rank by similarity | P0 |
| FR-4 | Ghost answers as structured JSON with claims and commit citations | P0 |
| FR-5 | Citation verification: hash exists, is dated ≤ `as_of`, and supports the claim; unsupported claims are dropped | P0 |
| FR-6 | Fixed refusal reply when no claim survives verification | P0 |
| FR-7 | Autopsy stats: time of death, last-gasp commit, weekly activity, bus factor, all computed as of the selected date | P0 |
| FR-8 | Web UI with a time slider that re-queries stats and the ghost | P0 |
| FR-9 | Tombstones for deleted files; deleted functions/classes via tree-sitter (Python plus one more language) | P1 |
| FR-10 | Graveyard view | P1 |
| FR-11 | Eras: cluster commit embeddings, label with the LLM, 2D map | P1 |
| FR-12 | Resurrection plan export (Markdown) | P1 |
| FR-13 | Evaluation harness with ground truth derived from git | P1 |
| FR-14 | Issue ingestion from a one-time cached export | P2 |
| FR-15 | Tool-using ghost with a read-only git command allowlist | P2 |
| FR-16 | Reincarnation finder: map a deleted symbol to its most similar living one | P2 |
| FR-17 | NLI-model entailment check for citations (config flag) | P2 |
| FR-18 | Voice mode (speech-to-text in, text-to-speech out) | P2 |

## 8. Non-functional requirements

| Area | Requirement |
|---|---|
| **Offline** | After clone and one-time model downloads, no network access is needed. The frontend vendors all JS (no CDN). |
| **Privacy** | No telemetry. No data leaves the machine. Raw author emails are never stored (hash only). |
| **Performance** | Index 2,000 commits in under 5 minutes on CPU. Slider interaction updates in under 500 ms (retrieval) plus model latency for the ghost. |
| **Correctness** | Zero date leaks (enforced by a mandatory automated test). Citation validity of 100% after verification. |
| **Reproducibility** | Models pinned by name and version in `config.py`; evaluation results are stored with the config that produced them. |
| **Portability** | Linux, macOS, Windows (WSL acceptable). Python 3.11+. |
| **Resource use** | Default configuration fits in 16 GB RAM. A smaller fallback model is documented for 8 GB machines. |
| **Safety** | Analyzed repo content is untrusted data. Necropsy never executes code from it. |
| **Licensing** | MIT for the project; prefer permissively licensed models (verify each checkpoint's license before pinning). |

## 9. Scope

### MVP (24-hour cut)
Ingest, embeddings, date-filtered retrieval, cited ghost with verifier, autopsy stats, and a basic UI with the time slider. One polished demo repo.

### Full build (48 hours)
Add tombstones and the graveyard, eras map, resurrection plan, evaluation harness, polished UI, README and docs.

### Stretch
Issues, tool-using ghost, reincarnation finder, NLI verification, voice mode, a small LoRA fine-tune compared against plain retrieval.

## 10. Success metrics

| Metric | Target |
|---|---|
| Date leaks in tests | 0 |
| Citation validity after verification | 100% (every returned citation exists and is dated ≤ `as_of`) |
| Answer accuracy on git-derived eval questions (default 7-8B model) | ≥ 70% (to be measured and reported honestly, whatever the result) |
| Indexing time, 2,000 commits, CPU | < 5 min |
| Demo length | ≤ 3 min, one take, offline |
| README | Quickstart works from a clean machine in < 15 min |

## 11. Event requirements mapping (Ghost in the Machine)

| Challenge requirement | How Necropsy meets it |
|---|---|
| Open-source AI at the core | Local LLM (Ollama) and local embeddings are required for retrieval, verification and narration |
| Open components essential | Remove the models and there is no ghost, no era map, and no verification |
| Halloween / memory / digital afterlife theme | Dead repos, tombstones, ghosts, an autopsy of abandoned code |
| Explain why open innovation matters | `WHY_OPEN.md` and the README section: offline, private repos, zero per-query cost, swappable and pinned models, preservation |
| Demonstrable | 3-minute offline demo on the author's own abandoned project |

## 12. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Collapses into "yet another chat with your repo" | Low originality | Make time travel, tombstones and verified citations the visible centerpiece of the demo |
| Small local model hallucinates | Loss of trust | Schema-constrained output, verification, refusal path, evaluation reporting |
| Weak demo data | Flat demo | Use the author's own project with real history; keep a second small public repo as backup |
| Tree-sitter symbol tracking takes longer than planned | Missing graveyard detail | Fall back to file-level tombstones (from git itself), which is still a valid MVP |
| Slow CPU inference | Sluggish demo | Smaller fallback model; pre-index before recording; cache seance results |
| Deadline | Incomplete submission | 24-hour MVP defined above; docs and README written alongside code, not after |
| Impersonation concerns | Ethical objection | The ghost speaks as the project and quotes verbatim; no named-person personas |

## 13. Open questions

1. Which repo is the primary demo (author's own project vs. a public one)? Preference: author's own.
2. Which second language beyond Python gets tree-sitter support (C or JavaScript)?
3. Is issue data worth the setup friction for the MVP? Default: no (P2).
4. Is the NLI verification worth the extra model download? Default: off.

## 14. Glossary

- **Autopsy:** the analytics report on a project's life and death.
- **Ghost:** the LLM persona that answers as the project, strictly from retrieved evidence.
- **Séance:** the Q&A session with the ghost.
- **Tombstone:** a deleted file or symbol, with its birth, death and epitaph.
- **Epitaph:** the commit message of the commit that removed the item.
- **As-of date:** the date selected by the slider; the cut-off for all evidence.
- **Last gasp:** the last meaningful commit before the final period of inactivity.
