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
