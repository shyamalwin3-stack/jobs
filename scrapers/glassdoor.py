"""Glassdoor public job search scraper (no login required).

URL pattern reference (provided by user):
    https://www.glassdoor.co.in/Job/bengaluru-india-devops-engineer-jobs-SRCH_IL.0,15_IC2940587_KO16,31.htm?fromAge=1

Breakdown:
    {city-slug}-{role-slug}-jobs-SRCH_IL.0,{loc_len}_IC{loc_id}_KO{role_start},{role_end}.htm?fromAge={days}

    IL.0,15  = location span in the slug (start=0, end=loc_len)
    IC2940587 = Glassdoor's internal city id
    KO16,31  = role keyword span (start=loc_len+1, end=loc_len+1+role_len)
    fromAge  = days (1, 3, 7, 14, 30)
    Pagination uses _IP{n} suffix before .htm
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import random
import re
from datetime import datetime, timedelta


# Hardcoded Glassdoor city -> (slug, IC code) for common Indian metros.
# Bengaluru code verified from user-provided URL. Others sourced from public
# Glassdoor URLs; if any prove wrong they can be updated here.
GLASSDOOR_CITIES = {
    "Bengaluru":  ("bengaluru-india",   "2940587"),
    "Hyderabad":  ("hyderabad-india",   "2954929"),
    "Mumbai":     ("mumbai-india",      "2967211"),
    "Pune":       ("pune-india",        "2964304"),
    "Chennai":    ("chennai-india",     "2942439"),
    "Delhi":      ("new-delhi-india",   "2945657"),
    "Noida":      ("noida-india",       "2948308"),
    "Gurugram":   ("gurgaon-india",     "2942260"),
}


def _slug(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _parse_glassdoor_date(text):
    """Glassdoor card date text e.g. '1d', '24h', '30d+', 'Today', '7d'."""
    if not text:
        return datetime.today().strftime("%Y-%m-%d")
    text = text.lower().strip()
    today = datetime.today()
    if "today" in text or "just" in text or text in ("0d", "0h"):
        return today.strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*h", text)
    if m:
        return today.strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*d", text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*w", text)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*mo", text)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def _build_url(role, location_slug, location_id, from_age, page=1):
    role_slug = _slug(role)
    loc_len = len(location_slug)
    role_start = loc_len + 1
    role_end = role_start + len(role_slug)

    full_slug = f"{location_slug}-{role_slug}-jobs"
    path = f"{full_slug}-SRCH_IL.0,{loc_len}_IC{location_id}_KO{role_start},{role_end}"
    if page and page > 1:
        path += f"_IP{page}"
    return f"https://www.glassdoor.co.in/Job/{path}.htm?fromAge={int(from_age)}"


def scrape_glassdoor(role, from_age_days, limit, locations, apply_mode="include_easy"):
    """
    role: free-text role string
    from_age_days: int (1, 3, 7, 14, 30)
    limit: number of jobs to return
    locations: list of city names matching GLASSDOOR_CITIES keys
    apply_mode: include_easy | only_easy | only_external
    """
    all_jobs = []
    seen_links = set()

    mode_multipliers = {"include_easy": 1.0, "only_easy": 2.5, "only_external": 2.5}
    internal_limit = max(limit, int(limit * mode_multipliers.get(apply_mode, 1.0)))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
            ],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en']});
            window.chrome = {runtime: {}};
        """)

        page = context.new_page()

        for city in locations:
            if city not in GLASSDOOR_CITIES:
                print(f"[Glassdoor] Skipping unknown city: {city}")
                continue
            if len(all_jobs) >= internal_limit:
                break

            slug, loc_id = GLASSDOOR_CITIES[city]
            page_num = 1

            while len(all_jobs) < internal_limit:
                url = _build_url(role, slug, loc_id, from_age_days, page_num)
                print(f"[Glassdoor] Fetching: {url}")

                # Glassdoor often serves a Cloudflare "Just a moment..."
                # challenge page that needs ~5-20s of JS to auto-clear. We retry
                # up to 3 times, each retry: navigate, then poll for the title
                # to change to a real Glassdoor page, then wait for listings.
                cleared = False
                for attempt in range(3):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        print(f"[Glassdoor] goto attempt {attempt+1} failed: {e}")
                        time.sleep(3)
                        continue

                    # Poll up to 25s for CF challenge to clear
                    deadline = time.time() + 25
                    while time.time() < deadline:
                        t = (page.title() or "").lower()
                        if "just a moment" not in t and "cloudflare" not in t and t:
                            cleared = True
                            break
                        time.sleep(1)

                    if cleared:
                        break
                    print(f"[Glassdoor] CF challenge not cleared (attempt {attempt+1}/3)")
                    time.sleep(random.uniform(4.0, 7.0))

                try:
                    try:
                        page.wait_for_selector("li[data-test='jobListing']", timeout=20000)
                    except Exception:
                        pass
                    time.sleep(random.uniform(2.0, 3.0))

                    # Glassdoor lazy-loads cards as you scroll. A single scroll
                    # often leaves 1-2 cards unrendered. Loop scrolling until the
                    # card count stabilizes for two consecutive checks (or we
                    # hit a safety cap).
                    last_count = -1
                    stable_passes = 0
                    for _ in range(8):
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(1.2)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                        time.sleep(0.4)
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        time.sleep(0.8)
                        cur = len(page.query_selector_all("li[data-test='jobListing']"))
                        if cur and cur == last_count:
                            stable_passes += 1
                            if stable_passes >= 1:
                                break
                        else:
                            stable_passes = 0
                        last_count = cur

                    # Job cards — Glassdoor has shipped several layouts. Try fallbacks.
                    cards = page.query_selector_all("li[data-test='jobListing']")
                    if not cards:
                        cards = page.query_selector_all("li[class*='JobsList_jobListItem']")
                    if not cards:
                        cards = page.query_selector_all("ul[aria-label*='Jobs'] > li")
                    if not cards:
                        cards = page.query_selector_all("li.react-job-listing")

                    if not cards:
                        # Page either has no results or Cloudflare blocked us
                        # (title="Just a moment..."). Either way stop paginating.
                        print(f"[Glassdoor] No cards for {city} page={page_num}")
                        break

                    found_this_page = 0

                    for card in cards:
                        if len(all_jobs) >= internal_limit:
                            break
                        try:
                            title_el = (
                                card.query_selector("a[data-test='job-title']")
                                or card.query_selector("a.JobCard_jobTitle__GLyJ1")
                                or card.query_selector("a[class*='jobTitle']")
                                or card.query_selector("a[class*='JobTitle']")
                            )
                            title = title_el.inner_text().strip() if title_el else None
                            if not title:
                                continue

                            # Glassdoor's title anchor href points to a category
                            # listing (e.g. /job-listing/...-JV_IC..._KO..._KE.htm)
                            # and only the `?jl={JOBID}` query param resolves it
                            # to the actual job page. The job ID is also on the
                            # parent <li> as `data-jobid`. Preserve `jl` (or
                            # append it from data-jobid) so the link opens the
                            # real job listing instead of a category page.
                            href = (title_el.get_attribute("href") or "").strip() if title_el else ""
                            if href and not href.startswith("http"):
                                link_full = "https://www.glassdoor.co.in" + href
                            else:
                                link_full = href
                            base, _, query = link_full.partition("?")
                            jl = None
                            for piece in query.split("&"):
                                if piece.startswith("jl="):
                                    jl = piece[3:]
                                    break
                            if not jl:
                                jl = (card.get_attribute("data-jobid") or "").strip() or None
                            link = f"{base}?jl={jl}" if (base and jl) else link_full
                            if not link:
                                continue
                            if link in seen_links:
                                continue

                            comp_el = (
                                card.query_selector("span.EmployerProfile_compactEmployerName__9MGcV")
                                or card.query_selector("span[class*='compactEmployerName']")
                                or card.query_selector("[data-test='employer-name']")
                                or card.query_selector("div[class*='EmployerProfile_employerName']")
                                or card.query_selector("div[class*='EmployerProfile']")
                            )
                            company = comp_el.inner_text().strip() if comp_el else "N/A"
                            company = company.split("\n")[0].strip()

                            loc_el = (
                                card.query_selector("[data-test='emp-location']")
                                or card.query_selector("div.JobCard_location__Ds1fM")
                                or card.query_selector("div[class*='location']")
                            )
                            loc_text = loc_el.inner_text().strip() if loc_el else city

                            age_el = (
                                card.query_selector("[data-test='job-age']")
                                or card.query_selector("div[class*='listing-age']")
                                or card.query_selector("div.JobCard_listingAge__jJsuc")
                            )
                            posted = _parse_glassdoor_date(age_el.inner_text() if age_el else "")

                            # Easy Apply detection — Glassdoor surfaces this on the
                            # card itself (the detail page doesn't), so card text is
                            # the reliable signal. Skipping the detail page also makes
                            # the scraper ~7x faster.
                            card_text = (card.inner_text() or "").lower()
                            easy_apply = "easy apply" in card_text
                            apply_type = "Easy Apply" if easy_apply else "External"

                            if apply_mode == "only_external" and easy_apply:
                                continue
                            if apply_mode == "only_easy" and not easy_apply:
                                continue

                            seen_links.add(link)
                            all_jobs.append({
                                "Job Title": title,
                                "Company": company,
                                "Location": loc_text,
                                "Posted": posted,
                                "Link": link,
                                "Easy Apply": easy_apply,
                                "Apply Type": apply_type,
                            })
                            found_this_page += 1

                        except Exception as e:
                            print(f"[Glassdoor] Card error: {e}")
                            continue

                    print(f"[Glassdoor] Got {found_this_page} jobs from {city} page={page_num}")
                    if found_this_page == 0:
                        break
                    page_num += 1
                    time.sleep(random.uniform(2.0, 3.5))

                except PWTimeout:
                    print(f"[Glassdoor] Timeout: {city}")
                    break
                except Exception as e:
                    print(f"[Glassdoor] Error: {e}")
                    break

        browser.close()

    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    all_jobs.sort(key=sort_key, reverse=True)
    return all_jobs[:limit]
