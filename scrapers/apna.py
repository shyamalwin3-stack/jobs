"""Apna (apna.co) public job search scraper (no login required).

Apna.co is a Next.js app whose server-rendered page already contains the
entire job feed in `__NEXT_DATA__`. We can fetch the page with plain HTTP
requests — no browser, no Playwright — making this the fastest scraper in
the project.

URL format:
    https://apna.co/jobs?location_id=0
        &location_identifier={CITY_OBJECTID}
        &location_type=NBCity
        &location_name={CITY_DISPLAY_NAME_URLENCODED}
        &search=true&text={ROLE}
        &raw_text_correction=true
        [&posted_in={DAYS}]
        [&min_experience={N}&max_experience={N}]
        [&page={N}]   // 25 jobs/page

Each job object inside `pageProps.jobs[].data` is rich: title, organization,
address, salary band, experience range, ui_tags, job_highlights, last_updated
timestamp and the canonical `public_url` for the apply page.
"""

import requests
import re
import json
import time
from urllib.parse import quote_plus
from datetime import datetime, timedelta


# Display name -> Apna's internal city ObjectId.
# Note: Apna does NOT have separate Gurugram/Noida cities — they roll up into
# "Delhi-NCR" (slug new_delhi).
APNA_CITIES = {
    "Bengaluru":  "64e4ad5bc35bd44248ca6899",   # Bengaluru/Bangalore
    "Mumbai":     "64e4ad5bc35bd44248ca6885",   # Mumbai/Bombay
    "Delhi NCR":  "64e4ad63c35bd44248ca7779",   # Delhi-NCR (covers Gurugram + Noida)
    "Hyderabad":  "64e4ad5bc35bd44248ca680d",
    "Chennai":    "64e4ad59c35bd44248ca63a1",
    "Pune":       "64e4ad3cc35bd44248ca5d52",
    "Kolkata":    "64e4ad63c35bd44248ca7735",   # Kolkata/Calcutta
    "Ahmedabad":  "64e4ad5bc35bd44248ca690b",
}

# Display name on Apna's side (used in the URL's location_name param)
_APNA_CITY_NAME = {
    "Bengaluru":  "Bengaluru/Bangalore",
    "Mumbai":     "Mumbai/Bombay",
    "Delhi NCR":  "Delhi-NCR",
    "Hyderabad":  "Hyderabad",
    "Chennai":    "Chennai",
    "Pune":       "Pune",
    "Kolkata":    "Kolkata/Calcutta",
    "Ahmedabad":  "Ahmedabad",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>([^<]+)</script>'
)


def _build_url(role, city, posted_in, min_exp, max_exp, page):
    city_id = APNA_CITIES[city]
    city_name = _APNA_CITY_NAME[city]
    qs = [
        "location_id=0",
        f"location_identifier={city_id}",
        "location_type=NBCity",
        f"location_name={quote_plus(city_name)}",
        "search=true",
        f"text={quote_plus(role)}",
        "raw_text_correction=true",
    ]
    if posted_in:
        qs.append(f"posted_in={int(posted_in)}")
    if min_exp is not None:
        qs.append(f"min_experience={int(min_exp)}")
    if max_exp is not None:
        qs.append(f"max_experience={int(max_exp)}")
    if page and page > 1:
        qs.append(f"page={int(page)}")
    return "https://apna.co/jobs?" + "&".join(qs)


def _extract_next_data(html):
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _parse_last_updated(ts):
    """Apna `last_updated` is ISO-8601; many records carry epoch-ish
    1970-* sentinels meaning 'unknown' — surface a blank for those."""
    if not ts:
        return ""
    try:
        dt = datetime.strptime(ts[:10], "%Y-%m-%d")
        if dt.year < 2000:
            return ""
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""


def _ui_tag_text(ui_tags):
    if not ui_tags or not isinstance(ui_tags, list):
        return [], "", ""
    skills = []
    workplace = ""
    employment = ""
    for t in ui_tags:
        txt = (t or {}).get("text", "")
        if not txt:
            continue
        low = txt.lower()
        if "work from" in low or "remote" in low or "hybrid" in low or "office" in low:
            workplace = txt
        elif "full time" in low or "part time" in low or "internship" in low or "contract" in low:
            employment = txt
        elif "min." in low and ("year" in low or "yr" in low):
            # min experience badge — surfaced separately below
            pass
        else:
            skills.append(txt)
    return skills, workplace, employment


