"""Shine.com public job search scraper (no login required).

Shine is a Next.js app whose SSR-rendered page embeds the entire job-search
result inside `__NEXT_DATA__` at
`props.pageProps.initialState.jsrp.searchresult.data.results`.  Plain HTTP
works — no browser needed, on par with the Apna scraper for speed.

URL format:
    https://www.shine.com/job-search/{role-slug}-jobs-in-{city-slug}[-{page}]
        ?q={role}&loc={City}&sort=1
        [&fexp={1..7}]    (repeatable: each value is an experience band)

Pagination:
    page 1   →  /job-search/devops-jobs-in-bangalore
    page 2+  →  /job-search/devops-jobs-in-bangalore-{N}
    20 jobs per page; result wraps to page 1 when N > num_pages.

Experience bands (fexp values):
    1 = "< 1 Year"
    2 = "1 to 2 Years"
    3 = "3 to 5 Years"
    4 = "6 to 8 Years"
    5 = "9 to 10 Years"
    6 = "11 to 15 Years"
    7 = ">15 Years"

Each job record's keys (abbreviated by Shine):
    id, jJT (title), jCName (company), jLoc (list of locations),
    jPDate, jExp, jSal, jSlug, jJD (HTML desc), jKwd (skills CSV),
    jInd (industry), jLU (logo url).  Apply URL = https://www.shine.com/jobs/{jSlug}.

Freshness: Shine has no URL-level "posted within N days" filter — we
post-filter on `jPDate` and rely on `sort=1` (newest first).
"""

import requests
import re
import json
import time
from urllib.parse import quote_plus
from datetime import datetime, timedelta


# Display name -> Shine's city URL slug. Shine uses simple lowercase city
# names; Delhi/Gurgaon/Noida are interchangeable index pages of NCR.
SHINE_CITIES = {
    "Bangalore":  "bangalore",
    "Mumbai":     "mumbai",
    "Delhi":      "delhi",
    "Hyderabad":  "hyderabad",
    "Chennai":    "chennai",
    "Pune":       "pune",
    "Kolkata":    "kolkata",
    "Ahmedabad":  "ahmedabad",
    "Gurgaon":    "gurgaon",
    "Noida":      "noida",
    "Coimbatore": "coimbatore",
}

