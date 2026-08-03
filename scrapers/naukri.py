"""Naukri.com public job search scraper (no login required).

Naukri uses Akamai Bot Manager and aggressively blocks anything that looks
like a headless browser. We get around that with two tricks:

1. **launch_persistent_context** — saves cookies + fingerprint between runs
   so Akamai treats us like a returning user (gives us a trust cookie on
   first warmup visit).
2. **`--headless=new` flag** + `headless=False` in Playwright — uses
   Chrome's "new" headless mode (no visible window) but Playwright doesn't
   add the `HeadlessChrome` token to the user-agent, so Akamai doesn't
   flag us. The window is never visible.

URL format:
    https://www.naukri.com/{role-slug}-jobs-in-{city-slug}[-{page}]
        ?k={role}&l={city}&jobAge={days}[&experience={years}]

Filters:
    jobAge      — days since posted: 1, 3, 7, 15, 30 (Naukri honors any int)
    experience  — exact years of experience (0..30)

We intentionally don't append `&sort=date` because that surfaces low-relevance
"just posted" agency listings. The default relevance sort combined with a
jobAge window gives the best balance of "fresh AND relevant".
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import random
import re
import os
from datetime import datetime, timedelta


# Display city -> Naukri slug (lowercase, in URL path AND l= param)
NAUKRI_CITIES = {
    "Bengaluru":  "bengaluru",
    "Hyderabad":  "hyderabad",
    "Mumbai":     "mumbai",
    "Pune":       "pune",
    "Chennai":    "chennai",
    "Delhi":      "delhi-ncr",
    "Noida":      "noida",
    "Gurugram":   "gurgaon",
}

# Profile directory — persisted between scraper invocations so Akamai keeps
# trusting us. Created on first run.
_PROFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "_naukri_profile")


def _slug(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _parse_naukri_date(text):
    if not text:
        return datetime.today().strftime("%Y-%m-%d")
    text = text.lower().strip()
    today = datetime.today()
    if "today" in text or "just" in text or "few hours" in text:
        return today.strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*day", text)
    if m:
        return (today - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*week", text)
    if m:
        return (today - timedelta(weeks=int(m.group(1)))).strftime("%Y-%m-%d")
    m = re.search(r"(\d+)\s*month", text)
    if m:
        return (today - timedelta(days=int(m.group(1)) * 30)).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def _build_url(role, city_slug, city_query, job_age, experience, page=1):
    role_slug = _slug(role)
    path = f"{role_slug}-jobs-in-{city_slug}"
    if page > 1:
        path += f"-{page}"
    qs = [
        f"k={role.replace(' ', '%20')}",
        f"l={city_query}",
        f"jobAge={int(job_age)}",
    ]
    if experience is not None and experience != "":
        qs.append(f"experience={int(experience)}")
    return f"https://www.naukri.com/{path}?" + "&".join(qs)


def scrape_naukri(role, city, job_age_days, limit, experience=None):
    """
    role:          free-text role (e.g. "devops engineer")
    city:          display city name (key of NAUKRI_CITIES)
    job_age_days:  int (1, 3, 7, 15, 30)
    limit:         max results
    experience:    optional int years (0..30), or None for any
    """
    if city not in NAUKRI_CITIES:
        raise ValueError(f"Unknown city: {city}")
    if not role.strip():
        raise ValueError("role is required")

    city_slug = NAUKRI_CITIES[city]
    # `l=` param expects the same lowercase slug
    city_query = city_slug

    os.makedirs(_PROFILE_DIR, exist_ok=True)

    all_jobs = []
    seen_links = set()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            _PROFILE_DIR,
            headless=False,                  # Playwright doesn't add HeadlessChrome
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/132.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=AutomationControlled",
                "--headless=new",            # Chrome's new headless (no UI, undetected)
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        ctx.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
            Object.defineProperty(navigator,'languages',{get:()=>['en-IN','en']});
            window.chrome = {runtime:{}, loadTimes:function(){}, csi:function(){}, app:{}};
        """)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Warmup on homepage to establish Akamai trust cookies
        try:
            page.goto("https://www.naukri.com/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(random.uniform(3.0, 4.5))
        except Exception:
            pass

        page_num = 1
        while len(all_jobs) < limit:
            url = _build_url(role, city_slug, city_query, job_age_days, experience, page_num)
            print(f"[Naukri] Fetching: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(3.5, 5.0))

                # Quick block detection
                title = page.title() or ""
                if "Access Denied" in title or "Just a moment" in title:
                    print(f"[Naukri] BLOCKED on page {page_num} (title={title!r}). Stopping.")
                    break

                # Cards lazy-load on scroll
                for _ in range(2):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1.2)

                cards = page.query_selector_all("div.srp-jobtuple-wrapper")
                if not cards:
                    cards = page.query_selector_all("div.cust-job-tuple")

                if not cards:
                    print(f"[Naukri] No cards on page {page_num}")
                    break

                found_this_page = 0
                for card in cards:
                    if len(all_jobs) >= limit:
                        break
                    try:
                        title_el = card.query_selector("a.title")
                        title_txt = title_el.inner_text().strip() if title_el else ""
                        if not title_txt:
                            continue
                        link = (title_el.get_attribute("href") or "").strip() if title_el else ""
                        if not link:
                            continue
                        if not link.startswith("http"):
                            link = "https://www.naukri.com" + link
                        if link in seen_links:
                            continue

                        comp_el = card.query_selector("a.comp-name")
                        company = comp_el.inner_text().strip() if comp_el else "N/A"

                        rating_el = card.query_selector("a.rating span.main-2")
                        rating = rating_el.inner_text().strip() if rating_el else ""

                        exp_el = card.query_selector("span.expwdth") or card.query_selector("span.exp")
                        exp_text = exp_el.inner_text().strip() if exp_el else ""

                        sal_el = (
                            card.query_selector("span.sal-wrap span")
                            or card.query_selector("span.sal")
                            or card.query_selector("[class*='sal-wrap']")
                        )
                        salary = sal_el.inner_text().strip() if sal_el else ""

                        loc_el = card.query_selector("span.locWdth") or card.query_selector("span.loc")
                        loc_text = loc_el.inner_text().strip() if loc_el else city

                        desc_el = card.query_selector("span.job-desc")
                        desc = desc_el.inner_text().strip() if desc_el else ""

                        skills = [li.inner_text().strip() for li in card.query_selector_all("ul.tags-gt li")]

                        age_el = card.query_selector("span.job-post-day")
                        posted_raw = age_el.inner_text().strip() if age_el else ""
                        posted = _parse_naukri_date(posted_raw)

                        seen_links.add(link)
                        all_jobs.append({
                            "Job Title": title_txt,
                            "Company": company,
                            "Location": loc_text,
                            "Posted": posted,
                            "Link": link,
                            "Experience": exp_text,
                            "Salary": salary,
                            "Rating": rating,
                            "Skills": ", ".join(skills),
                            "Description": desc,
                            "Easy Apply": False,
                            "Apply Type": "Naukri Apply",
                        })
                        found_this_page += 1
                    except Exception as e:
                        print(f"[Naukri] Card error: {e}")
                        continue

                print(f"[Naukri] Got {found_this_page} jobs from page {page_num}")
                if found_this_page == 0:
                    break
                page_num += 1
                time.sleep(random.uniform(2.0, 3.5))

            except PWTimeout:
                print(f"[Naukri] Timeout on page {page_num}")
                break
            except Exception as e:
                print(f"[Naukri] Error: {e}")
                break

        ctx.close()

    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    all_jobs.sort(key=sort_key, reverse=True)
    return all_jobs[:limit]