def _format_experience(min_e, max_e):
    if min_e is None and max_e is None:
        return ""
    try:
        mn = int(min_e) if min_e is not None else 0
        mx = int(max_e) if max_e is not None else 0
    except Exception:
        return ""
    if mn == 0 and (mx == 0 or mx >= 31):
        return "Any experience"
    if mx >= 31:
        return f"{mn}+ yrs"
    if mn == mx:
        return f"{mn} yrs"
    return f"{mn}-{mx} yrs"


def scrape_apna(role, city, posted_in_days=0, limit=25,
                min_experience=None, max_experience=None):
    """
    role:           free-text role (e.g. "DevOps Engineer")
    city:           display city name (key of APNA_CITIES)
    posted_in_days: 0 = any time; 1/3/7/15/30 = posted within N days
    limit:          max results
    min_experience: optional int (0..30)
    max_experience: optional int (0..30)
    """
    if city not in APNA_CITIES:
        raise ValueError(f"Unknown city: {city}")
    if not role.strip():
        raise ValueError("role is required")

    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://apna.co/",
    }

    all_jobs = []
    seen_ids = set()
    page = 1
    max_pages = 25  # 25*25 = 625 (more than enough)

    cutoff = None
    if posted_in_days and int(posted_in_days) > 0:
        cutoff = (datetime.today() - timedelta(days=int(posted_in_days))).date()

    while len(all_jobs) < limit and page <= max_pages:
        url = _build_url(role, city, posted_in_days,
                         min_experience, max_experience, page)
        print(f"[Apna] Fetching: {url}")
        try:
            resp = requests.get(url, headers=headers, timeout=25)
        except Exception as e:
            print(f"[Apna] Request error on page {page}: {e}")
            break
        if resp.status_code != 200:
            print(f"[Apna] HTTP {resp.status_code} on page {page}; stopping.")
            break

        data = _extract_next_data(resp.text)
        if not data:
            print(f"[Apna] No __NEXT_DATA__ on page {page}; stopping.")
            break

        pp = (data.get("props") or {}).get("pageProps") or {}
        raw_jobs = pp.get("jobs") or []
        total_pages = pp.get("totalPages") or 1
        if not raw_jobs:
            print(f"[Apna] Empty job list on page {page}; stopping.")
            break

        added_this_page = 0
        for entry in raw_jobs:
            if len(all_jobs) >= limit:
                break
            jd = (entry or {}).get("data") or {}
            jid = jd.get("id")
            if not jid or jid in seen_ids:
                continue

            posted = _parse_last_updated(jd.get("last_updated"))
            if cutoff and posted:
                try:
                    pd = datetime.strptime(posted, "%Y-%m-%d").date()
                    if pd < cutoff:
                        # past cutoff — Apna doesn't reliably filter SSR by
                        # posted_in, so we post-filter ourselves.
                        continue
                except Exception:
                    pass

            org = jd.get("organization") or {}
            addr = jd.get("address") or {}
            skills, workplace, employment = _ui_tag_text(jd.get("ui_tags"))

            apply_link = jd.get("public_url") or ""
            external = jd.get("external_job_url") or ""
            is_external = bool(jd.get("is_external_job"))

            seen_ids.add(jid)
            all_jobs.append({
                "Job Title": jd.get("title") or "",
                "Company": org.get("name") or "N/A",
                "Location": jd.get("location_name") or addr.get("line_1") or "",
                "Posted": posted or "",
                "Link": apply_link,
                "Salary": jd.get("salary_detail") or "",
                "Experience": _format_experience(
                    jd.get("min_experience"), jd.get("max_experience")),
                "Workplace": workplace,
                "Skills": ", ".join(skills) if skills else "",
                "Description": (employment + (" • " if employment and is_external else "")
                                + ("External Apply" if is_external else "")).strip(" •"),
                "Apply Type": "External Site" if is_external else "Apna Apply",
                "Easy Apply": not is_external,
                "Source ATS": external if is_external else "",
            })
            added_this_page += 1

        print(f"[Apna] Page {page}: kept {added_this_page} jobs "
              f"(running total {len(all_jobs)} / {limit}; totalPages={total_pages})")

        if page >= total_pages:
            break
        if added_this_page == 0:
            # All filtered out this page — try one more then bail to avoid
            # infinite scan if posted_in filter is very tight.
            if page >= 3:
                break
        page += 1
        time.sleep(0.6)  # be polite

    # newest-first sort (blank posted dates sink to bottom)
    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min
    all_jobs.sort(key=sort_key, reverse=True)
    return all_jobs[:limit]
