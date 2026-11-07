# SEO Inspector

One unified AI agent that takes a website URL, scans the site against a fixed set of SEO
factors, and produces a prioritised report of what is missing, what is broken, and how to fix it.

**Audit only.** SEO Inspector recommends and generates ready-to-use fixes. It never publishes,
sends or changes anything on the audited site or anywhere else.

## Build status

| Phase | Scope | Status |
|---|---|---|
| 1 | Crawler + Site Model, Technical Audit (6), Crawling & Indexing (7, crawl-based), Meta Tags (3), Image SEO (5, deterministic), Postgres job queue, reports, dashboard | **Built** |
| 2 | Performance (PageSpeed), Schema detection/validation/generation, Internal Linking | **Built** |
| 3 | LLM layer: keywords, intent, content gaps, rewrites, alt text, GEO content | **Built** (live test pending the API key) |
| 4 | Own data layer (keyword engine, Common Crawl authority, backlink discovery, Colab import), modules 10, 11, 12, 16 | **Built** (Brave-dependent parts tested with recorded responses until the key works) |
| 5 | Local SEO (13–15), dashboard polish, PDF export, audit comparison, deployment hardening | Planned |

Modules that are not built yet still appear in every report as **NOT_CHECKED** ("planned for
build phase N"), so the coverage percentage always shows how much of the audit was actually measured.

## How it works

```
URL ─► Crawler (once) ─► Site Model ─► check modules ─► scoring ─► reports
        │                    │              │
        │ robots.txt         │ pages, links,│ each module reads the Site Model,
        │ sitemaps, llms.txt │ images, SD,  │ never re-crawls, and returns one
        │ HTTP + JS render   │ site facts   │ result per declared check
```

* **Crawler** (`backend/app/crawler/`): async HTTP with a custom User-Agent, robots.txt,
  ≤ 2 requests/second per site, timeouts and retries with backoff. Redirects are followed
  manually so chains and loops are recorded. Headless Chromium renders a page only when the raw
  HTML is missing content (plus the homepage, to compare raw vs rendered HTML); one browser,
  one page at a time. Images and sitemaps are streamed; only image headers are kept.
* **Site Model** (`backend/app/sitemodel/`): per URL, status, redirect chain, timing, head tags,
  headings, text, content hash + simhash, links (anchor, rel flags), images, structured data
  (JSON-LD, Microdata, RDFa), NAP, bylines and dates. Per site: robots.txt (including AI crawler
  rules), sitemaps vs crawled URLs, llms.txt, HTTPS and URL-variant probes. Click depth, inbound
  links, page templates and duplicate clusters are computed after the crawl.
* **Check modules** (`backend/app/modules/`): one file per factor area. A module declares its
  checks (`CheckSpec`: impact, effort, why, fix) and returns exactly one `CheckResult` per check:
  `PASS | WARNING | FAIL | NOT_CHECKED (with reason) | N/A`. A module that crashes is caught and
  all its checks become NOT_CHECKED with the error; the audit continues.
* **Job queue** (`backend/app/queue/`): a Postgres `jobs` table claimed with
  `SELECT … FOR UPDATE SKIP LOCKED`, heartbeats, retry with backoff, and stale-job recovery.
  The worker runs one audit at a time. APScheduler runs maintenance tasks in the worker.
* **Reports** (`backend/app/reports/`): standalone HTML, CSV (one row per affected URL) and full
  JSON, stored in Replit Object Storage (Postgres fallback).

The full list of checks is in [docs/CHECKS.md](docs/CHECKS.md). An example report produced from
the bundled test site is in [docs/example-report.html](docs/example-report.html) and
[docs/example-findings.csv](docs/example-findings.csv).

### Adding a check module

Create `backend/app/modules/mNN_name.py` with a `CheckModule` subclass (key, number, name,
category, `needs`, `integrations`, `checks`, `async run(ctx)`), and register it in
`backend/app/modules/registry.py` in place of its `PlannedModule` placeholder.

## Scoring

Implemented in `backend/app/scoring/scorer.py`.

* **Check score.** PASS = 100. For site-level checks, WARNING = 50 and FAIL = 0. For checks
  measured over a population (pages, images, links):

  `score = 100 × (1 − s × min(1, √(affected ÷ total)))`, where s = 1.0 for FAIL and 0.5 for WARNING.

  The square root makes the first few affected pages count without letting one bad page zero
  the check. Example: 1 of 100 pages failing scores 90; 25 of 100 scores 50.
* **Category score.** Weighted mean of the measured check scores in the category. The weight is
  the check's impact: High 3, Med 2, Low 1. NOT_CHECKED and N/A checks are excluded.
* **Overall score.** Weighted mean of the category scores. Default weights: Technical 25,
  On-page 30, Performance 15, Schema 5, Off-page 10, Local 5, GEO 10 (editable in Settings).
  The weights are re-normalised over categories that have at least one measured check, for
  example when Local is N/A.
* **Coverage %.** For each module: measured check weight ÷ applicable check weight (planned
  modules = 0). For each category: the mean over its modules. Overall: the category-weighted
  mean. This is how much of the audit was actually measured.
* **Priority.** impact ÷ effort (High 3, Med 2, Low 1). Ties go to FAIL before WARNING, then to
  more affected URLs. "Top 10 fixes" are the first ten FAIL/WARNING checks in this order.

### Keyword difficulty (our own estimate, label "Estimate")

From the top 10 search results for the keyword (Brave Search), in `backend/app/datalayer/keywords.py`:

| Component | How it is measured | Default weight |
|---|---|---|
| authority | mean Authority Score of the ranking domains (domains outside the table count as 10) | 0.55 |
| content_depth | median word count of the top 5 ranking pages; 2,500+ words = 100 | 0.15 |
| exact_match_titles | share of the top 10 titles containing every word of the keyword × 100 | 0.15 |
| intent_match | share of results whose URL/title pattern matches the keyword's intent × 100 | 0.15 |

`difficulty = Σ weight × component ÷ Σ weight` over the components that could be measured (e.g.
content depth is skipped when ranking pages cannot be fetched). Weights are editable in
Settings → Data layer.

### Authority Score (Common Crawl, 0–100)

`combined_rank = √(harmonic-centrality rank × PageRank rank)`;
`score = round(100 × (1 − ln(combined_rank) ÷ ln(100,000,000)))`, clamped to 1–100.
Rank 1 → 100, rank 100 → 75, rank 10,000 → 50, rank 1,000,000 → 25. Domains outside the loaded
top N (default 1,000,000) are reported as "Low authority (outside top N)".

## Data sources in Phase 2

* **Performance (module 8).** PageSpeed Insights API v5, mobile and desktop, for the homepage plus
  the most-linked page of each of the largest page templates (default 5). For each Core Web Vital
  the report uses, in order: URL-level CrUX field data (real Chrome users, p75), origin-level CrUX
  field data, then Lighthouse lab data. Every value is labelled with its source. INP exists only in
  field data; without it, INP is NOT_CHECKED and lab Total Blocking Time is shown as a proxy.
  Thresholds: LCP ≤ 2.5 s, INP ≤ 200 ms, CLS ≤ 0.1 (good); > 4 s / 500 ms / 0.25 is poor (FAIL).
  Results are cached for 24 h. The key is sent in the `X-Goog-Api-Key` header, never in the URL.
* **Schema (module 9).** Existing JSON-LD, Microdata and RDFa are validated against the
  required/recommended properties in `backend/app/modules/schema_rules.py` (based on Google's
  structured-data docs). Expected types per page come from deterministic signals (homepage, product
  and article URLs, og:type, dates, prices, video embeds, nested paths for breadcrumbs). Generated
  JSON-LD is validated in code; anything the page does not state (a price, an upload date) is listed
  under `needs_input`, never invented. Social profiles are only added to `sameAs` when they are
  linked site-wide or match the brand; others are listed for confirmation.
* **Internal linking (module 4).** Orphans are sitemap URLs that no crawled page links to. If the
  crawl reached its page limit before following every link, orphans are a WARNING with a caveat
  instead of a FAIL. Link suggestions use TF-IDF similarity over page text (terms that appear on
  most pages, such as navigation and footer text, are ignored), require at least two shared topic
  terms, skip near-duplicate pages, and add pillar ↔ cluster links within URL sections.

## AI (LLM) layer

Implemented in `backend/app/llm/` and used by modules 1, 2, 3, 4, 5 and 17.

* **Models** (Settings → AI): `claude-sonnet-5-5` for judgment (keyword mapping, intent, content gaps,
  title/description rewrites, link review, GEO) and `claude-haiku-5-5` for vision alt text. Effort
  defaults to `low`. Sonnet calls opt into server-side refusal fallback.
* **Inputs.** The model only ever sees compact page summaries: URL, title, meta description, H1–H3
  outline and an excerpt of the page's *main* text. Raw HTML is never sent. Main text excludes
  sentences repeated across many pages (navigation, footers, template text). Page content is passed
  as delimited data with an instruction to ignore any instructions inside it.
* **Outputs** are JSON validated against a schema. Measurable things are computed in code, never
  asked of the model (title pixel width, description length, keyword presence, readability).
  Rewrites are re-checked against the same limits, and FAQ JSON-LD is validated like all schema.
  The model is told not to invent prices, specs, dates or claims; FAQ answers it cannot take from
  the page are left blank for the owner.
* **Cost control.** Before each call, the worst-case cost (input written to cache + the full
  `max_tokens` of output) is checked against the per-audit cap (default $2) and the monthly cap
  (default $30). If either would be exceeded, the call is skipped and the affected checks report
  NOT_CHECKED with the reason. Actual cost from the API's usage numbers goes into the usage ledger;
  the audit shows its total. Identical requests are served from a 30-day cache at no cost. Only the
  most important pages are analysed (default 60; content gaps 20; GEO 15; alt text 30 images).
* **Not asked of the model:** search volume, keyword difficulty, backlinks or rankings. These stay
  NOT_CHECKED until the keyword engine and data layer (Phase 4).

### Manual AI mode (no API key)

Settings → AI → provider `manual`. Audits save each AI request (prompt + JSON schema) instead of
calling an API; answers are supplied by hand (for example by Claude in a Claude Code session),
validated against the schema, and served from the cache when the audit is re-run:

```bash
PYTHONPATH=backend python -m app.llm.manual export answers/ --task keywords.map
```

```bash
PYTHONPATH=backend python -m app.llm.manual import answers/
```

Write each answer as `answers/<id>.answer.json` next to its `<id>.request.json`. Keyword mapping
must be answered first (other tasks use it), then re-run the audit and answer the rest. The AI
visibility test needs live web search and is NOT_CHECKED in this mode. Findings are labelled
"Claude (manual mode via Claude Code)".

## Own data layer (Phase 4)

* **Authority table.** The worker streams the latest Common Crawl domain-ranks file (≈2.5 GB gzip)
  over HTTPS, keeps the top N rows (default 1,000,000 ≈ 2–3 minutes) and stops downloading.
  Nothing is written to disk. It loads on first start, refreshes quarterly, and can be refreshed
  from Settings. The current and previous release are kept (≈60 MB per million domains incl. index).
* **Keyword engine.** Ideas from the keyword map, Google Autocomplete (seed + question words + a–z
  for the top seeds; ≤1 request/second; cached 30 days) and questions shown in search results.
  Volume, in order: Google Ads Keyword Planner → Search Console → Google Trends relative interest
  (best-effort; often rate-limited from server IPs) → NOT_CHECKED. A Trends index is converted to
  a volume only when a keyword with known volume can anchor it.
* **Backlinks.** Live discovery searches for pages mentioning the domain/brand (excluding the
  site), fetches each result (robots.txt respected) and keeps verified links — always labelled
  "Discovered backlinks (sample)". Pages that mention the brand without linking feed module 12 as
  unlinked mentions. Full referring-domain lists come from the Colab notebook below.
* **Budgets.** Brave queries: 60 per audit / 1,000 per month (usage ledger); search results cached
  7 days, keyword SERPs 30 days.

### Colab notebook: full referring domains (`notebooks/cc_inlinks.ipynb`)

1. Set the `IMPORT_API_TOKEN` secret in Replit (any long random string) and restart.
2. Settings → Watchlist: add your sites and competitors.
3. Open the notebook in Google Colab (File → Upload notebook), set `APP_URL` in the first cell,
   run all cells and paste the token when asked.
4. It streams the Common Crawl domain graph (vertices ≈1 GB, edges ≈7.6 GB; nothing large is
   stored), filters edges pointing at your watchlist with `awk`, and uploads one referring-domain
   list per domain to `POST /api/import/inlinks`. Expect roughly 20–60 minutes.
5. Re-run after new Common Crawl releases; the app keeps the two latest imports per domain and
   reports new/lost referring domains between them.

## Local development

Requirements: Python 3.11+, Node 20+, PostgreSQL 14+, and Chromium for Playwright.

```bash
python -m venv .venv && . .venv/bin/activate
```

```bash
pip install -r requirements.txt && playwright install chromium
```

```bash
npm --prefix frontend ci && npm --prefix frontend run build
```

```bash
export DATABASE_URL=postgresql://seo:seo@localhost:5432/seo ADMIN_PASSWORD=change-me SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(32))") PYTHONPATH=backend
```

```bash
python -m app.run
```

This applies migrations, then starts the web server on :8000 and the worker. Open
http://localhost:8000. For frontend hot reload, run `npm --prefix frontend run dev` (it proxies
`/api` to :8000).

