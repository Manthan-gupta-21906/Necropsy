# Decision log

Significant choices, deviations and assumptions, recorded when they were made.
Format: problem, options, decision, why, trade-offs, consequences.

---

## D-001 — `as_of` means end of day in UTC

**Problem.** The date filter compared raw strings, so `as_of=2024-02-01` cut
off commits made during 2024-02-01 (`"2024-02-01T10:00:00Z" > "2024-02-01Z"`).

**Options.** (a) Interpret `as_of` as midnight, (b) as end of day.

**Decision.** `necropsy/config.py::as_of_end()` normalises a bare date to
`YYYY-MM-DDT23:59:59Z`; every query, the retrieval cutoff and the verifier use
that value.

**Why.** `docs/TRD.md` and the schema comments already state "as_of is the end
of that day in UTC".

**Trade-offs.** A caller that passes a full timestamp gets exactly that
timestamp (no expansion).

**Consequences.** `tests/test_dateleak.py` parametrises several `as_of` values
including the first and last commit days.

---

## D-002 — Eras are omitted from `/autopsy`

**Problem.** Era labels are computed by clustering the *full* history, so a
displayed era can encode commits dated after `as_of`.

**Options.** (a) Recompute eras per request, (b) filter eras by date, (c) omit
eras until a date-safe clustering pass exists.

**Decision.** (c). `/autopsy` returns no `eras` field at all.

**Why.** `AGENTS.md` non-negotiable 5 and 3: nothing dated after `as_of` may
reach any displayed statistic.

**Trade-offs.** The UI currently has no era timeline.

**Consequences.** When eras are implemented they must be clustered from the
prefix slice only.

---

## D-003 — `NECROPSY_DB_PATH` resolved at call time

**Problem.** Tests could not avoid `data/necropsy.db`, which is tracked in git
and contains real user data.

**Options.** (a) Pass a connection into every function, (b) a module-level
constant read at import, (c) a function read at call time.

**Decision.** (c): `config.db_path()` reads `NECROPSY_DB_PATH` on every call;
`db.py`, `index/store.py`, `ghost/verify.py` resolve the path per call.

**Why.** Import-order independence, and a test can point at a temp file with a
single environment variable.

**Trade-offs.** One function call per connection.

**Consequences.** `tests/conftest.py` sets the variable before importing any
necropsy module.

---

## D-004 — Charts are hand-drawn SVG, not D3

**Problem.** `web/vendor/d3/d3.min.js` is a 999-byte stub that defines no real
D3 API (`d3.scaleLinear()` returns `{domain, range}` with no output). Using it
would silently produce nothing.

**Options.** (a) Vendor the real D3 bundle (needs a download and a licence
check), (b) draw the one chart with the DOM/SVG API.

**Decision.** (b). `web/app.js` builds the monthly-activity chart with
`document.createElementNS`. The stub file is left in place but no longer
loaded by `index.html`.

**Why.** `AGENTS.md` requires a static frontend with no CDN and no build step;
the app must work offline. One bar chart does not justify a 280 KB dependency.

**Trade-offs.** No general-purpose charting API.

**Consequences.** If richer charts are needed, vendor real D3 and log the
licence here first.

---

## D-005 — `/autopsy` answers GET and POST

**Problem.** `docs/TRD.md` specifies `GET /autopsy?repo_id&as_of`, while the
original implementation only accepted `POST`.

**Decision.** `GET /autopsy` is the documented endpoint; `POST /autopsy` stays
as a hidden alias for older clients.

**Consequences.** The UI uses GET, which is cacheable and matches the docs.

---

## D-006 — Background indexing job in-process

**Problem.** Indexing a repository takes minutes (clone, PyDriller, embeddings).
A blocking request made the UI look dead.

**Options.** (a) `BackgroundTasks`, (b) a thread per job with an in-memory
registry, (c) a task queue (Celery/RQ).

**Decision.** (b): `POST /exhume` starts a daemon thread and returns a job id;
`GET /exhume/{job_id}` reports `stage`/`progress`/`error`.

**Why.** No new dependency, single-user local app, no broker to run offline.

