# GrowthRadar

Autonomous AI web exploration agent that behaves like a real first-time user, explores SaaS
products, gathers evidence (screenshots, DOM, JavaScript, network traffic), and scores whether the
product is a strong UserGuiding prospect.

- **`Linear.md`** — the full behavioral spec (what the agent should do and why)
- **`Agents.md`** — engineering principles this codebase follows (simplicity, typed code, never
  conclude from one signal, fail gracefully, etc.)

Every module below cites the AGENTS.md/Linear.md section it implements in its own docstring —
that's the source of truth for *why* a given piece of logic exists.

## Setup

```bash
make install-dev   # creates .venv, installs deps, downloads the Chromium browser
cp .env.example .env
# fill in GROQ_API_KEY in .env (optional -- see Configuration below)
```

## Running the agent

Two ways to run it: the CLI, or a local clickable dashboard.

### CLI

```bash
make run URL=https://example.com

# or directly:
.venv/bin/python -m growthradar https://example.com --output markdown
.venv/bin/python -m growthradar https://example.com --output json --output-file report.json
.venv/bin/python -m growthradar https://example.com --headful --max-pages 5
```

Explores the target, attempts registration if a signup page is found, scores the result, and
prints the final report. See `python -m growthradar --help` for all options.

### Dashboard

```bash
make web
# then open http://localhost:8000
```

Paste a URL, click **Baslat**, and watch the run's status, score breakdown, screenshot gallery,
and past-run history in the browser -- no terminal needed. The dashboard is a thin FastAPI wrapper
(`web.py`) around the exact same `run_growthradar_session` the CLI calls; each run executes in a
background thread so the page can poll `/api/runs/{run_id}` for progress. It's a single-user local
tool -- state lives only for the life of the server process.

## Common tasks

```bash
make lint        # ruff
make format      # black + ruff --fix
make typecheck    # mypy
make test         # pytest
```

## Architecture

The agent is a pipeline of small, independently-testable modules under `src/growthradar/`. Each
one owns exactly one concern and writes its findings to a shared, append-only **Evidence store**
(SQLite) — nothing downstream re-fetches data another module already collected.

```
CLI (cli.py, __main__.py)              web.py -- FastAPI dashboard (same orchestrator call)
  └─ orchestrator.py  ── run_growthradar_session(url) / run_growthradar_batch(urls)
       ├─ browser.py          Playwright wrapper: launch/goto/retry/dismiss overlays,
       │                      passively records network requests
       ├─ exploration.py      breadth-first crawl (prioritizes signup/trial CTAs and
       │                      "Read more"-style article links; excludes OAuth/third-party
       │                      auth links entirely); on every page it calls into:
       │    ├─ screenshot.py       timestamped screenshots (kept for manual review --
       │    │                      no LLM analyzes them automatically)
       │    ├─ dom.py               URL/title/HTML/visible text/nav/interactive elements
       │    ├─ js_network.py        known onboarding/analytics/AI-chat-widget tool detection
       │    │                       (script/global/network signals)
       │    ├─ onboarding.py        combines dom+js+network into a heuristic verdict
       │    └─ page_classifier.py   confident Product-Updates/Help-Center/Docs/Blog tagging
       ├─ registration.py     fills & submits signup forms; identity.py + temp_email.py
       │                      generate realistic users and (optionally) verify email
       ├─ scoring.py          combines all evidence into ICP fit / onboarding opportunity /
       │                      product experience scores + a Hot/Warm/Cold verdict -- always
       │                      rule-based, never an LLM call
       ├─ llm_summary.py      optional: asks Groq for a plain-English "why" behind the
       │                      already-computed verdict (skipped without a Groq key)
       ├─ report.py           renders the Linear.md "Final Report" fields as Markdown/JSON
       └─ history.py          appends a one-line run summary for cross-run comparison

Cross-cutting:
  config.py       typed, validated settings loaded from .env (Config.from_env())
  event_log.py    structured, reproducible JSONL run log (logs/<run_id>.jsonl)
  evidence.py     the append-only SQLite Evidence store every module above writes to
```

**Data flow in one sentence:** the orchestrator opens a browser session and hands it to the
exploration engine, which visits pages breadth-first and, on each one, fans out to
screenshot/DOM/JS-network/onboarding/page-classifier collectors that all write typed `Evidence`
rows; once exploration and registration finish, `scoring.py` reads every evidence row back to
compute a verdict, `report.py` turns that into the final report, and `history.py` appends a
one-line summary so repeat runs against the same company can be compared. `web.py` doesn't
participate in this pipeline at all -- it just calls `run_growthradar_session` in a background
thread and reads the same evidence/history stores back to render a browser UI.

