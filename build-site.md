---
description: Read every project document, then build the professional Halloween-themed Necropsy website in site/
agent: build
---

You are building the public documentation website for **Necropsy** (a local, offline tool that performs an autopsy on abandoned Git repositories and lets users hold a "séance" with the project's ghost, constrained by a time slider). The site explains the project, its architecture, its data schema and its file structure, with a polished Halloween theme. It is part of a Hacktoberfest "Ghost in the Machine" submission, so it must look professional, load fast, work offline, and be strictly accurate.

Extra instructions from the user (may be empty): $ARGUMENTS

Repository context right now:
!`ls -1 . docs site 2>/dev/null`

## 0. Standing rules (apply to every phase)

1. **Read before writing.** Do not write any site code until Phase 1 is finished.
2. **Accuracy over flair.** Every factual statement on the site must come from the project documents. Never invent features, metrics, benchmark numbers, screenshots, or user quotes. Where something is not built yet, label it **Planned** or **In progress**. Evaluation results stay "to be measured" until real results exist.
3. **The documents are the source of truth.** If two documents disagree, prefer the more specific one (DATA_SCHEMA for tables, TRD for technical details, ARCHITECTURE for structure, README for public wording), record the conflict in `docs/SITE_BRIEF.md` under "Discrepancies", and do **not** silently edit the source documents.
4. **Offline-first, zero runtime dependencies.** Plain HTML, CSS and vanilla JavaScript (classic scripts, no ES-module imports, no build step), so the site works by double-clicking `index.html` and on any static host. No CDN, no external fonts, no analytics, no remote images. External `https://` URLs are allowed only as ordinary links (`<a href>`) to the GitHub repo.
5. **Log your reasoning.** Follow the decision-log procedure in `AGENTS.md` (and the `/log` command): append an entry to `docs/AGENT_LOG.md` for every significant choice, deviation, assumption, or reversal, at the moment you make it, not at the end.
6. **Use the right skill.** Follow the `/frontend-skill` procedure before any UI work (Phase 2).
7. Keep commits small, one per phase or sub-task, with conventional commit messages (`feat(site): ...`, `docs: ...`). Do not commit generated junk or `node_modules`.
8. If something is ambiguous, state your assumption in `docs/AGENT_LOG.md` and continue. Only stop if you are blocked.

## Phase 1: Read everything and build a fact base

Read these in full:

@README.md
@docs/PRD.md
@docs/TRD.md
@docs/ARCHITECTURE.md
@docs/DATA_SCHEMA.md
@AGENTS.md

Then list `docs/` and read any other documents that exist (for example `API.md`, `UX.md`, `WHY_OPEN.md`, `EVALS.md`, `DECISIONS.md`, `CONTRIBUTING.md`). Skim the source tree (`necropsy/`, `web/`, `tests/`) to determine what is actually implemented.

Create `docs/SITE_BRIEF.md` containing:

1. **One-paragraph summary** of the product in your own words.
2. **Audience** and what each audience should understand within 30 seconds, 2 minutes and 10 minutes.
3. **Claim inventory**: a table with columns `Claim | Source (file §section) | Status (planned / in progress / implemented) | Used in site section`. Include every claim the site will make. Determine status by inspecting the repository; default to **planned** if you cannot verify.
4. **Glossary** (autopsy, ghost, séance, tombstone, epitaph, as-of date, last gasp, eras, embedding, retrieval, verification), each in one plain sentence.
5. **Discrepancies and open questions** between documents.
6. **Content map**: the sections below, each with a one-line purpose and the claims it uses.

Append a log entry summarizing what you read and any discrepancies found. Commit.

## Phase 2: Choose skills and design system

1. Run the `/frontend-skill` procedure for the task "build a professional, accessible, Halloween-themed static documentation site". Load the project skill `halloween-docs-site` and any other suitable frontend/design/accessibility skill that is actually available. Record which skills you loaded and why.
2. Create the design tokens as CSS custom properties in `site/assets/css/tokens.css` (color, type scale, spacing, radii, shadows, motion durations) following the skill's palette and rules. Document the tokens briefly at the top of the file.
3. Typography: use system font stacks by default. Only vendor a display font into `site/assets/fonts/` if it is under an open license (for example an OFL font from a fontsource package), use `font-display: swap`, and keep a complete fallback stack. Record the font license in `site/assets/fonts/LICENSE-NOTES.md`.
4. Log the design decisions (palette, type, motion, layout) with the reason for each.

## Phase 3: Site structure

Create this structure (adjust only with a logged reason):

```text
site/
├── index.html            # single page with anchored sections and a sticky nav
├── 404.html
├── README.md             # how to preview, edit content, deploy
└── assets/
    ├── css/  tokens.css  base.css  components.css  sections.css
    ├── js/   data.js  main.js  nav.js  timetravel.js  architecture.js  schema.js  filetree.js  roadmap.js
    ├── img/  favicon.svg  og-image.svg
    └── fonts/ (optional)
```

All structured content (components, tables, file tree, roadmap, models) lives in `site/assets/js/data.js` as plain objects assigned to `window.NECROPSY_DATA`, derived from the documents. Do not load it with `fetch()` (that breaks on `file://`). Each data item that describes a feature carries a `status` field (`planned`, `in-progress`, `implemented`) taken from `docs/SITE_BRIEF.md`.

## Phase 4: Build the sections

Build in this order. Use semantic HTML (`header`, `nav`, `main`, `section`, `figure`, `footer`), one `h1`, a logical heading outline, and a visible "skip to content" link.

1. **Hero.** Title "Necropsy", tagline "Hold a séance with any dead repository.", a two-sentence explanation, a status chip ("In development" unless the repo proves otherwise), buttons for **GitHub** and **Explore the architecture**, and an atmospheric but restrained background (layered CSS fog and a moon glow; no stock imagery).
2. **The problem.** Why abandoned repos are hard to understand and why existing "chat with your repo" tools fall short (use the comparison in `PRD.md` §3).
3. **How it works.** The six steps (Exhume, Embed, Choose a date, Retrieve, Answer, Verify) as a vertical timeline with a short plain-English explanation each, sourced from the README.
4. **Time-travel simulator** (interactive; `timetravel.js`). A clearly labelled **illustrative simulation** (no real model, no real repo):
   - A synthetic timeline of about 40 fictional commits drawn as dots sorted by date.
   - A date slider styled as a moon-phase control. Moving it dims every commit after the chosen date and highlights the prefix that "exists", with a readout of the binary-search cut position.
   - Three canned example questions. For each, show: evidence retrieved (only from the prefix), the ghost's claims with commit hashes, and the verifier's verdict per claim (kept, dropped because dated after the as-of date, dropped because not in the evidence set, dropped because unsupported). One question must end with the refusal "The dead do not remember that."
   - A caption explaining why the filter is structural (sorted vectors plus prefix slice) rather than a post-filter, taken from `ARCHITECTURE.md` §5.
5. **Architecture** (interactive; `architecture.js`). Build the diagram as inline SVG generated from `data.js` (no Mermaid runtime):
   - Layered component diagram (browser, API, ingest, index, coroner, ghost, SQLite, embedding model, Ollama) that matches `ARCHITECTURE.md` §2. Hover or focus highlights a component's connections; click or Enter opens a detail panel with responsibility, dependencies, planned files, and status.
   - Two step-through flows with Previous/Next controls and keyboard support: the **Exhume** flow and the **Séance** flow (from `ARCHITECTURE.md` §4), each step explaining what happens and which components are involved.
   - A table "How the time-travel invariant is enforced" (storage, retrieval, prompt, verification, statistics, eras, tests).
   - A short "Architectural decisions" list from `ARCHITECTURE.md` §12 (decision, alternative, reason).
6. **Data schema** (interactive; `schema.js`). An entity-relationship diagram (SVG) and a table explorer: pick a table to see its columns, types, constraints, indexes and notes; a tab with the time-travel query patterns from `DATA_SCHEMA.md` §5 in readable code blocks with a copy button. Show the conventions (UTC ISO-8601, hashed emails, `as_of` semantics).
7. **Project structure** (interactive; `filetree.js`). A collapsible, searchable file tree built from `TRD.md` §16 and the README, where each file or folder has a one-line description and a status. Keyboard accessible (arrow keys, Enter/Space to expand).
8. **Models.** Cards for the ghost model, optional code model, embedding model and optional NLI checker, with role, default, fallback, and why each is needed. Include the "verify each checkpoint's license before pinning" warning prominently.
9. **Why open-source AI.** Six concrete cards (offline operation, privacy for private repos, no per-request cost, swappable models, reproducibility and preservation, customization), each tied to something Necropsy actually does.
10. **Evaluation.** Explain the harness and show the results table with "to be measured" cells. Never fabricate numbers.
11. **Limitations and ethics.** Honest, from `TRD.md` §17 and the README ethics section.
12. **Roadmap** (`roadmap.js`). Render the README checklist with progress derived from `data.js`; include a progress bar and filter by status.
13. **Documentation hub.** Cards linking to each document in the GitHub repo (`PRD`, `TRD`, `ARCHITECTURE`, `DATA_SCHEMA`, `README`). Put the repository URL in one constant in `data.js` so it can be changed in one place.
14. **Footer** with license note, a link to the repo, and a line about the model licenses.

## Phase 5: Halloween theme (follow the skill; summary)

- Direction: **elegant gothic, not cartoon.** Deep ink backgrounds, parchment-colored text, one ember-orange accent, and a cool spectral glow reserved for the "ghost" and time-travel elements.
- Atmosphere through CSS: layered fog gradients, subtle candle-flicker on the accent, tombstone-shaped cards with monospace "epitaph" captions, a moon glow behind the hero.
- Motion is slow, subtle and purposeful, and is fully disabled under `prefers-reduced-motion`.
- No autoplay audio, no jump scares, no gore, no clip-art pumpkins, no emoji walls, no novelty fonts.

## Phase 6: Quality assurance (do all of it, and fix what you find)

1. **Offline check:** search `site/` for `http://` and `https://` and for `<link>`/`<script>`/`@import`/`url()` references to remote hosts; the only remote URLs allowed are `<a href>` links. Open `index.html` from `file://` and confirm that every interactive section works.
2. **Accessibility:** WCAG AA contrast for all text and controls; visible focus rings; every interactive element reachable and operable by keyboard; ARIA only where semantics are not enough; diagrams have text alternatives; `prefers-reduced-motion` and forced-colors respected.
3. **Responsive:** verify layouts at 360, 768, 1280 and 1920 px widths; no horizontal scroll on the body; wide tables and code scroll inside their own container.
4. **Performance budget:** total page weight under 1 MB excluding optional fonts; no layout shift on load; images are SVG.
5. **Content accuracy audit:** walk the claim inventory in `docs/SITE_BRIEF.md` and confirm each claim on the site matches its source and has the correct status. Fix mismatches.
6. If tools are available (for example Playwright or Lighthouse via `npx`), run them and fix issues; if not, state that the check was not run. Never claim a check passed that you did not run.
7. Log the QA results, including anything you could not verify.

## Phase 7: Documentation and deployment

1. Write `site/README.md`: how to preview (open the file, or `python -m http.server -d site`), where content lives (`data.js`), how to update statuses as features ship, how to change the repo URL.
2. Add `.github/workflows/pages.yml` that deploys the `site/` folder to GitHub Pages using the official Pages actions (`actions/checkout`, `actions/configure-pages`, `actions/upload-pages-artifact`, `actions/deploy-pages`). Use the current major versions and note in the log that they should be re-checked.
3. Add a short "Website" section to the root README linking to the published site URL placeholder and to `site/README.md`. Do not claim the site is live until it is.

## Definition of done

- `site/index.html` opens from `file://` with all sections and interactions working and no console errors.
- No remote requests are made at runtime.
- Every claim traces to a source in `docs/SITE_BRIEF.md`, and planned features are labelled as planned.
- `docs/AGENT_LOG.md` contains an entry for every significant decision, deviation and assumption, with reasons.
- Skills used are recorded in the log.
- A final report lists: what was built, what was verified, what could not be verified, assumptions made, and suggested next steps.

Start with Phase 1 now. After each phase, print a short status report and continue automatically unless you are blocked.
