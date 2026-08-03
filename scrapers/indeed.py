"""Indeed public job search scraper (no login required).

URL pattern reference (provided by user):
    https://in.indeed.com/jobs?q=DevOps+Engineer&l=Bengaluru%2C+Karnataka&fromage=1&radius=100&sort=date

Params:
    q       = role (spaces as +)
    l       = "City, State" (URL-encoded)
    fromage = posted within N days (1, 3, 7, 14)
    radius  = miles (hardcoded 100)
    sort    = date
    start   = pagination offset (multiples of 10)
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import random
import re
from urllib.parse import quote_plus
from datetime import datetime, timedelta


# Indeed accepts free-text locations. We map our standard city chips to
# "City, State" so results match the user's reference URL.
INDEED_CITIES = {
    "Bengaluru":  "Bengaluru, Karnataka",
    "Hyderabad":  "Hyderabad, Telangana",
    "Mumbai":     "Mumbai, Maharashtra",
    "Pune":       "Pune, Maharashtra",
    "Chennai":    "Chennai, Tamil Nadu",
    "Delhi":      "Delhi, Delhi",
    "Noida":      "Noida, Uttar Pradesh",
    "Gurugram":   "Gurugram, Haryana",
}


def _parse_indeed_date(text):
    if not text:
        return datetime.today().strftime("%Y-%m-%d")
    text = text.lower().strip()
    today = datetime.today()
    if "just posted" in text or "today" in text or "active today" in text:
        return today.strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*day", text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*hour", text)
    if m:
        return today.strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\+?\s*day", text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def _build_url(role, location, fromage_days, start=0):
    return (
        "https://in.indeed.com/jobs?"
        f"q={quote_plus(role)}"
        f"&l={quote_plus(location)}"
        f"&fromage={int(fromage_days)}"
        "&radius=100"
        "&sort=date"
        f"&start={int(start)}"
    )


def _launch(p, headless):
    browser = p.chromium.launch(
        headless=headless,
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
    return browser, context


def _is_blocked(page):
    """Detect bot-check page (Cloudflare, hCaptcha, Indeed's own block)."""
    title = (page.title() or "").lower()
    if "just a moment" in title or "cloudflare" in title:
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return False
    return (
        "verifying you are human" in body
        or "additional verification required" in body
        or "you've been blocked" in body
    )


def _scrape_with(p, role, fromage_days, internal_limit, locations,
                 apply_mode, headless):
    """One scraping pass with the given headless mode. Returns (jobs, blocked_flag)."""
    all_jobs = []
    seen_links = set()
    blocked_any = False

    browser, context = _launch(p, headless=headless)
    page = context.new_page()

    try:
        for city_key in locations:
            if city_key not in INDEED_CITIES:
                print(f"[Indeed] Skipping unknown city: {city_key}")
                continue
            if len(all_jobs) >= internal_limit:
                break

            location_query = INDEED_CITIES[city_key]
            start = 0

            while len(all_jobs) < internal_limit:
                url = _build_url(role, location_query, fromage_days, start)
                print(f"[Indeed] Fetching: {url}")

                # Bot-check retry: 1st attempt waits 20s for CF to clear,
                # 2nd attempt waits 12s. Past page 1 Indeed often hard-blocks
                # paginated requests — short retries avoid wasting minutes.
                attempts_budget = 2 if start == 0 else 1
                wait_budget = (20, 12)
                cleared = False
                for attempt in range(attempts_budget):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e:
                        print(f"[Indeed] goto attempt {attempt+1} failed: {e}")
                        time.sleep(3)
                        continue

                    deadline = time.time() + wait_budget[attempt]
                    while time.time() < deadline:
                        if not _is_blocked(page):
                            cleared = True
                            break
                        time.sleep(1)

                    if cleared:
                        break
                    print(f"[Indeed] Bot-check not cleared (attempt {attempt+1}/{attempts_budget})")
                    if attempt + 1 < attempts_budget:
                        time.sleep(random.uniform(3.0, 5.0))

                if not cleared:
                    blocked_any = True
                    break

                try:
                    page.wait_for_selector("div.job_seen_beacon, div[data-jk], li[data-empn]", timeout=20000)
                except Exception:
                    pass
                time.sleep(random.uniform(2.0, 3.0))

                page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                time.sleep(1.0)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.5)

                cards = page.query_selector_all("div.job_seen_beacon")
                if not cards:
                    cards = page.query_selector_all("div[data-jk]")
                if not cards:
                    cards = page.query_selector_all("li[data-empn]")

                if not cards:
                    print(f"[Indeed] No cards for {city_key} start={start}")
                    break

                found_this_page = 0
                for card in cards:
                    if len(all_jobs) >= internal_limit:
                        break
                    try:
                        # ── Title + link ──
                        title_link = (
                            card.query_selector("h2.jobTitle a")
                            or card.query_selector("a[data-jk]")
                            or card.query_selector("a.jcs-JobTitle")
                        )
                        if not title_link:
                            continue

                        title_span = title_link.query_selector("span[title]") or title_link.query_selector("span")
                        title = (title_span.inner_text().strip() if title_span
                                 else title_link.inner_text().strip())
                        if not title:
                            continue

                        href = (title_link.get_attribute("href") or "").strip()
                        if not href:
                            continue
                        if not href.startswith("http"):
                            href = "https://in.indeed.com" + href
                        link = href.split("&")[0]
                        # Build canonical viewjob URL from data-jk
                        jk = title_link.get_attribute("data-jk") or card.get_attribute("data-jk")
                        if jk:
                            link = f"https://in.indeed.com/viewjob?jk={jk}"

                        if link in seen_links:
                            continue

                        # ── Company ──
                        comp_el = (
                            card.query_selector("[data-testid='company-name']")
                            or card.query_selector("span.companyName")
                            or card.query_selector("span[data-testid='company-name']")
                        )
                        company = comp_el.inner_text().strip() if comp_el else "N/A"

                        # ── Location ──
                        loc_el = (
                            card.query_selector("[data-testid='text-location']")
                            or card.query_selector("div.companyLocation")
                        )
                        loc_text = loc_el.inner_text().strip() if loc_el else location_query

                        # ── Date ──
                        date_el = (
                            card.query_selector("[data-testid='myJobsStateDate']")
                            or card.query_selector("span.date")
                            or card.query_selector("[class*='date']")
                        )
                        posted = _parse_indeed_date(date_el.inner_text() if date_el else "")

                        # ── Easy Apply (card-level signal) ──
                        card_text = (card.inner_text() or "").lower()
                        easy_apply = ("easily apply" in card_text) or ("easy apply" in card_text)
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
                        print(f"[Indeed] Card error: {e}")
                        continue

                print(f"[Indeed] Got {found_this_page} jobs from {city_key} start={start}")
                if found_this_page == 0:
                    break
                start += 10
                time.sleep(random.uniform(2.0, 3.5))
    finally:
        try:
            browser.close()
        except Exception:
            pass

    return all_jobs, blocked_any


def scrape_indeed(role, fromage_days, limit, locations, apply_mode="include_easy"):
    """Scrape Indeed. Tries headless first; falls back to headed if blocked."""
    mode_multipliers = {"include_easy": 1.0, "only_easy": 2.5, "only_external": 2.5}
    internal_limit = max(limit, int(limit * mode_multipliers.get(apply_mode, 1.0)))

    with sync_playwright() as p:
        jobs, blocked = _scrape_with(
            p, role, fromage_days, internal_limit, locations, apply_mode,
            headless=True,
        )
        if not jobs and blocked:
            print("[Indeed] Headless blocked — retrying headed.")
            jobs, _ = _scrape_with(
                p, role, fromage_days, internal_limit, locations, apply_mode,
                headless=False,
            )

    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    jobs.sort(key=sort_key, reverse=True)
    return jobs[:limit]