**No AI Vision step.** An earlier version of this project sent screenshots to an LLM to answer
onboarding questions. That's gone -- screenshots are still captured on every page, but only for a
human to look at afterward (CLI: `screenshots/<run_id>/`, dashboard: the screenshot gallery on
each run's report). `onboarding.py`'s heuristic verdict is based only on DOM content and JS/network
tool detection.

**Design principles that show up everywhere** (see `Agents.md` / `Linear.md` for the full list):
- **Never conclude from one signal** — `onboarding.py` and `scoring.py` only reach high confidence
  once 2+ independent evidence sources agree.
- **Never terminate on one failure** — every phase in `orchestrator.py` (exploration,
  registration, post-registration exploration, report persistence, history) is isolated behind its
  own `try/except`; a failed run still produces a report, flagged `partial_run=True`.
- **Evidence first, conclusion second** — `report.py` only reads what earlier stages already
  collected; it never fetches new data.
- **Pure functions where possible** — `onboarding.detect_onboarding_signals`,
  `scoring.score_run`, `report.generate_report`, and `page_classifier.classify_page` take
  already-collected data and return a result with no I/O, so they're unit-tested without a
  browser.

## Project layout

```
src/growthradar/   application code (see Architecture above)
  web.py            FastAPI dashboard: start/poll runs, render reports + screenshots in a browser
tests/             pytest suite -- one test file per module, plus:
  test_e2e_fake_saas.py   full pipeline against a local, fully offline fake SaaS site
evidence/          collected evidence per exploration run (gitignored)
screenshots/       timestamped screenshots per run -- browse these manually, nothing analyzes
                   them automatically (gitignored)
logs/              structured run logs (gitignored)
growthradar.db     SQLite: evidence + run_history tables (gitignored)
```

## Configuration

All configuration is loaded from environment variables (see `.env.example`) via
`growthradar.config.Config.from_env()`. Nothing is hardcoded — secrets must come from `.env`,
which is gitignored. `Config` validates itself on load (e.g. scoring weights must sum to 1.0) and
raises `ConfigError` with a clear message if something's wrong.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` / `GROQ_MODEL` | — / `llama-3.3-70b-versatile` | Only LLM provider this project uses -- powers `llm_summary.py`'s plain-English explanation of the verdict only; scoring itself is always rule-based. Leave blank to skip it |
| `GROWTHRADAR_LLM_PROVIDER` | `auto` | `auto` picks Groq if a key is set, otherwise heuristic, or force `groq`/`heuristic` |
| `GROWTHRADAR_REQUEST_TIMEOUT` | `10` | Seconds Playwright waits per navigation/action |
| `GROWTHRADAR_MAX_PAGES` | `8` | Exploration budget per run |
| `GROWTHRADAR_CRAWL_DELAY` | `0.5` | Seconds between page visits |
| `GROWTHRADAR_USER_AGENT` | `GrowthRadarBot/0.1 ...` | Sent on every request |
| `GROWTHRADAR_DB_PATH` | `growthradar.db` | SQLite file for both the Evidence store and run history |
| `GROWTHRADAR_LOG_LEVEL` | `INFO` | Console log level |
| `GROWTHRADAR_COUNTRY` | `United States` | Country value used to fill signup/free-trial forms (registration.py handles both text inputs and `<select>` dropdowns) |
| `GROWTHRADAR_COMPANY` | — | Fixed company name for signup forms; leave blank to get a random generated one each run |
| `GROWTHRADAR_EMAIL` | — | Real address to use for signup forms (plus-tagged per run, e.g. `you+ab12cd@x.com`, so repeated runs against the same site don't collide on "already registered"); leave blank for a random `@example.com` address. Needed for sites that reject non-work-email domains |
| `GROWTHRADAR_GOOGLE_PROFILE_DIR` | — | Path to a persistent Chrome profile with an already-signed-in Google session (create one with `scripts/google_profile_bootstrap.py`) |
| `GROWTHRADAR_ALLOW_GOOGLE_OAUTH` | `false` | With a profile dir set, click "Continue with Google" instead of routing around it. Google-only -- Facebook/Microsoft/etc. OAuth buttons are still always skipped |
| `GROWTHRADAR_HOT_THRESHOLD` / `WARM_THRESHOLD` | `70` / `40` | Overall-score cutoffs for the Hot/Warm/Cold verdict |
| `GROWTHRADAR_WEIGHT_ICP_FIT` / `WEIGHT_ONBOARDING_OPPORTUNITY` / `WEIGHT_PRODUCT_EXPERIENCE` | `0.30` / `0.45` / `0.25` | Must sum to 1.0 — how the three scoring dimensions combine into the overall score |

## Contributing

1. `make install-dev` once, then `make test` before and after any change.
2. Follow the module boundaries above — a bug fix or new heuristic almost always belongs in one
   existing file, not a new abstraction (see `Agents.md`: "Keep changes minimal and isolated").
3. `make format && make lint && make typecheck && make test` must all be clean before considering
   a change done.
4. Tests never touch the real network: use `data:` URLs, Playwright `page.route()` interception,
   or hand-built `Evidence`/`DomSnapshot` objects for pure functions (see any `tests/test_*.py`
   for the established pattern). `tests/test_e2e_fake_saas.py` is the reference example for a
   full-pipeline test.
5. New behavior should be reflected in this README's Architecture/Configuration sections when it
   changes what a module owns or how the agent is configured (`Agents.md`: "Update documentation
   when behavior changes").
