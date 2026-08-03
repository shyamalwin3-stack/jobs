"""HiringCafe public job search scraper (no login required).

HiringCafe is a Next.js app that server-renders its job results into a
`<script id="__NEXT_DATA__">` block on every search page. We construct a
`searchState` JSON (city lat/lon + posted-within days + free-text query),
URL-encode it as a query param, fetch the page once, and parse the JSON.

URL reference (provided by user, decoded):
    https://hiring.cafe/?searchState=<URL-encoded JSON>

searchState shape we send:
    {
      "locations": [{
        "types": ["locality"],
        "address_components": [
            {"long_name": "Bengaluru", "short_name": "Bengaluru", "types": ["locality"]},
            {"long_name": "Karnataka", "short_name": "KA", "types": ["administrative_area_level_1"]},
            {"long_name": "India",     "short_name": "IN", "types": ["country"]}
        ],
        "geometry": {"location": {"lat": 12.97194, "lon": 77.59369}},
        "formatted_address": "Bengaluru, Karnataka, IN",
        "workplace_types": ["Remote", "Hybrid", "Onsite"],
        "options": {"radius": 100, "radius_unit": "miles", "ignore_radius": false}
      }],
      "dateFetchedPastNDays": 2,
      "searchQuery": "devops"
    }

Note: HiringCafe also accepts an opaque `id` per location (e.g. Bengaluru =
sxg1yZQBoEtHp_8UbmmX). We tested that omitting it still works as long as
lat/lon are provided — the backend uses coordinates for radius matching.
"""

# Patchright (a binary-compatible Playwright fork) is required for HiringCafe
# specifically: vanilla Playwright launches Chrome with --enable-automation,
# which trips Cloudflare Turnstile's silent-rejection path no matter how the
# checkbox is clicked. Patchright strips that flag at the binary level and
# overrides several CDP signals that Turnstile fingerprints on. Same API as
# playwright.sync_api — only the import line differs.
# Install: pip install patchright  &&  patchright install chrome
from patchright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta


_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "_hcafe_profile"
)


# Display city -> (lat, lon, state)
HCAFE_CITIES = {
    "Bengaluru":  (12.97194, 77.59369, "Karnataka"),
    "Hyderabad":  (17.38504, 78.48667, "Telangana"),
    "Mumbai":     (19.07283, 72.88261, "Maharashtra"),
    "Pune":       (18.51957, 73.85535, "Maharashtra"),
    "Chennai":    (13.08784, 80.27847, "Tamil Nadu"),
    "Delhi":      (28.65195, 77.23149, "Delhi"),
    "Noida":      (28.5708,  77.3260,  "Uttar Pradesh"),
    "Gurugram":   (28.4595,  77.0266,  "Haryana"),
}

# Map state long_name -> short_name used in HiringCafe's address_components
_STATE_SHORT = {
    "Karnataka": "KA",
    "Telangana": "TG",
    "Maharashtra": "MH",
    "Tamil Nadu": "TN",
    "Delhi": "DL",
    "Uttar Pradesh": "UP",
    "Haryana": "HR",
}


def _build_url(role, city, fetched_window_days):
    lat, lon, state = HCAFE_CITIES[city]
    ss = {
        "locations": [{
            "types": ["locality"],
            "address_components": [
                {"long_name": city, "short_name": city, "types": ["locality"]},
                {"long_name": state, "short_name": _STATE_SHORT.get(state, state[:2].upper()),
                 "types": ["administrative_area_level_1"]},
                {"long_name": "India", "short_name": "IN", "types": ["country"]},
            ],
            "geometry": {"location": {"lat": lat, "lon": lon}},
            "formatted_address": f"{city}, {state}, IN",
            "workplace_types": ["Remote", "Hybrid", "Onsite"],
            "options": {
                "radius": 100,
                "radius_unit": "miles",
                "ignore_radius": False,
                # Broadens to anywhere in the state (not just 100mi around the
                # city). HiringCafe's UI sets this by default.
                "flexible_regions": ["anywhere_in_administrative_area_level_1"],
            },
        }],
        # HiringCafe's dateFetchedPastNDays filters by when HC indexed/refreshed
        # the job, not when the company originally posted it. We fetch a wide
        # window and then post-filter by estimated_publish_date so the user
        # actually gets jobs posted within their chosen window.
        "dateFetchedPastNDays": int(fetched_window_days),
        "searchQuery": role.strip(),
    }
    return "https://hiring.cafe/?searchState=" + urllib.parse.quote(json.dumps(ss))


