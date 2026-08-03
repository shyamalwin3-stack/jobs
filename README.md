# Job Scout

A Flask + Playwright web app that scrapes recent job listings from eight job portals on demand. Pick a portal, fill in role + filters, get a sortable list of results in the browser, and download the run as an Excel file. No accounts, no API keys, no database.

## Supported portals

| Portal | Notes |
|---|---|
| LinkedIn | Public job search. Filters: role, multiple locations, posted-within, apply type (Easy Apply / External). |
| Glassdoor | City-based search with posted-age and apply preference. |
| Indeed | City-based search with posted-age and apply preference. |
| Hirist | Tech-only. Category-based (DevOps, Backend, Frontend, Full Stack, etc.). |
| Naukri | India's largest portal. Experience + posted-within filters. |
| Foundit | Formerly Monster India. Rich experience-range, location, skill data. |
| Apna | Apna.co — fastest scraper in the set. Salary bands + experience. |
| Shine | Shine.com — plain HTTP, multi-band experience filter. |

> **HiringCafe is temporarily disabled.** It sits behind Cloudflare Turnstile and the scraper hits a verification loop. The module and template are preserved in the repo for a later fix; the card is hidden from the landing page so you won't see it.

## Requirements

- Python 3.9 or newer
- Windows, macOS, or Linux
- ~500 MB free disk (Playwright downloads a Chromium build)
- Internet connection

## Setup

Clone the repo, then from the project root:

**Windows (PowerShell)**

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

**macOS / Linux (bash)**

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

The `playwright install chromium` step is required — it downloads the browser binary the scrapers drive. Skipping it will cause every scrape to fail with a "browser not found" error.

## Running

```powershell
python app.py
```

Then open <http://127.0.0.1:5000> in a browser. Pick a portal card, fill in the form, and hit search.

The server runs in Flask debug mode on port 5000 by default. To change the port:

```powershell
python -c "from app import app; app.run(port=8000)"
```

## How to use

1. **Landing page** lists all nine portals. Click any card.
2. **Portal page** shows the filters that portal supports — role, city/locations, posted-within, experience, apply type, etc. The available cities and categories are defined per scraper and surfaced to the form.
3. Hit **Search**. The page calls `POST /search/<portal>` and the scraper runs synchronously. Expect anywhere from ~10 seconds (Apna, Shine) to a couple of minutes (LinkedIn with detail-page enrichment) depending on portal and `limit`.
4. Results render in a table. Click **Download Excel** to get an `.xlsx` of the most recent search for that portal. Files are written into the project root as `jobs_<portal>.xlsx`.
5. There is no persistence between runs — restarting the Flask process clears all cached results.

## Project layout

```
app.py                  Flask routes (one page + one search + one download per portal)
scrapers/               One self-contained module per portal
  linkedin.py           Plain headless Chromium
  glassdoor.py          Plain headless Chromium
  indeed.py             Plain headless Chromium
  hirist.py             Plain headless Chromium
  shine.py              Plain headless Chromium
  naukri.py             Persistent profile + Chrome --headless=new (Akamai bypass)
  foundit.py            Persistent profile + Chrome --headless=new (Akamai bypass)
  apna.py               Persistent profile
  hcafe.py              Disabled — Cloudflare Turnstile; kept in tree for later
templates/              One Jinja template per portal + landing.html
static/                 CSS + favicon
_naukri_profile/        Auto-created Chrome profile dirs (do not delete or commit)
_foundit_profile/
_apna_profile/
_fi_profile/
jobs_<portal>.xlsx      Last downloaded export per portal (regenerated each download)
```

## Two browser strategies

Most scrapers run plain headless Chromium. **Naukri** and **Foundit** are protected by Akamai Bot Manager and need a different setup:

- They use `launch_persistent_context` against a profile directory at the project root (e.g. `_naukri_profile/`) so cookies and browser fingerprint persist between runs and Akamai treats the scraper like a returning user.
- They pass `--headless=new` to Chrome (true headless mode) while telling Playwright `headless=False` — this avoids the `HeadlessChrome` user-agent token that Akamai blocks. The browser window is never visible.

These profile directories are created automatically on first run. **Leave them in place** — deleting them means the next run starts cold and is much more likely to be challenged or rate-limited. They are runtime state, not source, and should be added to `.gitignore` if you publish the repo.

## Output

Each scrape returns a list of jobs with at least: `Job Title`, `Company`, `Location`, `Posted`, `Link`. Portals also surface any subset of: `Easy Apply`, `Apply Type`, `Experience`, `Workplace`, `Seniority`, `Rating`, `Salary`, `Skills`, `Industry`, `Description`, `Source ATS`. The Excel export normalizes column order and adds a `Source` column identifying the portal.

## Common issues

- **"Executable doesn't exist at .../chromium..."** — you skipped `playwright install chromium`. Run it inside your activated venv.
- **Zero results from Naukri or Foundit** — Akamai is challenging you. Wait a few minutes, then re-run; the profile directory will accumulate trust over successive runs. Do not delete `_naukri_profile/` or `_foundit_profile/`.
- **LinkedIn results are slow** — expected. LinkedIn requires opening each job's detail page to classify Easy Apply vs. External, so total time scales with `limit`.
- **`ModuleNotFoundError` on first run** — your venv isn't active, or you ran `pip install` outside it. Re-activate and reinstall.

## Disclaimer

This project scrapes public job listings for personal job-hunting use only. Respect each portal's Terms of Service and `robots.txt`. Avoid running with high `limit` values in tight loops — you will get rate-limited or IP-blocked. Do not redistribute scraped data.