# Single fexp value -> human label (for status messages, also drives the UI)
SHINE_EXPERIENCE = {
    "1": "< 1 Year",
    "2": "1 to 2 Years",
    "3": "3 to 5 Years",
    "4": "6 to 8 Years",
    "5": "9 to 10 Years",
    "6": "11 to 15 Years",
    "7": ">15 Years",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>'
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _slug(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _build_url(role, city, page, fexp_values):
    role_slug = _slug(role)
    city_slug = SHINE_CITIES[city]
    suffix = f"-{int(page)}" if page and page > 1 else ""
    qs = [f"q={quote_plus(role)}", f"loc={quote_plus(city)}", "sort=1"]
    for v in (fexp_values or []):
        qs.append(f"fexp={int(v)}")
    return (f"https://www.shine.com/job-search/{role_slug}-jobs-in-{city_slug}"
            f"{suffix}?" + "&".join(qs))


def _extract_next_data(html):
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _parse_posted_date(ts):
    """jPDate is ISO-8601 like '2026-05-12T00:00:00'."""
    if not ts:
        return ""
    try:
        return datetime.strptime(ts[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return ""


def _clean_html(html):
    """Strip tags and collapse whitespace — Shine ships HTML descriptions."""
    if not html:
        return ""
    text = _HTML_TAG_RE.sub(" ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _format_locations(loc_list):
    if not loc_list:
        return ""
    if isinstance(loc_list, str):
        return loc_list
    # Drop "All India" if multiple specific cities are present.
    cleaned = [x for x in loc_list if x and x != "All India"]
    if not cleaned:
        cleaned = loc_list
    # Cap to 4 locations so the badge stays readable
    if len(cleaned) > 4:
        return ", ".join(cleaned[:4]) + f" +{len(cleaned) - 4}"
    return ", ".join(cleaned)


def _normalize_fexp(fexp):
    """Accept None, a single value, or a list. Return list of stringy ints."""
    if not fexp:
        return []
    if isinstance(fexp, (str, int)):
        fexp = [fexp]
    out = []
    for v in fexp:
        s = str(v).strip()
        if s and s in SHINE_EXPERIENCE:
            out.append(s)
    return out


def scrape_shine(role, city, fexp=None, posting_days=0, limit=20):
    """
    role:         free-text role (e.g. "DevOps")
    city:         display city (key of SHINE_CITIES)
    fexp:         optional list of experience-band ids (strings or ints, 1-7)
    posting_days: 0 = any; otherwise post-filter on jPDate within N days
    limit:        max results
    """
    if city not in SHINE_CITIES:
        raise ValueError(f"Unknown city: {city}")
    if not role.strip():
        raise ValueError("role is required")

    fexp_values = _normalize_fexp(fexp)

    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.shine.com/",
    }

    cutoff = None
    if posting_days and int(posting_days) > 0:
        cutoff = (datetime.today() - timedelta(days=int(posting_days))).date()

    all_jobs = []
    seen_ids = set()
    page = 1
    max_pages = 30  # safety: Shine caps long tails anyway

    while len(all_jobs) < limit and page <= max_pages:
        url = _build_url(role, city, page, fexp_values)
        print(f"[Shine] Fetching: {url}")
        try:
            resp = requests.get(url, headers=headers, timeout=25)
        except Exception as e:
            print(f"[Shine] Request error on page {page}: {e}")
            break
        if resp.status_code != 200:
            print(f"[Shine] HTTP {resp.status_code} on page {page}; stopping.")
            break

        data = _extract_next_data(resp.text)
        if not data:
            print(f"[Shine] No __NEXT_DATA__ on page {page}; stopping.")
            break

        try:
            sd = (data["props"]["pageProps"]["initialState"]
                       ["jsrp"]["searchresult"]["data"])
        except (KeyError, TypeError):
            print(f"[Shine] Search-result slice missing on page {page}.")
            break

        # When requested page > num_pages Shine wraps back to page 1 — break out
        # rather than scraping the same page twice.
        reported_page = sd.get("page") or 0
        if page > 1 and reported_page == 1:
            print(f"[Shine] Out of pages at page={page}; stopping.")
            break

        raw_jobs = sd.get("results") or []
        num_pages = sd.get("num_pages") or 1
        total = sd.get("count") or 0
        if not raw_jobs:
            print(f"[Shine] Empty results on page {page}; stopping.")
            break

        added_this_page = 0
        skipped_old = 0
        for jd in raw_jobs:
            if len(all_jobs) >= limit:
                break
            jid = jd.get("id")
            if not jid or jid in seen_ids:
                continue

            posted = _parse_posted_date(jd.get("jPDate"))
            if cutoff and posted:
                try:
                    pd = datetime.strptime(posted, "%Y-%m-%d").date()
                    if pd < cutoff:
                        skipped_old += 1
                        continue
                except Exception:
                    pass

            seen_ids.add(jid)
            slug = (jd.get("jSlug") or "").strip("/")
            apply_link = f"https://www.shine.com/jobs/{slug}" if slug else ""

            description = _clean_html(jd.get("jJD") or "")
            if len(description) > 800:
                description = description[:797] + "..."

            all_jobs.append({
                "Job Title": jd.get("jJT") or "",
                "Company": jd.get("jCName") or "N/A",
                "Location": _format_locations(jd.get("jLoc")),
                "Posted": posted or "",
                "Link": apply_link,
                "Experience": jd.get("jExp") or "",
                "Salary": jd.get("jSal") or "",
                "Skills": (jd.get("jKwd") or "").strip(),
                "Industry": jd.get("jInd") or "",
                "Description": description,
                "Apply Type": "Shine Apply",
                "Easy Apply": True,
            })
            added_this_page += 1

        print(f"[Shine] Page {page}: kept {added_this_page} jobs "
              f"(skipped_old={skipped_old}, total {len(all_jobs)}/{limit}; "
              f"shine_total={total}, num_pages={num_pages})")

        if page >= num_pages:
            break
        # If we filtered everything out due to date cutoff, results past
        # this page will be older still (sort=1 = newest first) — bail.
        if cutoff and added_this_page == 0 and skipped_old > 0:
            print(f"[Shine] All remaining jobs older than cutoff; stopping.")
            break
        page += 1
        time.sleep(0.4)

    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min
    all_jobs.sort(key=sort_key, reverse=True)
    return all_jobs[:limit]
