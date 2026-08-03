"""LinkedIn public job search scraper (no login required)."""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time
import random
import re
from datetime import datetime, timedelta


def _seconds_to_tpr(seconds):
    return f"r{int(seconds)}"


def _parse_linkedin_date(text):
    if not text:
        return datetime.today().strftime("%Y-%m-%d")
    text = text.lower().strip()
    today = datetime.today()

    if any(k in text for k in ("just", "minute", "second", "hour", "today")):
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


def _build_url(role, location, time_filter, start=0):
    base = "https://www.linkedin.com/jobs/search/?"
    params = [
        f"keywords={role.replace(' ', '%20')}",
        f"location={location.replace(' ', '%20')}",
        "sortBy=DD",
        f"f_TPR={_seconds_to_tpr(time_filter)}",
        f"start={start}",
        "position=1",
        "pageNum=0",
    ]
    return base + "&".join(params)


def scrape_linkedin(role, time_filter, limit, locations, apply_mode="include_easy"):
    all_jobs = []
    seen_links = set()

    mode_multipliers = {
        "include_easy": 1.0,
        "only_easy": 2.5,
        "only_external": 2.5,
    }
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

        try:
            page.goto("https://www.linkedin.com", wait_until="domcontentloaded", timeout=15000)
            time.sleep(random.uniform(1.5, 2.5))
        except Exception:
            pass

        for location in locations:
            if len(all_jobs) >= internal_limit:
                break

            start = 0
            while len(all_jobs) < internal_limit:
                url = _build_url(role, location, time_filter, start)
                print(f"[LinkedIn] Fetching: {url}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    time.sleep(random.uniform(3.0, 4.5))

                    page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                    time.sleep(1.0)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1.5)

                    cards = page.query_selector_all("ul.jobs-search__results-list > li")
                    if not cards:
                        cards = page.query_selector_all("div.base-card")
                    if not cards:
                        print(f"[LinkedIn] No cards at start={start} for '{location}'")
                        break

                    found_this_page = 0

                    for card in cards:
                        if len(all_jobs) >= internal_limit:
                            break
                        try:
                            title_el = (
                                card.query_selector("h3.base-search-card__title")
                                or card.query_selector("span.sr-only")
                                or card.query_selector("h3")
                            )
                            title = title_el.inner_text().strip() if title_el else None
                            if not title or title.lower() in ("", "n/a"):
                                continue

                            comp_el = (
                                card.query_selector("h4.base-search-card__subtitle a")
                                or card.query_selector("h4.base-search-card__subtitle")
                                or card.query_selector("a.hidden-nested-link")
                            )
                            company = comp_el.inner_text().strip() if comp_el else "N/A"

                            loc_el = (
                                card.query_selector("span.job-search-card__location")
                                or card.query_selector("span.base-search-card__metadata")
                            )
                            loc_text = loc_el.inner_text().strip() if loc_el else location

                            time_el = card.query_selector("time")
                            if time_el:
                                posted = time_el.get_attribute("datetime") or _parse_linkedin_date(time_el.inner_text())
                            else:
                                date_el = (
                                    card.query_selector("span.job-search-card__listdate")
                                    or card.query_selector("span[class*='listdate']")
                                )
                                posted = _parse_linkedin_date(date_el.inner_text() if date_el else "")

                            if posted and len(posted) >= 10:
                                posted = posted[:10]

                            link_el = (
                                card.query_selector("a.base-card__full-link")
                                or card.query_selector("a[data-tracking-control-name='public_jobs_jserp-result_search-card']")
                                or card.query_selector("a[href*='/jobs/view/']")
                            )
                            if link_el:
                                href = link_el.get_attribute("href") or ""
                                link = href.split("?")[0].strip()
                                if not link.startswith("http"):
                                    link = "https://www.linkedin.com" + link
                            else:
                                continue

                            if "/jobs/view/" not in link:
                                continue
                            if link in seen_links:
                                continue

                            easy_apply = False
                            apply_type = "External"
                            posted_detail = None

                            card_text = (card.inner_text() or "").lower()
                            if "easy apply" in card_text:
                                easy_apply = True

                            if apply_mode == "only_external" and easy_apply:
                                continue

                            try:
                                detail = context.new_page()
                                detail.goto(link, wait_until="domcontentloaded", timeout=20000)
                                time.sleep(random.uniform(2.0, 3.0))
                                detail.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                                time.sleep(1.0)

                                d_time_el = detail.query_selector("time")
                                if d_time_el:
                                    posted_attr = d_time_el.get_attribute("datetime")
                                    if posted_attr:
                                        posted_detail = posted_attr[:10]
                                    else:
                                        posted_detail = _parse_linkedin_date(d_time_el.inner_text() or "")

                                if not easy_apply:
                                    detail_text_full = (detail.inner_text("body") or "").lower()

                                    apply_btn = (
                                        detail.query_selector("button.apply-button")
                                        or detail.query_selector("a.apply-button")
                                    )
                                    ext_btn = detail.query_selector(".sign-up-modal__outlet")

                                    if apply_btn and not ext_btn:
                                        easy_apply = True
                                    elif apply_btn:
                                        href = (apply_btn.get_attribute("href") or "").lower()
                                        if "linkedin.com/signup" in href or "linkedin.com/login" in href or "session_redirect" in href:
                                            easy_apply = True

                                    if not easy_apply:
                                        easy_apply_btn = (
                                            detail.query_selector("button[aria-label*='Easy apply']")
                                            or detail.query_selector("button[aria-label*='easy apply']")
                                            or detail.query_selector("button:has-text('Easy Apply')")
                                            or detail.query_selector(".apply-button--easy-apply")
                                            or detail.query_selector("[data-is-easy-apply-button]")
                                            or detail.query_selector(".jobs-apply-button--easy-apply")
                                        )
                                        if easy_apply_btn:
                                            easy_apply = True

                                    if not easy_apply and "easy apply" in detail_text_full:
                                        easy_apply = True

                                if easy_apply:
                                    apply_type = "Easy Apply"
                                else:
                                    detail_text_full = (detail.inner_text("body") or "").lower()
                                    if re.search(r"apply\s+on\s+(company|website)|company\s+site|apply\s+via|company website", detail_text_full):
                                        apply_type = "Company Site"
                                    else:
                                        apply_type = "External"

                                if not posted_detail and posted:
                                    posted_detail = posted[:10]

                                detail.close()
                            except Exception as e:
                                try:
                                    detail.close()
                                except Exception:
                                    pass
                                print(f"[LinkedIn] Detail error for {title}: {e}")
                                posted_detail = posted[:10] if posted else None

                            if apply_mode == "only_external" and easy_apply:
                                continue
                            if apply_mode == "only_easy" and not easy_apply:
                                continue

                            seen_links.add(link)
                            all_jobs.append({
                                "Job Title": title,
                                "Company": company,
                                "Location": loc_text,
                                "Posted": (posted_detail or (posted[:10] if posted else "")),
                                "Link": link,
                                "Easy Apply": easy_apply,
                                "Apply Type": apply_type,
                            })
                            found_this_page += 1

                        except Exception as e:
                            print(f"[LinkedIn] Card error: {e}")
                            continue

                    print(f"[LinkedIn] Got {found_this_page} jobs from {location} start={start}")

                    if found_this_page == 0:
                        break
                    start += 25
                    time.sleep(random.uniform(2.0, 3.5))

                except PWTimeout:
                    print(f"[LinkedIn] Timeout: {location}")
                    break
                except Exception as e:
                    print(f"[LinkedIn] Error: {e}")
                    break

        browser.close()

    def sort_key(j):
        try:
            return datetime.strptime(j["Posted"][:10], "%Y-%m-%d")
        except Exception:
            return datetime.min

    all_jobs.sort(key=sort_key, reverse=True)
    return all_jobs[:limit]
