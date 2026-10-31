# Necropsy Privacy

## What We Collect

- **Repository data**: Git history (commits, authors, file changes) that is publicly available from the analyzed repository. This data is read-only; Necropsy never modifies the repo.
- **Author emails**: Stored only as `sha256(lowercase(email))` hash. Raw emails are never written to disk or transmitted.
- **Usage metrics**: Machine status (model name, Ollama reachability, embeddings loaded status) and answer latency are reported via the `/health` endpoint to power the Machine Status panel. No individual user profiles are built.

## What We Do NOT Collect

- **No code transcription**: Your repository code stays on your machine. The initial `git clone` is the only network transfer.
- **No API keys**: No closed-model API keys are stored or transmitted.
- **No telemetry**: No analytics, no usage tracking beyond what is explicitly reported in the Machine Status panel.
- **No content upload**: Repository contents are never sent to a remote service after the initial clone.

## Data Retention

- All data stays in `data/necropsy.db` (SQLite) local to your machine.
- The server binds to `127.0.0.1` only; no external exposure.
- No data is sent to third parties.

## Deleting Your Data

- Delete the `data/necropsy.db` file and the `clones/` directory.
- Delete the browser session data (the static UI has no server-side persistence).