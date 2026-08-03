"""Foundit (Monster India) public job search scraper (no login required).

Foundit is protected by Akamai Bot Manager just like Naukri — we get past
it with the same `launch_persistent_context` + `--headless=new` trick: a
real Chrome process running without a visible window, plus a saved profile
so Akamai treats us as a returning user.

URL format:
    https://www.foundit.in/search/{role-slug}-jobs-in-{city-slug}
        ?start={offset}&limit=20&query={role}&location={loc-text}
        &queryDerived=true[&jobFreshness={days}]
        [&experience={yrs}&experienceRanges={yrs}~{yrs}]

Filters:
    jobFreshness     — posted within N days (1, 3, 7, 15, 30)
    experience       — exact years of experience (0..30); paired with
                       experienceRanges=N~N as foundit expects both
    start            — pagination offset (1, 21, 41, ...)
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import random
import re
import os
from urllib.parse import quote_plus
from datetime import datetime, timedelta


# Display name -> (URL path slug, location query param value)
FOUNDIT_CITIES = {
    "Bengaluru": ("bengaluru-bangalore", "bengaluru / bangalore"),
    "Hyderabad": ("hyderabad",            "hyderabad"),
    "Mumbai":    ("mumbai",               "mumbai"),
    "Pune":      ("pune",                 "pune"),
    "Chennai":   ("chennai",              "chennai"),
    "Delhi":     ("delhi-ncr",            "delhi ncr"),
    "Noida":     ("noida",                "noida"),
    "Gurugram":  ("gurugram",             "gurugram"),
}

_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "_foundit_profile"
)

_POSTED_RE = re.compile(
    r"Posted\s+(\d+)\s*(minute|hour|day|week|month)", re.IGNORECASE
)


def _slug(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"-+", "-", text)
    return text.strip("-")


def _parse_posted(card_text):
    """Extract 'Posted X minutes/hours/days/weeks/months ago' from the card text."""
    if not card_text:
        return datetime.today().strftime("%Y-%m-%d")
    today = datetime.today()
    if "few seconds ago" in card_text.lower() or "just posted" in card_text.lower():
        return today.strftime("%Y-%m-%d")
    m = _POSTED_RE.search(card_text)
    if not m:
        return today.strftime("%Y-%m-%d")
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("minute", "hour"):
        return today.strftime("%Y-%m-%d")
    if unit == "day":
        return (today - timedelta(days=n)).strftime("%Y-%m-%d")
    if unit == "week":
        return (today - timedelta(weeks=n)).strftime("%Y-%m-%d")
    if unit == "month":
        return (today - timedelta(days=n * 30)).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


def _extract_skills(card_text):
    """Skills appear in 'In JD: A, B, C' line on the card."""
    if not card_text:
        return ""
    m = re.search(r"In JD\s*:\s*([^\n]+)", card_text)
    if not m:
        return ""
    return m.group(1).strip().rstrip("…").strip()


def _build_url(role, city, job_freshness, experience, start=1):
    path_slug, loc_query = FOUNDIT_CITIES[city]
    role_slug = _slug(role)
    qs = [
        f"start={int(start)}",
        "limit=20",
        f"query={quote_plus(role)}",
        f"location={quote_plus(loc_query)}",
        "queryDerived=true",
    ]
    if job_freshness:
        qs.append(f"jobFreshness={int(job_freshness)}")
    if experience is not None and experience != "":
        n = int(experience)
        qs.append(f"experience={n}")
        qs.append(f"experienceRanges={n}~{n}")
    return f"https://www.foundit.in/search/{role_slug}-jobs-in-{path_slug}?" + "&".join(qs)


def scrape_foundit(role, city, job_freshness_days, limit, experience=None):
    """
    role:               free-text role (e.g. "devops")
    city:               display city name (key of FOUNDIT_CITIES)
    job_freshness_days: int (1, 3, 7, 15, 30) or 0/None for any
    limit:              max results
    experience:         optional int (0..30 yrs)
    """
    if city not in FOUNDIT_CITIES:
        raise ValueError(f"Unknown city: {city}")
    if not role.strip():
        raise ValueError("role is required")

    os.makedirs(_PROFILE_DIR, exist_ok=True)

    all_jobs = []
    seen_links = set()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            _PROFILE_DIR,
            headless=False,
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
                "--headless=new",
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

        # Warmup on homepage so Akamai sees an organic-looking session
        try:
            page.goto("https://www.foundit.in/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(random.uniform(3.0, 4.5))
        except Exception:
            pass

        start = 1
        while len(all_jobs) < limit:
            url = _build_url(role, city, job_freshness_days, experience, start)
            print(f"[Foundit] Fetching: {url}")

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(3.5, 5.0))

                title = page.title() or ""
                if "Access Denied" in title:
                    print(f"[Foundit] BLOCKED at start={start}. Stopping.")
                    break

                # Trigger lazy load
                for _ in range(2):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1.2)

                # Cards: each is the rounded-2xl wrapper containing the job link
                # We anchor on the title link and walk up to the card wrapper
                # in JS, then read all fields from there in one go.
                cards_data = page.evaluate("""
                    () => {
                        const out = [];
                        const seen = new Set();
                        for (const lnk of document.querySelectorAll("a[href*='/job/']")) {
                            const href = lnk.getAttribute('href') || '';
                            if (!href || seen.has(href)) continue;
                            // Walk up to the rounded card wrapper
                            let n = lnk;
                            let card = null;
                            for (let i=0; i<10; i++) {
                                if (!n) break;
                                if (n.tagName === 'DIV' && /rounded-2xl/.test(n.className||'')) {
                                    card = n; break;
                                }
                                n = n.parentElement;
                            }
                            if (!card) continue;
                            seen.add(href);

                            const text = (sel) => {
                                const e = card.querySelector(sel);
                                return e ? (e.innerText || '').trim() : '';
                            };
                            out.push({
                                href: href,
                                title: text('h2.jobCardTitle'),
                                company: text('span.jobCardCompany'),
                                experience: text("[class*='jobCardExperience']"),
                                location: text("[class*='jobCardLocation']"),
                                card_text: card.innerText || '',
                            });
                        }
                        return out;
                    }
                """)

                if not cards_data:
                    print(f"[Foundit] No cards at start={start}")
                    break

                found_this_page = 0
                for cd in cards_data:
                    if len(all_jobs) >= limit:
                        break
                    try:
                        link = cd["href"]
                        if not link.startswith("http"):
                            link = "https://www.foundit.in" + link
                        if link in seen_links:
                            continue
                        title_txt = cd["title"] or ""
                        if not title_txt:
                            continue

                        seen_links.add(link)
                        all_jobs.append({
                            "Job Title": title_txt,
                            "Company": cd["company"] or "N/A",
                            "Location": cd["location"] or "",
                            "Posted": _parse_posted(cd["card_text"]),
                            "Link": link,
                            "Experience": cd["experience"] or "",
                            "Skills": _extract_skills(cd["card_text"]),
                            "Easy Apply": False,
                            "Apply Type": "Foundit Apply",
                        })
                        found_this_page += 1
                    except Exception as e:
                        print(f"[Foundit] Card error: {e}")
                        continue

                print(f"[Foundit] Got {found_this_page} jobs from start={start}")
                if found_this_page == 0:
                    break
                start += 20
                time.sleep(random.uniform(2.0, 3.0))

            except PWTimeout:
                print(f"[Foundit] Timeout at start={start}")
                break
            except Exception as e:
                print(f"[Foundit] Error: {e}")
                break

        ctx.close()

    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    all_jobs.sort(key=sort_key, reverse=True)
    return all_jobs[:limit]
