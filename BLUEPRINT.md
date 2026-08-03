# Job Scout — Project Blueprint

A design document covering what Job Scout is, how it's built, why it's built that way, and how to extend it. Read [README.md](README.md) first if you're a user; read this if you're a contributor.

---

## 1. Vision

A single browser-based tool that lets a job seeker pull fresh listings from major Indian + global job portals **without** signing in, without an API key, and without leaving a paper trail in a database. Each portal lives behind its own form on a unified Flask UI; results are previewed in the browser and downloadable as Excel.

Currently wired and working: **8 portals** (LinkedIn, Glassdoor, Indeed, Hirist, Naukri, Foundit, Apna, Shine). HiringCafe code lives in the tree but is disabled because Cloudflare Turnstile traps the scraper in a verification loop. See [§ 12](#12-known-limitations--future-work).

**Non-goals**: applying to jobs, storing user accounts, scheduled scraping, multi-user serving, analytics. This is a personal-use power tool, not a SaaS.

---

## 2. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Web framework | **Flask** | One-process, one-file, no boilerplate — fits a personal tool. No need for FastAPI's async since scrapers are sync anyway. |
| Browser automation | **Playwright (sync API)** | Better stealth + first-class headless modes than Selenium. Sync API keeps scraper code linear and debuggable. |
| Browser | **Chromium (bundled by Playwright)** | One install, reliable across OSes. Some scrapers also use Chrome's `--headless=new` mode to dodge bot detection. |
| Data shaping | **pandas + openpyxl** | Trivial dict-list → DataFrame → XLSX pipeline. No need for SQLAlchemy or anything heavier. |
| Frontend | **Vanilla Jinja templates + plain CSS + tiny inline JS** | No build step, no node_modules, no framework. Fast to ship, easy to read. |
| Persistence | **In-process dict + Chromium profile dirs on disk** | No DB. Result cache lives only for the lifetime of the Flask process. Browser profiles persist between runs to keep bot-detection trust. |

---

## 3. High-level architecture

```
┌──────────────────────────────────────────────────────────────┐
│                          Browser                              │
│  landing.html  ──►  linkedin.html, naukri.html, ... (forms)   │
└────────────┬──────────────────────┬──────────────────────────┘
             │ GET /<portal>        │ POST /search/<portal>
             │                      │      (form data)
             ▼                      ▼
┌──────────────────────────────────────────────────────────────┐
│                       Flask  (app.py)                         │
│                                                               │
│   page routes  ──►  render template with city/category lists  │
│   search routes ─►  validate ─► scraper ─► latest[portal]     │
│   /download/<src> ► DataFrame ─► XLSX ─► send_file            │
│                                                               │
│   in-memory:  latest = { "linkedin": [...], ... }             │
└─────────────────────────────┬────────────────────────────────┘
                              │ synchronous call
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                  scrapers/<portal>.py  (×9)                   │
│                                                               │
│   constants (cities, categories, exp bands)                   │
│   _build_url(filters)                                         │
│   _parse_*_date(text) ──► YYYY-MM-DD                          │
│   scrape_<portal>(...) ─► [{"Job Title":..., "Link":...}, ...]│
└─────────────────────────────┬────────────────────────────────┘
                              │ Playwright drives
                              ▼
┌──────────────────────────────────────────────────────────────┐
│   Chromium  (plain headless  OR  persistent profile + new HL) │
│        │                                                      │
│        └──► live portal HTML  (linkedin.com, naukri.com, ...) │
└──────────────────────────────────────────────────────────────┘
```

**One Flask process. One Chromium per scrape. No background workers.**

---

## 4. Request lifecycle (POST /search/&lt;portal&gt;)

1. Browser submits the form. JS in the portal template prevents default and `fetch`-es the same route.
2. Flask route parses form fields, coerces types (`int(...)`), and validates against the scraper module's constants (city must be in `NAUKRI_CITIES`, experience band must be in `SHINE_EXPERIENCE`, etc.). On failure: 400 JSON.
3. Flask calls the scraper function **synchronously** on the request thread. The request stays open for the entire scrape (can be 10s to 2min).
4. Scraper launches its own Chromium, drives the portal, parses cards, optionally opens detail pages, sorts by `Posted` descending, trims to `limit`, and returns a list of dicts.
5. Flask stores the list in `latest[portal]` and returns `{jobs, count, requested}` as JSON.
6. Browser renders the table from JSON. Excel download is a separate GET against `/download/<portal>` that reads from the same `latest` dict.

**Implication**: searches are slow and serial. The user is expected to wait. The dev server's `use_reloader=False` is intentional — a reload mid-scrape kills the Playwright process.

---

## 5. Scraper anatomy

Every `scrapers/<portal>.py` is self-contained and follows roughly the same skeleton:

```python
"""Module docstring with URL format and quirks."""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time, random, re
from datetime import datetime, timedelta

# 1. Constants surfaced to the UI
PORTAL_CITIES = {"Bengaluru": "bengaluru-slug", ...}
PORTAL_CATEGORIES = {...}     # optional, portal-specific
PORTAL_EXPERIENCE = {...}     # optional, portal-specific

# 2. URL builder (portal-specific)
def _build_url(role, city, ...): ...

# 3. Date parser ("2 days ago" → "YYYY-MM-DD")
def _parse_portal_date(text): ...

# 4. The public entry point
def scrape_portal(role, city, ..., limit):
    all_jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[...])
        # ... drive portal, parse cards, build dicts ...
    all_jobs.sort(key=lambda j: j["Posted"], reverse=True)
    return all_jobs[:limit]
```

**Why no base class?** Each portal has so many quirks (different bot-detection, different pagination model, different card markup, different date formats, different "no results" signals) that a base class would either be a god-object or force every scraper to override most methods. Copy-paste with intent is clearer.

---

## 6. The two browser strategies

### 6a. Plain headless (LinkedIn, Glassdoor, Indeed, Hirist, HiringCafe, Shine)

```python
browser = p.chromium.launch(headless=True, args=[
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    ...
])
context = browser.new_context(user_agent=..., viewport=..., locale=...)
context.add_init_script("""Object.defineProperty(navigator, 'webdriver', {get: () => undefined});""")
```

Standard anti-fingerprint tricks. Works because these portals don't run an active bot-management layer.

### 6b. Persistent profile + Chrome's new headless (Naukri, Foundit, also Apna and a second Foundit profile)

```python
ctx = p.chromium.launch_persistent_context(
    _PROFILE_DIR,                  # e.g. _naukri_profile/
    headless=False,                # tells Playwright not to add HeadlessChrome UA token
    args=[
        "--headless=new",          # Chrome's real headless: no UI, no telltale UA
        "--disable-features=AutomationControlled",
        ...
    ],
)
```

Two things matter:

1. **`launch_persistent_context`** writes cookies, localStorage, and Akamai's trust tokens (`_abck`, `bm_sz`, etc.) into a real profile directory at the project root. On the next run, Akamai sees a returning user instead of a fresh fingerprint. Trust accrues across runs.
2. **`headless=False` (Playwright) + `--headless=new` (Chrome args)** is the key trick. Playwright's `headless=True` mode appends a `HeadlessChrome` token to the user-agent that Akamai blocks instantly. Chrome's "new" headless mode renders without a window, doesn't change the UA, and presents as a real Chrome process. The combination gives us "invisible but indistinguishable."

The `_*_profile/` directories MUST NOT be deleted, gitignored-on-clone, or shared between users. They are personal browser state; losing them means starting cold and being challenged again.

---

## 7. Filter constants as single source of truth

Each scraper module exports its own constants (`PORTAL_CITIES`, `PORTAL_CATEGORIES`, `PORTAL_EXPERIENCE`). These are imported by:

- **`app.py`** — for form validation. Reject any city/category the form posts that isn't in the dict.
- **The portal's Jinja template** — rendered as `<option>` lists in the form.

This means the UI options, the validation, and the URL builder all agree by construction. Adding a city in one place would otherwise drift across three files; here it's one edit.

City dict shape varies by portal:

| Portal | Shape | Reason |
|---|---|---|
| Naukri | `{display: slug}` | Slug used in both URL path and `l=` param. |
| Foundit | `{display: (path_slug, query_value)}` | Path slug and the `location=` query are spelled differently (e.g. `delhi-ncr` vs `delhi ncr`). |
| Hirist | `{display: alt_spelling}` | Hirist uses `Bangalore` even though India officially uses `Bengaluru`. |
| LinkedIn / Glassdoor / Indeed | `{display: display}` | Portal accepts the free-text city verbatim. |

Always inspect the dict before adding entries.

---

## 8. Output schema

Each scrape returns `list[dict]`. The dict keys are **not** uniform — every portal contributes whichever fields are cheap to extract from its DOM.

Guaranteed keys (every portal):
- `Job Title`, `Company`, `Location`, `Posted` (YYYY-MM-DD), `Link`

Optional keys (subset depending on portal):
- `Easy Apply` (bool), `Apply Type` (Easy Apply / Company Site / External) — LinkedIn, Glassdoor, Indeed only
- `Experience`, `Workplace`, `Seniority`, `Rating`, `Salary`, `Skills`, `Industry`, `Description`, `Source ATS`

Excel export ([app.py:354](app.py#L354)) normalizes:
1. Renames `Company` → `Company Name`.
2. Injects a `Source` column (pretty portal name).
3. Reorders columns according to `column_order`; columns not present in the data are skipped.

**To add a new output column**: emit it from the scraper AND append it to `column_order` in `app.py`. Skipping step two means it'll be in the dict but invisible in the XLSX.

---

## 9. Adding a new portal (end-to-end checklist)

1. **Create `scrapers/newportal.py`** with:
   - `NEWPORTAL_CITIES` (and any category/experience dicts the portal exposes)
   - `_build_url(...)`, `_parse_newportal_date(...)`
   - `scrape_newportal(role, city, ..., limit)` returning the standard list-of-dicts
   - Module docstring with the URL format and any anti-bot caveats
2. **Re-export** in [scrapers/__init__.py](scrapers/__init__.py) (import + `__all__`).
3. **Wire into [app.py](app.py)**:
   - `from scrapers.newportal import NEWPORTAL_CITIES, ...`
   - Add `"newportal": []` to the `latest` dict
   - Add `@app.route("/newportal")` page route
   - Add `@app.route("/search/newportal", methods=["POST"])` search route with validation
   - Add `"newportal": "NewPortal"` to the source label map in `/download/<source>`
4. **Create `templates/newportal.html`** — copy the closest existing portal template (Hirist if it's category-based, Naukri if it's city-based) and rename fields.
5. **Add a card** in [templates/landing.html](templates/landing.html) inside `.portal-grid`.
6. **Document** in [README.md](README.md) supported-portals table and in this file's [§ 11](#11-portal-matrix).
7. **Smoke test**: `python -c "from scrapers import scrape_newportal; print(scrape_newportal(...))"` before wiring the UI.

If the portal uses Akamai or similar: also create a `_newportal_profile/` dir (Playwright creates it on first run), use `launch_persistent_context` + `--headless=new`, and document the trick in the module docstring.

---

## 10. Frontend conventions

- **One CSS file** ([static/style.css](static/style.css)) with all theming via `:root` variables and a `html[data-theme="dark"]` override block. Adding a new themeable color = add to both blocks.
- **Theme toggle** is duplicated inline in each portal template (no shared header partial yet). `localStorage` key is `jobscout_theme`.
- **Landing header** is a flex `.top-bar` with two visually matched pills:
  - `.dev-credit` (left) — "Built by Rajath" label + GitHub/LinkedIn `.social-btn` circles
  - `.theme-btn` (right) — moon/sun icon circle + label, mirrored layout via `flex-direction: row-reverse`
  - The two icon-circles inside the pills are both 30px so the pills have the same height. If you resize one, resize the other.
- **Tooltips** on social buttons are CSS-only: `data-tip="..."` attribute + `::after`/`::before` pseudo-elements driven by `:hover`/`:focus-visible`. No tooltip library.
- **Icons** are inline SVG paths (GitHub mark, LinkedIn mark, moon, sun) — no external requests, works offline, color-controllable via `fill="currentColor"`.

---

## 11. Portal matrix

| Portal | Bot defense | Launch mode | Filters exposed | Detail page hit? | Notes |
|---|---|---|---|---|---|
| LinkedIn | None active | `launch(headless=True)` | role, multi-location, posted-within (seconds), apply-mode | Yes (for Easy Apply classification) | Slowest. Over-fetches via `mode_multipliers`, trims at end. |
| Glassdoor | Light | `launch(headless=True)` | role, multi-location, from-age-days, apply-mode | Yes | |
| Indeed | Moderate | `launch(headless=True)` with helper `_launch()` | role, multi-location, fromage-days, apply-mode | Yes | |
| Hirist | None | `launch(headless=True)` | category, city, exp range, posting-days | Card-only (uses JSON-LD) | Tech-only; categories live in URL path slug. |
| ~~HiringCafe~~ | **Cloudflare Turnstile** | _disabled_ | _n/a_ | | Module preserved for later; needs patchright + system Chrome workaround. |
| Naukri | **Akamai** | persistent profile + `--headless=new` | role, city, experience (years), job-age | | India's biggest portal. Profile dir critical. |
| Foundit | **Akamai** | persistent profile + `--headless=new` | role, city, experience, freshness | | Formerly Monster India. Rich tagging. |
| Apna | Light | persistent profile | role, city, posted-in-days, min/max exp | | Fastest scraper. |
| Shine | None | `launch(headless=True)` (plain HTTP-ish) | role, city, multi-experience-band, posting-days | | Lightest portal. |

---

## 12. Known limitations & future work

| Limit | Why it exists | Possible fix |
|---|---|---|
| Single-user, in-memory result cache | Personal-tool scope. | Add SQLite + per-session keys if it ever becomes multi-user. |
| Synchronous request blocks Flask thread during a scrape | Simplicity. Playwright sync API doesn't release the GIL meaningfully anyway. | Move to a background task queue (RQ or Celery) and poll. |
| No retries on transient Playwright failures | Errors are reported back to the user as-is. | Wrap scrape entry points in a small retry decorator with backoff. |
| Test suite is absent | Targets are scraping live HTML — snapshot tests rot weekly. | Add unit tests around date parsers and URL builders; record-and-replay for full scrape paths. |
| `python app.py` runs Flask's dev server | Personal tool, no prod deployment. | If hosting: `waitress` or `gunicorn` + a real WSGI setup. |
| Akamai trust profile is shared across local runs but personal | By design. | None — these profiles must stay user-local, never committed, never shared. |
| HiringCafe disabled — Cloudflare Turnstile rejects scrapes | Even Patchright + system Chrome got stuck in a verify-loop on Reliance Jio (DoH-flavored DNS issues compounded the problem). | Revisit with `nodriver` (Python-native, stronger Turnstile bypass than Patchright) OR FlareSolverr running in Docker. When re-enabling, restore `scrapers/__init__.py` export, `app.py` import + routes, and the card in `landing.html`. The scraper module and template are intact. |

---

## 13. Repository layout

```
job_scraper/
├── app.py                     # Flask app: routes + result cache + XLSX export
├── requirements.txt
├── scrapers/
│   ├── __init__.py            # Re-exports scrape_* and constants
│   ├── linkedin.py
│   ├── glassdoor.py
│   ├── indeed.py
│   ├── hirist.py
│   ├── naukri.py              # Akamai-grade strategy
│   ├── foundit.py             # Akamai-grade strategy
│   ├── apna.py
│   ├── shine.py
│   └── hcafe.py               # Disabled (Cloudflare Turnstile); not exported
├── templates/
│   ├── landing.html           # Portal grid + dev-credit + theme toggle
│   └── <portal>.html          # One form-and-results page per scraper
├── static/
│   ├── style.css              # All theming via CSS variables
│   └── favicon.png
├── public/                    # Extra static assets (favicon)
├── _naukri_profile/           # Auto-created Chrome profile (do not commit)
├── _foundit_profile/
├── _apna_profile/
├── _fi_profile/
├── jobs_<portal>.xlsx         # Last-export-per-portal files (regenerated)
├── README.md                  # User-facing
├── CLAUDE.md                  # Agent-facing
└── BLUEPRINT.md               # This document
```