A local test website with deliberate SEO problems:

```bash
PYTHONPATH=backend:. python -m tests.fixtures.site_server
```

Then audit `http://127.0.0.1:8765/`.

### Tests

```bash
PYTHONPATH=backend:. pytest -q
```

Unit tests cover every deterministic check, the parser, fetcher, sitemaps, robots.txt, image
header parsing, scoring, the job queue and the API. `tests/e2e/test_full_audit.py` runs a full
audit against the local test site through the real orchestrator, Postgres and Chromium.
Database tests use `TEST_DATABASE_URL` (default `postgresql+psycopg://seo:seo@localhost:5432/seo_test`)
and are skipped if it is unreachable. **The test database schema is dropped and recreated.**

## Deploying on Replit

1. **Import** the GitHub repository into a new Repl. `.replit` and `replit.nix` configure
   Python 3.11, Node 20, PostgreSQL and a system Chromium.
2. **Database.** Open the *Database* tool and create a PostgreSQL database. Replit sets
   `DATABASE_URL` automatically. Migrations run on every start.
3. **Object Storage.** Open the *Object Storage* tool and create a bucket. Reports are stored
   there automatically. Without a bucket, they go into Postgres.
4. **Secrets.** Open the *Secrets* tool and add at least `ADMIN_PASSWORD` and `SESSION_SECRET`
   (see the table below).