def _parse_publish_date(iso_str):
    if not iso_str:
        return datetime.today().strftime("%Y-%m-%d")
    try:
        return iso_str[:10]
    except Exception:
        return datetime.today().strftime("%Y-%m-%d")


def _company_name(hit):
    """Prefer enriched_company_data.name (real legal name) over the raw
    v5_processed_job_data.company_name (often the ATS slug)."""
    enriched = (hit.get("enriched_company_data") or {}).get("name")
    if enriched and enriched.lower() != "none":
        return enriched
    raw = (hit.get("v5_processed_job_data") or {}).get("company_name")
    return raw or "N/A"


def _experience_str(min_yoe):
    if not min_yoe or str(min_yoe).lower() in ("none", "not mentioned", ""):
        return ""
    try:
        n = int(min_yoe)
        return f"{n}+ yrs"
    except Exception:
        return str(min_yoe)


def _hits_from_next_data(html):
    """Try to pull ssrHits from the inline Next.js SSR payload. Returns the
    hits list or None if the page no longer ships data this way."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None
    hits = (data.get("props", {})
                .get("pageProps", {})
                .get("ssrHits"))
    return hits if isinstance(hits, list) and hits else None


def _looks_like_job_hit(obj):
    if not isinstance(obj, dict):
        return False
    return any(k in obj for k in ("v5_processed_job_data", "job_information", "apply_url"))


def _find_hits_array(payload, depth=0):
    """Walk an arbitrary JSON payload looking for an array of job hits.
    Used to recover the jobs list from a client-side API response when the
    initial HTML no longer carries __NEXT_DATA__."""
    if depth > 8 or payload is None:
        return None
    if isinstance(payload, list):
        if payload and _looks_like_job_hit(payload[0]):
            return payload
        for item in payload:
            found = _find_hits_array(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(payload, dict):
        for preferred in ("ssrHits", "hits", "results", "jobs", "data"):
            if preferred in payload:
                found = _find_hits_array(payload[preferred], depth + 1)
                if found:
                    return found
        for v in payload.values():
            if isinstance(v, (list, dict)):
                found = _find_hits_array(v, depth + 1)
                if found:
                    return found
    return None


def _compensation_str(v5):
    if not v5:
        return ""
    if str(v5.get("is_compensation_transparent", "")).lower() != "true":
        return ""
    cur = v5.get("listed_compensation_currency") or ""
    freq = (v5.get("listed_compensation_frequency") or "").lower()
    mn = v5.get("yearly_min_compensation") or v5.get("monthly_min_compensation") or v5.get("hourly_min_compensation")
    mx = v5.get("yearly_max_compensation") or v5.get("monthly_max_compensation") or v5.get("hourly_max_compensation")
    if (not mn or str(mn).lower() == "none") and (not mx or str(mx).lower() == "none"):
        return ""
    parts = []
    if mn and str(mn).lower() != "none":
        parts.append(str(mn))
    if mx and str(mx).lower() != "none":
        parts.append(str(mx))
    return f"{cur} {' - '.join(parts)} {freq}".strip()


def scrape_hcafe(role, city, posting_days, limit):
    """
    role:         free-text search query (e.g. "devops")
    city:         display city name (key of HCAFE_CITIES)
    posting_days: int (1, 2, 3, 7, 14, 30) — dateFetchedPastNDays
    limit:        max results (capped at 40, since one page returns ~100 SSR hits)
    """
    if city not in HCAFE_CITIES:
        raise ValueError(f"Unknown city: {city}")

    # HiringCafe only honors dateFetchedPastNDays at specific values (2, 4,
    # 14). Other values are silently ignored and HC returns its unfiltered
    # set (~3000 mostly-stale jobs). Map the user's window to the smallest
    # honored value that covers it. Post-filter by publish_date narrows it
    # back to the user's actual window.
    if int(posting_days) <= 2:
        fetch_window = 2
    elif int(posting_days) <= 4:
        fetch_window = 4
    else:
        fetch_window = 14
    url = _build_url(role, city, fetch_window)
    # Date-only cutoff so a job posted exactly N days ago at midnight isn't
    # accidentally excluded by today's current time-of-day.
    cutoff_date = (datetime.today() - timedelta(days=int(posting_days))).date()

    # HiringCafe sits behind Cloudflare Turnstile. Headless modes (even
    # Chrome's --headless=new) are flagged. The only reliable approach is
    # a fully visible browser + persistent profile:
    #   - First run: the browser opens visibly; if Turnstile shows the
    #     "Verify you are human" checkbox, the user clicks it once. Cloudflare
    #     issues a cf_clearance cookie that gets saved in _hcafe_profile/.
    #   - Subsequent runs within the cookie's TTL (hours to days) sail through
    #     without prompting; the browser still opens visibly but closes
    #     within seconds once jobs load.
    with sync_playwright() as p:
        # Patchright runs its own patched Chrome build (installed via
        # `patchright install chrome`). We deliberately do NOT pass
        # channel="chrome" — Patchright's bundled binary has the automation
        # flags and infobar stripped at the binary level, which is what gets
        # us through Turnstile. System Chrome would lose those patches.
        context = p.chromium.launch_persistent_context(
            _PROFILE_DIR,
            headless=False,            # visible window; Turnstile demands it
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                # AsyncDns + DnsOverHttps make Chromium do its own DNS via
                # DoH endpoints, which fails on some ISPs (Reliance Jio in
                # particular). Disabling both forces Chromium to fall back to
                # the OS resolver — which we've already confirmed works.
                "--disable-features=IsolateOrigins,site-per-process,AsyncDns,EnableDnsOverHttps,DnsOverHttps",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
            "Object.defineProperty(navigator,'languages',{get:()=>['en-IN','en']});"
            "window.chrome = {runtime: {}};"
        )
        page = context.pages[0] if context.pages else context.new_page()
        print("[HiringCafe] Browser is visible — Cloudflare Turnstile may show "
              "a 'Verify you are human' checkbox. Click it once if it appears; "
              "subsequent runs will pass through automatically (cookie cached "
              "in _hcafe_profile/).")

        # Intercept every response with a body and try to parse it as JSON.
        # We deliberately don't filter by URL or content-type — HiringCafe may
        # fetch jobs from a domain like `*-dsn.algolia.net` or send JSON with
        # a non-standard content-type. The cheap parse-and-walk catches both.
        # We DO blocklist obviously non-data resources to keep memory bounded.
        captured_payloads = []
        skipped_urls = []
        response_count = {"total": 0, "json_ok": 0, "skipped_ext": 0}
        _IGNORE_EXT = (
            ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg",
            ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".mp4", ".webm", ".map",
        )

        def _on_response(response):
            try:
                response_count["total"] += 1
                if len(captured_payloads) > 60:
                    return
                u = response.url.split("?", 1)[0].lower()
                if u.endswith(_IGNORE_EXT):
                    response_count["skipped_ext"] += 1
                    return
                # Skip well-known analytics/tracking that's never job data.
                if any(host in u for host in (
                    "google-analytics", "googletagmanager", "doubleclick",
                    "facebook.com/tr", "sentry.io", "datadoghq",
                    "intercom.io", "segment.io",
                )):
                    return
                try:
                    body = response.json()
                except Exception:
                    # Some servers send JSON with the wrong content-type.
                    try:
                        text = response.text()
                        if not text or text[0] not in "[{":
                            return
                        body = json.loads(text)
                    except Exception:
                        return
                response_count["json_ok"] += 1
                if body is not None:
                    captured_payloads.append((response.url, body))
                else:
                    skipped_urls.append(response.url)
            except Exception:
                pass

        page.on("response", _on_response)

        print(f"[HiringCafe] Fetching: {url[:140]}...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PWTimeout:
            print("[HiringCafe] navigation timeout")
            context.close()
            return []

        # Wait up to 90s — most of that budget exists for a possible Turnstile
        # solve on the first run. Subsequent runs with cf_clearance cookie
        # already cached usually clear within 2-4 seconds.
        # We exit early as soon as __NEXT_DATA__ becomes populated OR job
        # cards render in the DOM.
        print("[HiringCafe] Waiting up to 90s for page to render "
              "(click the 'Verify you are human' checkbox if it appears)...")
        try:
            page.wait_for_function(
                """() => {
                    const n = document.getElementById('__NEXT_DATA__');
                    if (n && n.textContent && n.textContent.length > 2000) return true;
                    const cards = document.querySelectorAll(
                        'article, [class*="JobCard"], [class*="job-card"], '
                        + '[class*="JobResult"], [data-testid*="job"], '
                        + '[class*="ResultCard"]'
                    );
                    return cards.length > 0;
                }""",
                timeout=90000,
            )
            print("[HiringCafe] Page rendered — proceeding.")
        except Exception:
            print("[HiringCafe] Wait timed out — proceeding anyway with whatever loaded.")
        # Extra settle time so any final hydration completes.
        time.sleep(3.0)

        html = page.content()
        # Quick DOM-side card harvest as a final fallback, before we close
        # the browser. Cheap if there are no cards.
        dom_hits = page.evaluate(
            """() => {
                const cards = document.querySelectorAll(
                    'article, [class*="JobCard"], [class*="job-card"], '
                    + '[class*="JobResult"], [class*="ResultCard"], '
                    + '[data-testid*="job"]'
                );
                const out = [];
                cards.forEach(card => {
                    const a = card.querySelector('a[href]');
                    const title = card.querySelector('h1, h2, h3, h4, [class*="title"]');
                    const company = card.querySelector('[class*="ompany"], [class*="employer"]');
                    out.push({
                        title:   title   ? title.innerText.trim()   : '',
                        company: company ? company.innerText.trim() : '',
                        link:    a       ? a.href                   : '',
                        text:    card.innerText.trim().slice(0, 600),
                    });
                });
                return out;
            }"""
        )

        # If nothing came through at all, dump the page state to disk so we
        # can see whether HC is serving a real page, a blank SPA shell, or a
        # bot-detection challenge. Files land at the project root next to the
        # jobs_*.xlsx exports.
        nothing_came_through = (
            not _hits_from_next_data(html)
            and not captured_payloads
            and not dom_hits
        )
        if nothing_came_through:
            project_root = os.path.dirname(os.path.dirname(__file__))
            try:
                page.screenshot(
                    path=os.path.join(project_root, "_hcafe_debug.png"),
                    full_page=True,
                )
            except Exception as e:
                print(f"[HiringCafe] screenshot failed: {e}")
            try:
                with open(os.path.join(project_root, "_hcafe_debug.html"),
                          "w", encoding="utf-8") as f:
                    f.write(html)
            except Exception as e:
                print(f"[HiringCafe] html dump failed: {e}")
            print("[HiringCafe] Wrote _hcafe_debug.png and _hcafe_debug.html "
                  "to the project root — open them to see what HC actually served.")
        context.close()

    print(f"[HiringCafe] Responses seen: total={response_count['total']}  "
          f"json_ok={response_count['json_ok']}  "
          f"skipped_static={response_count['skipped_ext']}  "
          f"captured={len(captured_payloads)}")

    # 1. Original SSR path — fastest when it works.
    hits = _hits_from_next_data(html)
    source = "__NEXT_DATA__"

    # 2. Walk captured XHR payloads for a hits-shaped array.
    if not hits:
        for resp_url, body in captured_payloads:
            found = _find_hits_array(body)
            if found:
                hits = found
                source = f"XHR ({resp_url[:80]})"
                break

    # 3. DOM card harvest — last resort. Builds synthetic hit dicts from the
    # rendered page so the rest of the pipeline keeps working unchanged. The
    # output keys mirror what _company_name / _experience_str etc. expect.
    if not hits and dom_hits:
        synthetic = []
        for d in dom_hits:
            if not d.get("title") or not d.get("link"):
                continue
            synthetic.append({
                "apply_url": d["link"],
                "v5_processed_job_data": {
                    "core_job_title": d["title"],
                    "company_name":   d.get("company") or "",
                    "formatted_workplace_location": "",
                    "estimated_publish_date": "",
                    "min_industry_and_role_yoe": "",
                    "workplace_type": "",
                    "seniority_level": "",
                    "technical_tools": [],
                    "is_compensation_transparent": "false",
                },
                "job_information": {"title": d["title"]},
                "enriched_company_data": {"name": d.get("company") or ""},
                "source": "",
            })
        if synthetic:
            hits = synthetic
            source = f"DOM-harvest ({len(synthetic)} cards)"

    if not hits:
        sample = ", ".join(u.split("?", 1)[0] for u, _ in captured_payloads[:5])
        print(f"[HiringCafe] No hits found — __NEXT_DATA__ missing, "
              f"{len(captured_payloads)} captured JSON responses had no job array, "
              f"and DOM harvest returned 0 cards. "
              f"Sample captured URLs: {sample or '(none)'}")
        return []

    print(f"[HiringCafe] {len(hits)} hits parsed (via {source}); "
          f"will keep jobs posted on/after {cutoff_date.strftime('%Y-%m-%d')}")

    jobs = []
    seen_urls = set()
    filtered_old = 0
    for hit in hits:
        if len(jobs) >= limit:
            break
        try:
            v5 = hit.get("v5_processed_job_data") or {}
            info = hit.get("job_information") or {}
            title = info.get("title") or v5.get("core_job_title") or ""
            if not title:
                continue
            apply_url = hit.get("apply_url") or ""
            if not apply_url or apply_url in seen_urls:
                continue
            seen_urls.add(apply_url)

            # Skip jobs whose publish date is older than the user's window
            publish_iso = v5.get("estimated_publish_date")
            if publish_iso:
                try:
                    publish_date = datetime.strptime(publish_iso[:10], "%Y-%m-%d").date()
                    if publish_date < cutoff_date:
                        filtered_old += 1
                        continue
                except Exception:
                    pass

            jobs.append({
                "Job Title": title,
                "Company":   _company_name(hit),
                "Location":  v5.get("formatted_workplace_location") or "",
                "Posted":    _parse_publish_date(v5.get("estimated_publish_date")),
                "Link":      apply_url,
                "Experience": _experience_str(v5.get("min_industry_and_role_yoe")),
                "Workplace": v5.get("workplace_type") or "",
                "Seniority": v5.get("seniority_level") or "",
                "Skills":    ", ".join(v5.get("technical_tools") or []),
                "Salary":    _compensation_str(v5),
                # HiringCafe sends every job to an external ATS (Workday,
                # Greenhouse, etc.) — no Easy Apply concept.
                "Easy Apply": False,
                "Apply Type": "External",
                "Source ATS": hit.get("source") or "",
            })
        except Exception as e:
            print(f"[HiringCafe] Hit error: {e}")
            continue

    if filtered_old:
        print(f"[HiringCafe] Filtered out {filtered_old} hits older than the window")

    # Sort newest first
    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    jobs.sort(key=sort_key, reverse=True)
    return jobs[:limit]