**Trade-offs.** Job state is lost on restart; a second server process does not
see the first one's jobs.

**Consequences.** `stage` values are `queued`, `cloning`, `reading commits`,
`tombstones`, `embedding`, `ready`, `failed`.

---

## D-007 — PyDriller import at module load

**Problem.** Importing PyDriller inside the indexing thread took >15 s and
starved the event loop, so `/exhume/{job_id}` timed out mid-index.

**Decision.** Import PyDriller at module import time in
`necropsy/ingest/git.py` (cost paid at startup) and raise a clear error from
`_require_pydriller()` if it is missing.

**Consequences.** Server start-up imports PyDriller even for `/health`-only use.

---

## D-008 — PyDriller traversal order is not assumed

**Problem.** `traverse_commits()` returned oldest-first in PyDriller 2.12 on
this machine, not newest-first as the code assumed, so tombstone `born_at`
was the deletion date.

**Decision.** Sort commits by `author_date` explicitly before computing
first-seen paths.

**Consequences.** Tombstones report the real birth commit and lifespan.

---

## D-009 — LLM timeout 300 s, `keep_alive` 30 m, `think: false`

**Problem.** Cold-loading `qwen3:8b` plus a CPU-only generation took 177 s in a
measured séance, which is inside a 180 s timeout but with no margin.

**Decision.** `LLM_TIMEOUT` defaults to 300 s (`NECROPSY_LLM_TIMEOUT`), each
request sends `keep_alive: "30m"` and `think: false`, and a warm-up request
runs in a background thread at startup.

**Consequences.** `/health` reports the real warm-up state
(`not_started|warming|warm|error`), and the UI shows "loading the model"
instead of an indefinite spinner.

---

## D-010 — Errors are JSON, never stack traces

**Decision.** Domain errors raise `HTTPException` with
`{"detail": {"code", "message"}}`; a catch-all handler returns
`{"detail": {"code": "internal", "message": "<Type>: <message>"}}` and logs the
traceback server-side only.

**Consequences.** `tests/test_api.py` asserts the friendly message contains the
`ollama serve` command and no `Traceback`.

---

## D-011 — `/health` reads a cached snapshot, never Ollama

**Problem.** `/health` called `check_ollama()` (2 s probe) and a SQLite query on
every request. While `qwen3:8b` generated on CPU, Ollama's `/api/tags` did not
answer within 2 s, so `/health` both blocked and reported `reachable: false`,
`model: unknown`. The UI turned "busy" into "unreachable".

**Options.** (a) Cache the probe result in the endpoint with a TTL, (b) a
background thread that refreshes a snapshot every few seconds, (c) a separate
health sidecar process.

**Decision.** (b). `necropsy/api/server.py` keeps `_HEALTH` behind a lock; a
daemon thread (`health-refresh`) probes Ollama every `HEALTH_REFRESH_S` (3 s)
with the existing 2 s probe timeout, and also reads the embeddings/warm-up
state and the last séance latency. `GET /health` copies the dict and returns it:
no socket, no SQLite, no model on the request path.

**Why.** Status must be O(1) regardless of what the model is doing. A stale
value that is a few seconds old is far more useful than a 30 s request.

**Trade-offs.** `/health` can be up to ~3 s behind reality; it reports
`checked_at`, `age_ms` and `probe_failures` so clients can see that.

**Consequences.** Tests call `refresh_health_once()` explicitly after patching
the probe; `tests/test_health_busy.py` proves the endpoint never calls Ollama.

---

## D-012 — `busy` state; a timed-out probe while busy is not an outage

**Problem.** During a generation the probe times out, and treating that as
"Ollama down" was wrong: the machine was working, not broken.

**Decision.** `/seance` increments a counter around the threadpool call and
`/health` returns `busy: true`, `busy_reason: "answering"`, `status: "busy"`.
While busy, a failed probe keeps the last known good `reachable`,
`model_installed`, `models` and `error: null`; only `error_kind` records that
the probe timed out. Outside a séance, failures are reported as before.

**Consequences.** The UI shows "busy answering" (spectral pill) instead of
"unreachable", and only a failure while *not* busy can turn the panel red.