5. **Run** in the workspace to try it, then **Deploy → Reserved VM** (1 vCPU / 4 GB RAM to
   start). The build command installs Python packages and builds the dashboard; the run
   command is `python -m app.run`, which starts the web server and the worker in the same VM and
   restarts either if it dies.

Notes:

* The deployment filesystem is not persistent. All state is in Postgres and reports are in
  Object Storage.
* JavaScript rendering uses the Chromium from `replit.nix`. If it cannot start, the audit still
  runs and the JS-dependent checks are reported as NOT_CHECKED with the reason. *Not yet verified
  on a live Replit deployment.*
* The session cookie is marked `Secure` automatically on Replit deployments
  (`REPLIT_DEPLOYMENT` is set). Set `SESSION_COOKIE_SECURE=true` elsewhere behind HTTPS.

### Secrets

| Secret | Needed for | How to get it |
|---|---|---|
| `ADMIN_PASSWORD` | Login (required) | Choose a strong password. |
| `SESSION_SECRET` | Signing the session cookie (required) | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
| `ANTHROPIC_API_KEY` | LLM layer (Phase 3+) | console.anthropic.com → API Keys. |
| `BRAVE_API_KEY` | Search provider (keyword difficulty, backlinks, mentions, outreach) | api-dashboard.search.brave.com → subscribe to a plan → API Keys. |
| `PAGESPEED_API_KEY` | Performance (Phase 2) | Google Cloud Console → enable *PageSpeed Insights API* → Credentials → API key (restrict it to that API). |
| `IMPORT_API_TOKEN` | Colab inlinks import | Any long random string; paste the same value into the notebook. |
| `GCP_SERVICE_ACCOUNT_JSON`, `GCP_PROJECT`, `GCP_LOCATION` | Gemini via Vertex AI, optional (second engine in the AI-visibility test; model and prices in Settings → AI; $10/month cap) | Google Cloud Console → enable *Vertex AI API* → IAM → service account with role *Vertex AI User* → Keys → JSON. Paste the whole JSON as the secret. |
| `GSC_SERVICE_ACCOUNT_JSON` | Search Console, optional | Service account JSON key; add the service-account e-mail as a user of the Search Console property. |
| `GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`, `GOOGLE_ADS_CUSTOMER_ID` | Keyword Planner volumes, optional | Google Ads manager account → API Center → developer token (*Basic access* is required for real data) + an OAuth client and refresh token. |
| `GBP_SERVICE_ACCOUNT_JSON` | Google Business Profile, optional | Request Business Profile API access from Google, then use an account that manages the profile. |

