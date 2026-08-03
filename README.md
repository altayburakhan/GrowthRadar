# GrowthRadar

An **AI Growth Intelligence platform** that automatically identifies high-potential
companies for UserGuiding: it visits a company's public website, understands what
kind of product/business it is, evaluates onboarding quality and product-experience
signals, detects concrete opportunities, and produces a **Lead Score with explainable
reasoning** for every decision.

This is not a scraper with an LLM bolted on. It's a pipeline that separates *facts*
(deterministic, testable) from *judgment* (LLM-assisted, but always grounded in those
facts) so every score can be explained, reproduced, and trusted by a sales team.

## Why a company is (or isn't) a good lead

UserGuiding sells digital adoption / onboarding tooling. The system looks for the
combination that makes a company a strong prospect:

- **ICP fit** -- is it a self-serve, B2B SaaS business at all? (pricing model, signup
  CTAs, B2B language, hiring/growth signals, self-serve billing)
- **Onboarding opportunity** -- does it show gaps an onboarding tool would fix? (no
  onboarding/adoption tool detected + self-serve trial + feature-rich product = high
  opportunity; a competitor tool already installed = a displacement opportunity)
- **Product experience** -- how complex/mature does the product look, which affects
  how much users would benefit from guided onboarding?
- **Hard disqualifiers** -- e.g. UserGuiding is already installed (existing customer),
  or the site couldn't be verified as a real, active company at all.

The final **Lead Score (0-100)** is a deterministic, configurable weighted combination
of these dimensions, tiered into `hot` / `warm` / `cold` / `excluded`, with reasoning
and a recommended sales pitch angle attached to every result.

## Architecture

```
Input (domain/URL)
      |
      v
collection/            -- fetches pages with a headless browser, discovers key pages
                           (pricing, careers, docs...), extracts text/CTAs/pricing
                           signals, detects installed tech (deterministic, no LLM --
                           these are facts, not opinions)
      |
      v
analysis/
  evidence_builder.py   -- assembles a validated CompanyEvidence object
  icp.py                -- deterministic ICP-fit scoring from evidence
  disqualifiers.py       -- hard-exclusion rules (e.g. existing customer)
  llm/                   -- pluggable qualitative reasoning layer:
                             base.py       - LLMProvider interface
                             anthropic_provider.py - real reasoning via Claude
                             heuristic_provider.py - offline, deterministic fallback
      |
      v
scoring/scorer.py       -- combines dimension scores with configurable weights into
                           one Lead Score + tier (pure, deterministic, auditable)
      |
      v
storage/ + reporting/   -- persists to SQLite (append-only history), renders as
                           table / JSON / Markdown
```

Every layer only depends on the layer below it through typed Pydantic models
(`growthradar/core/models.py`). This is what makes the project easy to extend:

- **New tech to detect?** Add one entry to `collection/tech_signatures.py`.
- **New scoring dimension?** Add a scorer function returning a `DimensionScore`,
  wire it into `core/pipeline.py`, add its weight to `config.py`.
- **New LLM provider (e.g. a local Ollama model)?** Implement `LLMProvider.assess()`
  in `analysis/llm/`, no other code changes.
- **New storage backend (e.g. Postgres)?** Reimplement `storage/repository.py`
  against the same three functions; nothing above it changes.
- **A web UI or API later?** Call `growthradar.core.pipeline.analyze_company()` --
  it's the single entry point the CLI itself uses. (A Streamlit dashboard already does
  exactly this -- see `growthradar/dashboard.py`.)

## Key design decisions & trade-offs

- **Local-first LLM layer, with paid and free upgrade paths.** No API key required
  to run: `HeuristicProvider` gives deterministic, evidence-grounded reasoning with
  zero external calls. Set `ANTHROPIC_API_KEY` for Claude (`AnthropicProvider`), or
  `GROQ_API_KEY` for a free-tier-friendly alternative (`GroqProvider`, Llama 3.3 70B
  via Groq's fast API) -- both implement the same `LLMProvider` interface and return
  the same validated JSON schema, so the rest of the pipeline doesn't care which one
  ran. Auto-selection order is Claude > Groq > heuristic (first key found wins),
  overridable via `GROWTHRADAR_LLM_PROVIDER=anthropic|groq|heuristic`. If a live call
  fails mid-batch, the pipeline falls back to the heuristic provider rather than
  crashing the run.
- **Facts vs. judgment are split on purpose.** ICP fit and disqualifiers are pure
  rule-based code -- there's no reason to pay for or wait on an LLM to tell you a
  site has a pricing page. The LLM is reserved for genuinely subjective calls
  (product complexity, onboarding gaps, pitch framing), and even then its output is
  a fixed JSON schema, validated with Pydantic, combined into the final score by
  plain weighted-sum code -- never a freehand number from the model.
- **SQLite over Postgres for now.** Zero setup, ships with Python, and an
  append-only log is enough to track how a lead's score changes over time. Swapping
  in Postgres later only touches `storage/`.
- **Headless-browser fetching (Playwright), not a plain HTTP client.** A small,
  targeted set of pages (home, pricing, about, careers, docs, blog, changelog,
  customers) discovered from on-page links -- not a full site crawl -- is fetched
  with a real headless Chromium browser, so client-side-rendered (React/Vue/Next.js)
  sites yield their actual content instead of an empty pre-render shell.
- **robots.txt is honored** before any page is fetched, with a descriptive
  `User-Agent` and a configurable crawl delay -- this is meant to analyze a handful
  of public pages per prospect, not act as a mass crawler.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # or: pip install -e ".[anthropic,dev]"
cp .env.example .env               # optionally add ANTHROPIC_API_KEY or GROQ_API_KEY

playwright install chromium       # downloads the headless browser used for fetching
playwright install-deps           # Linux only; installs missing system libraries, needs sudo
```

## Usage

```bash
# Analyze a single company
python -m growthradar.cli analyze notion.so

# Get JSON or a shareable Markdown report instead of the table view
python -m growthradar.cli analyze notion.so --output json
python -m growthradar.cli analyze notion.so --output markdown

# Analyze a list of companies (one domain/URL per line)
python -m growthradar.cli batch examples/companies.txt

# List previously scored leads, e.g. only hot ones
python -m growthradar.cli list --tier hot --limit 10

# Re-show the latest stored result for a domain
python -m growthradar.cli show notion.so --output markdown
```

Or, after `pip install -e .`, the same commands are available as `growthradar analyze ...`.

### Dashboard (optional)

A thin Streamlit UI over the same `core.pipeline.analyze_company()` function used by the
CLI -- enter a domain, see the score/tier/reasoning, and browse previously scored leads:

```bash
pip install -e ".[dashboard]"   # or: pip install streamlit
streamlit run growthradar/dashboard.py
```

Every result view (fresh or from history) has a "Collected data (raw evidence)" section --
the actual page text, detected technologies, and signals a score was based on, not just
the final numbers.

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover the deterministic parts end-to-end without any network access: tech
detection, ICP scoring, the offline heuristic provider, extraction, and the scoring
math itself (weighting, tiering, disqualifier overrides).

## Configuration

All configuration is environment-driven (see `.env.example`) -- scoring weights,
hot/warm thresholds, request timeouts, crawl politeness, DB path, and LLM provider
selection. Nothing is hardcoded that a growth team would reasonably want to tune.