---

## D-013 — Evidence is fitted to a character budget

**Problem.** Ten evidence blocks with 200-character fields made a ~600–1000
token prompt for a 384-dim cosine search; on CPU the prefill is a large share
of the answer time, and the verifier accepted hashes that were retrieved but
never shown to the model.

**Decision.** `necropsy/config.py` sets `RETRIEVAL_K=6`, `RETRIEVAL_TOP_N=4`,
`EVIDENCE_CHAR_BUDGET=1400`, `EVIDENCE_ITEM_CHARS=220`. `prompt.fit_evidence()`
trims fields, drops whole items that do not fit and keeps at least one item;
`_seance` builds the evidence set from the *fitted* list, so the verifier only
accepts commits the model actually saw.

**Trade-offs.** Very long commit bodies are truncated; the hash and date are
always intact, so citations still verify.

**Consequences.** Measured prompt: 4 evidence commits, 199 chars of evidence,
1854–1992 prompt chars → 598–643 prompt tokens.

---

## D-014 — `num_predict=448`, `num_ctx=2048`, warm-up options must match

**Problem.** Nothing bounded the output, and the warm-up request used default
options while `chat()` used a different `num_ctx`. Ollama reloads the model
when `num_ctx` changes, so the first séance paid a ~70 s reload.

**Decision.** `LLM_NUM_PREDICT=448`, `LLM_NUM_CTX=2048` in `config.py`; both
`chat()` and `warmup_async()` send the same `num_ctx` (and `think: false`,
`keep_alive`). `chat()` returns and logs `prompt_eval_count`/`eval_count`.

**Why 448.** A real schema-constrained answer measured 253 completion tokens
(`done_reason: stop`); 448 leaves headroom without allowing an unbounded run.
The prompt also asks for ≤3 claims and short strings, and the schema's
`maxItems` is 3.

**Trade-offs.** A model that wants a longer answer is cut off and retried once,
which surfaces as a friendly `invalid_json`/timeout error rather than a hang.

**Consequences.** `scripts/check_llm.py` prints the token counts of a real call.
A full real séance on the fixture repo (4 evidence commits, 2130 prompt chars)
measured: `prompt_tokens=718`, `completion_tokens=240`, 181 s wall clock, and
3 proposed claims of which 1 verified — i.e. the tightened prompt produces a
citable answer instead of the refusal.

---

## D-015 — `sentence-transformers` is imported lazily

**Problem.** `necropsy/index/embed.py` imported `sentence_transformers` at
module load. On this machine that import (torch + transformers) took 87–244 s,
so uvicorn did not bind its socket for minutes and every `/health` call
"failed" simply because nothing was listening.

**Decision.** Import it inside `_load_class()`, called from `_get_model()`,
`embed_text()` and `compute_embeddings()`. The background warm-up thread pays
the cost; no request path does.

**Trade-offs.** A missing `sentence-transformers` is reported at first use
(`embeddings.state = "error"`) instead of at import.

**Consequences.** `python -c "import necropsy.api.server"` dropped from 244 s to
4.1 s and the server binds before the model is loaded.

---

## D-016 — The UI treats "busy" as work, and one failed poll as stale

**Problem.** A single slow `/health` poll (the old 6 s timeout) reset the panel
to `unknown` / `unreachable`, so a working machine looked dead.

**Decision.** `web/app.js` keeps the last good snapshot in `state.health`, raises
the health timeout to 15 s, and needs `HEALTH_FAILURE_LIMIT = 3` consecutive
failures before showing an error. Failures 1–2 only add `.is-stale` (values
dimmed, pill "stale"). Values are never blanked once a successful poll has been
seen. While a séance is in flight the panel shows "busy answering" and polling
is paused (`state.healthPaused`), then resumes with a forced poll.

**Why.** Status is advisory; it must not contradict the fact that the machine is
answering a question.

**Trade-offs.** Up to ~10 s (2 polls + 3 s snapshot age) can pass before a real
outage is shown as an outage.

**Consequences.** `scripts/check_ui.py` asserts the pill is `ok` on a healthy
server and that the Cancel button and progress line exist and start hidden.