Optional settings: `CRAWLER_CONTACT_URL` (added to the User-Agent), `PLAYWRIGHT_CHROMIUM_PATH`,
`BLOB_BACKEND` (`auto`/`replit`/`postgres`), `OBJECT_STORAGE_BUCKET_ID`, `LOG_LEVEL`.

The Settings page shows only whether each key is configured. Key values are never returned by
the API or written to logs.

## Settings

Editable on the Settings page and stored in Postgres. Each audit snapshots the settings it ran with.

* Crawl: max pages (default 100), max 2 req/s per site, JS render mode, link/image check caps.
* Budgets (hard caps): Brave 60 queries per audit / 1,000 per month; LLM $2 per audit / $30 per
  month; Gemini $10 per month; 30 alt-text images per audit; 20 keywords for difficulty;
  homepage + 5 templates for PageSpeed; AI visibility 10 prompts × 2 runs.
* Check thresholds (title/description lengths, image KB limits, slow response, …).
* Scoring category weights.
* Retention: full page text is kept for the last 3 audits per site. Findings and metrics are
  kept for every audit.

## Crawling politeness

User-Agent `Mozilla/5.0 (compatible; SEOInspector/<version>; …)`. robots.txt is obeyed, and
its Crawl-delay is honoured up to 10 s. If robots.txt returns 5xx, only the start page is fetched.
At most 2 requests per second per site (www and non-www count as one site). Requests time out
after 20 s and are retried twice with exponential backoff (respecting `Retry-After`). HTML bodies
are capped at 5 MB, and images are only read far enough to learn their size and dimensions.
