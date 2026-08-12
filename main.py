import os
import re
import json
import time
import html as html_lib
import requests
from google.genai import errors
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

PUSH_URL = os.environ.get("PUSH_URL") or "https://pushnotifapp.netlify.app/api/publish"
PUSH_TOKEN = os.environ.get("PUSH_TOKEN") or "w7CUAMuJyihXM5_lsPChcQViQVh25KDn"

ARCHIVE_PATH = "docs/jobs.json"

# How far back a listing counts as "new" for this digest. Kept in one place so
# every source shares the same window (and so it can be widened temporarily to
# check a scraper still extracts listings, independently of the date filter).
WINDOW_HOURS = 24

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def site_url():
    """Public GitHub Pages URL, derived from the Actions environment when
    available so the notification carries a link to the digest. Falls back to
    this repo's own Pages URL for local runs, where GITHUB_REPOSITORY is unset."""
    repo = os.environ.get("GITHUB_REPOSITORY") or "JustaSingular/Job-Digest"
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/"


def notify(message, title="T&T Job Digest"):
    """Pushes a run summary to the push service. Never raises - a failed
    notification must not fail the digest run."""
    link = site_url()
    if link:
        message = f"{message}\n\n{link}"
    try:
        requests.post(
            PUSH_URL,
            headers={"Authorization": f"Bearer {PUSH_TOKEN}"},
            json={"title": title, "message": message},
            timeout=15,
        )
        print("push: sent notification")
    except requests.RequestException as e:
        print(f"push: failed to send notification: {e}")


def _one_line(text):
    """Collapses whitespace so a listing is guaranteed to occupy exactly one
    line. main() joins listings with newlines before handing them to Gemini,
    so a stray newline inside one listing would read as several. Also drops
    zero-width characters, which some boards sprinkle through job titles and
    which would otherwise ride through into the archive and the page."""
    cleaned = re.sub(r"[​-‍﻿]", "", text or "")
    return re.sub(r"\s+", " ", cleaned).strip()


def _within_window(relative_text):
    """Parses IslandJobHunt-style relative time strings like '23min ago',
    '10h ago', '2d ago', '1w ago', '2m ago' (months) and returns True if the
    listing falls inside the digest window."""
    if not relative_text:
        return False
    text = relative_text.lower().strip()

    if re.search(r"(\d+)\s*min", text):
        return True  # minutes ago is inside any sane window

    for pattern, hours in (
        (r"(\d+)\s*h(?:our)?s?\b", 1),
        (r"(\d+)\s*d(?:ay)?s?\b", 24),
        (r"(\d+)\s*w(?:eek)?s?\b", 24 * 7),
        (r"(\d+)\s*mo(?:nth)?s?\b", 24 * 30),
    ):
        m = re.search(pattern, text)
        if m:
            return int(m.group(1)) * hours <= WINDOW_HOURS

    if "today" in text:
        return True

    return False


def _day_cutoff():
    """Start of the digest window for sources that only publish a date (no
    clock time). Matches the CaribbeanJobs convention: midnight of yesterday,
    so a listing dated today or yesterday still counts as 'within 24h'."""
    return (datetime.now() - timedelta(hours=WINDOW_HOURS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _looks_blocked(body):
    """True if a response is a bot/JS challenge rather than real content.
    Pin.tt and Eve Caribbean sit behind Cloudflare - they answer plain
    requests today, but a CI runner IP is far likelier to get challenged, so
    these scrapers must recognise a wall and bow out quietly instead of
    reporting zero listings as if the sites were simply empty."""
    if len(body) > 40000:
        return False  # a challenge page is always small
    return bool(re.search(
        r"cf-browser-verification|Just a moment|Checking your browser|"
        r"Enable JavaScript and cookies|Javascript is required",
        body, re.I))


def scrape_islandjobhunt():
    """Scrapes recent Trinidad & Tobago listings from IslandJobHunt."""
    url = "https://islandjobhunt.com/jobs"
    params = {"location": "trinidad"}
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if res.status_code != 200:
            print(f"IslandJobHunt returned status {res.status_code}, skipping.")
            return []

        raw_html = res.text
        listings = []

        # Job title links look like href="/jobs/539439022-sales-representative".
        # Slice the raw HTML between one title link and the next, rather than
        # walking the parsed DOM tree - this doesn't depend on guessing how
        # deeply the real page nests its containers.
        title_pattern = re.compile(r'<a[^>]*href="(/jobs/\d+-[^"#?]*)"')
        matches = list(title_pattern.finditer(raw_html))

        if not matches:
            print("IslandJobHunt: 0 job links found - page structure may have changed.")
            return []

        for i, m in enumerate(matches):
            href = m.group(1)
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else min(start + 4000, len(raw_html))
            block_html = raw_html[start:end]

            block_soup = BeautifulSoup(block_html, "html.parser")
            first_link = block_soup.find("a")
            title = first_link.get_text(strip=True) if first_link else ""
            if not title:
                continue

            block_text = " | ".join(block_soup.stripped_strings)

            time_match = re.search(r"(\d+\s*(?:min|h|hours?|d|days?|w|weeks?|m|mo|months?)\s*ago)", block_text)
            time_str = time_match.group(1) if time_match else ""

            if not _within_window(time_str):
                continue

            full_link = "https://islandjobhunt.com" + href
            listings.append(
                f"Source: IslandJobHunt | Listing: {_one_line(title)} | "
                f"Details: {_one_line(block_text)} | URL: {full_link}"
            )

        print(f"IslandJobHunt: {len(matches)} total listings found, {len(listings)} within last 24h.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping IslandJobHunt: {e}")
        return []


def _jsonld_jobposting(soup):
    """Pulls the schema.org JobPosting object out of a page's JSON-LD, coping
    with the usual shapes: a bare object, a list, or an @graph wrapper."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        candidates = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = data["@graph"]

        for obj in candidates:
            if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                return obj
    return None


def _jsonld_location(posting):
    """Flattens JobPosting.jobLocation into a readable place string."""
    loc = posting.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return ""

    addr = loc.get("address")
    if isinstance(addr, str):
        return addr
    if not isinstance(addr, dict):
        return ""

    parts = [addr.get("addressLocality"), addr.get("addressRegion"),
             addr.get("addressCountry")]
    return ", ".join(p for p in parts if isinstance(p, str) and p.strip())


def scrape_jobstt():
    """Scrapes recent listings from JobsTT.

    JobsTT used to answer plain HTTP clients with a JavaScript/bot challenge,
    which is why this was previously skipped - it no longer does. Rather than
    walking the paginated listing HTML (10 jobs a page), we read
    sitemap-job.xml, which names every job URL with a <lastmod> stamp, then
    fetch only the recently-touched ones. Each job page carries a schema.org
    JobPosting block, so title/company/location/datePosted come from JSON-LD
    instead of guessed selectors."""
    sitemap = "https://jobstt.com/sitemap-job.xml"
    try:
        res = requests.get(sitemap, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"JobsTT sitemap returned status {res.status_code}, skipping.")
            return []

        entries = re.findall(
            r"<url>\s*<loc>(.*?)</loc>.*?<lastmod>(.*?)</lastmod>\s*</url>",
            res.text, re.S)
        if not entries:
            # Fall back to bare <loc> list if the sitemap drops <lastmod>.
            entries = [(loc, "") for loc in re.findall(r"<loc>(.*?)</loc>", res.text)]

        # <lastmod> also moves when an employer edits an old posting, so it is
        # only a cheap prefilter here - the authoritative date is datePosted
        # on the job page itself. Allow a few days of slack past the window.
        prefilter = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS + 72)
        recent = []
        for loc, lastmod in entries:
            if not loc.startswith("http"):
                continue
            if not lastmod:
                recent.append((loc, None))
                continue
            try:
                stamp = datetime.fromisoformat(lastmod.strip())
            except ValueError:
                recent.append((loc, None))
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp >= prefilter:
                recent.append((loc, stamp))

        recent.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc),
                    reverse=True)
        # Bound the per-run request count (one fetch per candidate job).
        max_fetches = 25
        candidates, dropped = recent[:max_fetches], max(0, len(recent) - max_fetches)
        print(f"JobsTT: {len(entries)} jobs in sitemap, {len(recent)} touched recently"
              + (f" ({dropped} beyond the {max_fetches}-fetch cap not checked)" if dropped else "")
              + ".")
        recent = candidates

        cutoff = _day_cutoff()
        listings = []
        for loc, _stamp in recent:
            try:
                page = requests.get(loc, headers=HEADERS, timeout=20)
            except requests.RequestException as e:
                print(f"JobsTT: failed to fetch {loc}: {e}")
                continue
            if page.status_code != 200:
                continue

            soup = BeautifulSoup(page.text, "html.parser")
            posting = _jsonld_jobposting(soup)
            if not posting:
                continue

            posted_raw = (posting.get("datePosted") or "").strip()
            if not posted_raw:
                continue
            try:
                # datePosted is date-only ("2026-08-06"); take the date part
                # of anything longer so a timestamped variant still parses.
                posted = datetime.fromisoformat(posted_raw.replace("Z", "")[:19])
            except ValueError:
                continue
            if posted.tzinfo is not None:
                posted = posted.replace(tzinfo=None)
            if posted < cutoff:
                continue

            title = (posting.get("title") or "").strip()
            if not title:
                title = soup.title.get_text(strip=True).replace(" - JobsTT", "") if soup.title else ""
            if not title:
                continue

            org = posting.get("hiringOrganization") or {}
            company = (org.get("name") or "").strip() if isinstance(org, dict) else ""
            location = _jsonld_location(posting)
            employment = posting.get("employmentType") or ""
            if isinstance(employment, list):
                employment = ", ".join(str(e) for e in employment)

            details = " | ".join(p for p in [
                f"Company: {company}" if company else "",
                f"Location: {location}" if location else "",
                f"Type: {employment}" if employment else "",
                f"Posted: {posted.date().isoformat()}",
            ] if p)

            listings.append(
                f"Source: JobsTT | Listing: {_one_line(title)} | "
                f"Details: {_one_line(details)} | URL: {loc}"
            )

        print(f"JobsTT: {len(listings)} listings within last 24h.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping JobsTT: {e}")
        return []


CARIBBEAN_LINK = re.compile(r"-Job-\d+\.aspx")  # /Some-Job-Title-Job-233666.aspx
CARIBBEAN_UPDATED = re.compile(r"Updated (\d{2}/\d{2}/\d{4})")


def scrape_caribbeanjobs():
    """Scrapes recent Trinidad & Tobago listings from CaribbeanJobs."""
    url = "https://www.caribbeanjobs.com/ShowResults.aspx"
    params = {"Location": "124"}  # 124 = Trinidad and Tobago
    try:
        res = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if res.status_code != 200:
            print(f"CaribbeanJobs returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")

        # Each result card links to the same job twice - once on the title and
        # once on a "Show More" control - so group by URL and keep the longest
        # link text, which is the real title.
        grouped = {}
        for a in soup.find_all("a", href=CARIBBEAN_LINK):
            grouped.setdefault(a["href"], []).append(a)

        if not grouped:
            print("CaribbeanJobs: 0 job links found - page structure may have changed.")
            return []

        cutoff = _day_cutoff()
        listings = []

        for href, anchors in grouped.items():
            title = _best_anchor_text(anchors)
            if not title:
                continue

            # Take the "Updated DD/MM/YYYY" stamp from the smallest block that
            # holds this job alone. Walking up a fixed number of levels used to
            # overshoot into the whole results list, where the first stamp found
            # belonged to some other (often newer) job.
            block_text = _ad_card_text(anchors[0], CARIBBEAN_LINK, CARIBBEAN_UPDATED)
            if not block_text:
                continue

            try:
                updated_date = datetime.strptime(
                    CARIBBEAN_UPDATED.search(block_text).group(1), "%d/%m/%Y")
            except ValueError:
                continue

            if updated_date < cutoff:
                continue

            full_link = href
            if not full_link.startswith("http"):
                full_link = "https://www.caribbeanjobs.com" + full_link

            listings.append(
                f"Source: CaribbeanJobs | Listing: {_one_line(title)} | "
                f"Details: {_one_line(block_text)[:250]} | URL: {full_link}"
            )

        print(f"CaribbeanJobs: {len(grouped)} jobs found, {len(listings)} within last 24h.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping CaribbeanJobs: {e}")
        return []


def _best_anchor_text(anchors):
    """Listing rows usually link twice - once wrapping a thumbnail (no text)
    and once wrapping the title. Pick the longest text of the group."""
    texts = [a.get_text(" ", strip=True) for a in anchors]
    return max(texts, key=len) if texts else ""


def _ad_card_text(anchor, link_pattern, stamp_pattern, max_levels=6):
    """Walks up from a listing link to the smallest ancestor that carries the
    posting stamp, and refuses to walk into a container holding more than one
    listing - otherwise a card without its own stamp silently inherits the
    neighbouring ad's date, which is exactly the bug that makes a 24h filter
    quietly wrong."""
    node = anchor
    for _ in range(max_levels):
        if node.parent is None:
            return ""
        node = node.parent

        hrefs = {a["href"] for a in node.find_all("a", href=True)
                 if link_pattern.search(a["href"])}
        if len(hrefs) > 1:
            return ""  # walked into a multi-listing container - give up

        text = " | ".join(node.stripped_strings)
        if stamp_pattern.search(text):
            return text
    return ""


PINTT_AD = re.compile(r"/adv/\d+_")
PINTT_STAMP = re.compile(r"\b(\d{2})\.(\d{2})\.(20\d\d)\s+(\d{2}):(\d{2})")


def scrape_pintt():
    """Scrapes recent job ads from Pin.tt, a local classifieds site.

    Ads live at /adv/<id>_<slug>/ and every card carries a 'dd.mm.yyyy HH:MM'
    posting stamp - minute-level precision, so unlike the other sources this
    one needs no relative-time or midnight-rounding guesswork."""
    url = "https://pin.tt/jobs/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"Pin.tt returned status {res.status_code}, skipping.")
            return []
        if _looks_blocked(res.text):
            print("Pin.tt: blocked by a Cloudflare challenge this run, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")

        grouped = {}
        for a in soup.find_all("a", href=True):
            if PINTT_AD.search(a["href"]):
                grouped.setdefault(a["href"], []).append(a)

        if not grouped:
            print("Pin.tt: 0 ad links found - page structure may have changed.")
            return []

        cutoff = datetime.now() - timedelta(hours=WINDOW_HOURS)
        listings = []
        undated = 0

        for href, anchors in grouped.items():
            title = _best_anchor_text(anchors)
            if not title:
                continue

            card = _ad_card_text(anchors[0], PINTT_AD, PINTT_STAMP)
            if not card:
                # Promoted/VIP ads render without their own stamp; skipping is
                # better than dating them from a neighbour.
                undated += 1
                continue

            m = PINTT_STAMP.search(card)
            day, month, year, hour, minute = (int(g) for g in m.groups())
            try:
                posted = datetime(year, month, day, hour, minute)
            except ValueError:
                continue

            if posted < cutoff:
                continue

            full_link = href if href.startswith("http") else "https://pin.tt" + href
            listings.append(
                f"Source: Pin.tt | Listing: {_one_line(title)} | "
                f"Details: {_one_line(card)[:250]} | URL: {full_link}"
            )

        print(f"Pin.tt: {len(grouped)} ads found, {len(listings)} within last 24h "
              f"({undated} skipped for having no own timestamp).")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping Pin.tt: {e}")
        return []


EMPLOYTT_LINK = re.compile(r"/jobs/view/\d+")
EMPLOYTT_DATE = re.compile(r"\b(\d{2})/(\d{2})/(20\d\d)\b")


def _employtt_date(match):
    """EmployTT prints dates month-first (a row's '08/14/2026' pins it down)."""
    month, day, year = (int(g) for g in match)
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def scrape_employtt():
    """Scrapes recent listings from EmployTT, the government employment portal.

    Each row exposes three US-format (mm/dd/yyyy) dates - created, published
    and application deadline. The middle one is the published date: it is what
    the site's own 'N days ago' label counts from, which is how it was
    identified. Listings whose deadline has passed are dropped."""
    url = "https://employtt.gov.tt/jobs/list"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"EmployTT returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")

        grouped = {}
        for a in soup.find_all("a", href=True):
            if EMPLOYTT_LINK.search(a["href"]):
                grouped.setdefault(a["href"], []).append(a)

        if not grouped:
            print("EmployTT: 0 job links found - page structure may have changed.")
            return []

        cutoff = _day_cutoff()
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        listings = []

        for href, anchors in grouped.items():
            title = _best_anchor_text(anchors)
            if not title:
                continue

            # Climb to the row that actually holds the date columns.
            node, row_text = anchors[0], ""
            for _ in range(5):
                if node.parent is None:
                    break
                node = node.parent
                candidate = " | ".join(node.stripped_strings)
                if len(EMPLOYTT_DATE.findall(candidate)) >= 2:
                    row_text = candidate
                    break
            if not row_text:
                continue

            dates = [d for d in (_employtt_date(m)
                                 for m in EMPLOYTT_DATE.findall(row_text)) if d]
            if len(dates) < 2:
                continue

            posted = dates[1]
            if posted < cutoff:
                continue
            if len(dates) > 2 and dates[2] < today:
                continue  # application deadline already passed

            full_link = href if href.startswith("http") else "https://employtt.gov.tt" + href
            listings.append(
                f"Source: EmployTT | Listing: {_one_line(title)} | "
                f"Details: {_one_line(row_text)[:250]} | URL: {full_link}"
            )

        print(f"EmployTT: {len(grouped)} jobs found, {len(listings)} within last 24h.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping EmployTT: {e}")
        return []


EVE_LINK = re.compile(r"/tt/job/[a-z0-9-]+/?$", re.I)
EVE_DATE = re.compile(r"Listed\s+([A-Z][a-z]+\s+\d{1,2},\s*20\d\d)")


def scrape_evecaribbean():
    """Scrapes recent listings from Eve Caribbean, the job board run by Eve
    Anderson Recruitment. Cards are labelled 'Listed <Month D, YYYY>'."""
    url = "https://evecaribbean.com/tt/jobs/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"Eve Caribbean returned status {res.status_code}, skipping.")
            return []
        if _looks_blocked(res.text):
            print("Eve Caribbean: blocked by a Cloudflare challenge this run, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")

        grouped = {}
        for a in soup.find_all("a", href=True):
            if EVE_LINK.search(a["href"]):
                grouped.setdefault(a["href"], []).append(a)

        if not grouped:
            print("Eve Caribbean: 0 job links found - page structure may have changed.")
            return []

        cutoff = _day_cutoff()
        listings = []

        for href, anchors in grouped.items():
            title = _best_anchor_text(anchors)
            if not title:
                continue

            card = _ad_card_text(anchors[0], EVE_LINK, EVE_DATE)
            if not card:
                continue

            try:
                posted = datetime.strptime(
                    EVE_DATE.search(card).group(1).replace(",", ", ").replace("  ", " "),
                    "%B %d, %Y")
            except ValueError:
                continue

            if posted < cutoff:
                continue

            full_link = href if href.startswith("http") else "https://evecaribbean.com" + href
            listings.append(
                f"Source: EveCaribbean | Listing: {_one_line(title)} | "
                f"Details: {_one_line(card)[:250]} | URL: {full_link}"
            )

        print(f"Eve Caribbean: {len(grouped)} jobs found, {len(listings)} within last 24h.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping Eve Caribbean: {e}")
        return []


# --------------------------------------------- employer career sites
#
# The portals above carry hundreds of standing listings, so each one is cut
# down to WINDOW_HOURS. Employer boards work the other way round: they show
# only what is currently open - a couple of dozen roles at most - and most
# publish no posting date at all. So these scrapers return every open role
# and let the archive's link-based dedup decide what is new, which reports a
# vacancy on the first run that sees it and never again. The exception is
# NIDCO, which leaves expired adverts up for years and so is filtered on the
# date carried in each advert's filename.
#
# Employers that block datacenter IPs (First Citizens, ANSA McAL, COLFIRE,
# Guardian Media, SM Jaleel all answer 403) or that render postings only in
# the browser (Sagicor, UTC, National Energy, Agostini, Massy) are absent on
# purpose - see scrapability-audit.md.


def scrape_bamboohr(subdomain, employer):
    """Reads an employer's open roles from BambooHR's JSON listing.

    Parameterised by subdomain because one function then covers every
    employer on the platform - Maritime Financial and Hadco today, anyone
    else who moves onto BambooHR later for the cost of one line."""
    url = f"https://{subdomain}.bamboohr.com/careers/list"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"{employer}: BambooHR returned status {res.status_code}, skipping.")
            return []

        result = res.json().get("result", [])
        listings = []

        for job in result:
            title = _one_line(job.get("jobOpeningName") or "")
            job_id = job.get("id")
            if not title or job_id is None:
                continue

            place = job.get("location") or {}
            where = ", ".join(p for p in (place.get("city"), place.get("state")) if p)
            details = " | ".join(p for p in (
                f"Employer: {employer}",
                f"Location: {where}" if where else "Location: Trinidad & Tobago",
                job.get("departmentLabel") or "",
                job.get("employmentStatusLabel") or "",
            ) if p)

            listings.append(
                f"Source: {employer} | Listing: {title} | "
                f"Details: {_one_line(details)} | "
                f"URL: https://{subdomain}.bamboohr.com/careers/{job_id}"
            )

        print(f"{employer} (BambooHR): {len(result)} open roles, {len(listings)} usable.")
        return listings[:40]
    except (requests.RequestException, ValueError) as e:
        print(f"Error scraping {employer} (BambooHR): {e}")
        return []


def scrape_oracle_recruiting(host, site_number, employer, country="TT"):
    """Reads open requisitions from an Oracle Recruiting career site.

    Guardian Group's site serves the whole region, so requisitions are cut to
    one country: PrimaryLocationCountry is the field that tells a Port of
    Spain role from a Curacao one (the human-readable PrimaryLocation is free
    text and not reliable enough to match on)."""
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
           f"?onlyData=true&expand=requisitionList"
           f"&finder=findReqs;siteNumber={site_number},limit=200")
    try:
        res = requests.get(api, headers=HEADERS, timeout=25)
        if res.status_code != 200:
            print(f"{employer}: Oracle Recruiting returned status {res.status_code}, skipping.")
            return []

        items = res.json().get("items") or []
        requisitions = items[0].get("requisitionList", []) if items else []

        listings = []
        for req in requisitions:
            if country and req.get("PrimaryLocationCountry") != country:
                continue

            title = _one_line(req.get("Title") or "")
            req_id = req.get("Id")
            if not title or req_id is None:
                continue

            details = " | ".join(p for p in (
                f"Employer: {employer}",
                f"Location: {req.get('PrimaryLocation') or 'Trinidad & Tobago'}",
                f"Posted: {req.get('PostedDate')}" if req.get("PostedDate") else "",
            ) if p)

            listings.append(
                f"Source: {employer} | Listing: {title} | "
                f"Details: {_one_line(details)} | "
                f"URL: https://{host}/hcmUI/CandidateExperience/en/sites/"
                f"{site_number}/job/{req_id}"
            )

        print(f"{employer} (Oracle): {len(requisitions)} requisitions, "
              f"{len(listings)} in {country}.")
        return listings[:40]
    except (requests.RequestException, ValueError, IndexError, AttributeError) as e:
        print(f"Error scraping {employer} (Oracle Recruiting): {e}")
        return []


DIGICEL_JOB = re.compile(r"/job/[^/]+/\d+")


def scrape_digicel():
    """Scrapes Digicel's Trinidad vacancies.

    Digicel runs a SuccessFactors site, which usually means the postings are
    drawn by JavaScript and invisible to a plain request. This one is the
    exception - it renders results server-side, so the location filter can go
    in the query string and the rows read straight out of the HTML. Every job
    is linked twice per row, hence the dedup on href."""
    url = "https://careers.digicelgroup.com/search/?q=&locationsearch=Trinidad"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"Digicel returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        seen, listings = set(), []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not DIGICEL_JOB.search(href) or href in seen:
                continue

            title = _one_line(a.get_text(" ", strip=True))
            if not title:
                continue
            seen.add(href)

            # The row carries the location in its own cell. Reading it from the
            # row's full text instead would sweep up the repeated job title,
            # since each row prints the title once for desktop and once for phone.
            row = a.find_parent("tr")
            cell = row.find("span", class_="jobLocation") if row else None
            place = _one_line(cell.get_text(" ", strip=True)) if cell else ""
            full_link = href if href.startswith("http") else "https://careers.digicelgroup.com" + href

            listings.append(
                f"Source: Digicel | Listing: {title} | "
                f"Details: Employer: Digicel | "
                f"Location: {place or 'Trinidad & Tobago'} | "
                f"URL: {full_link}"
            )

        if not listings:
            print("Digicel: 0 job links found - page structure may have changed.")
        else:
            print(f"Digicel: {len(listings)} Trinidad roles found.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping Digicel: {e}")
        return []


# Filenames lead with the advert's date, in either 2025-12-03 or 2024-08_12 form.
NIDCO_STAMP = re.compile(r"(20\d\d)[-_](\d{2})[-_](\d{2})")
NIDCO_MAX_AGE_DAYS = 180


def scrape_nidco():
    """Scrapes NIDCO's vacancy adverts.

    Each vacancy is a PDF whose link text is the job title. Expired adverts
    are never taken down - the page still carries ads from 2022 - so the date
    in the filename decides what is still live. Adverts with no date in the
    filename are all pre-2023 and are dropped with them."""
    url = "https://www.nidco.co.tt/careers/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"NIDCO returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        cutoff = datetime.now() - timedelta(days=NIDCO_MAX_AGE_DAYS)
        adverts, listings = 0, []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf"):
                continue

            title = _one_line(a.get_text(" ", strip=True))
            if not title:
                continue
            adverts += 1

            stamp = NIDCO_STAMP.search(href.rsplit("/", 1)[-1])
            if not stamp:
                continue
            try:
                posted = datetime(int(stamp.group(1)), int(stamp.group(2)),
                                  int(stamp.group(3)))
            except ValueError:
                continue
            if posted < cutoff:
                continue

            full_link = href if href.startswith("http") else "https://www.nidco.co.tt" + href
            listings.append(
                f"Source: NIDCO | Listing: {title} | "
                f"Details: Employer: NIDCO | Location: Trinidad & Tobago | "
                f"Posted: {posted.date().isoformat()} | URL: {full_link}"
            )

        print(f"NIDCO: {adverts} adverts on file, {len(listings)} posted in the "
              f"last {NIDCO_MAX_AGE_DAYS} days.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping NIDCO: {e}")
        return []


PATT_SECTION_HEADING = re.compile(r"^(vacanc|career|opportunit|job)", re.I)
ROLE_WORD = re.compile(
    r"\b(manager|officer|analyst|engineer|technician|supervisor|clerk|assistant|"
    r"accountant|developer|administrator|coordinator|specialist|attendant|driver|"
    r"cashier|teller|operator|representative|executive|director|intern|trainee|"
    r"graduate|apprentice|consultant|auditor|architect|designer|planner|secretary|"
    r"receptionist|foreman|welder|electrician|agent|programmer|chemist|nurse|"
    r"surveyor|inspector|associate|advisor|adviser|attorney|counsel|buyer|"
    r"controller|economist|mechanic|fitter|steward|seaman|rating)\b", re.I)


def scrape_patt():
    """Scrapes the Port Authority's vacancy page.

    Vacancies here are headings, not links, so there is no per-job URL. Each
    job is given the page URL plus a slug fragment: without a distinct link
    the archive's link-based key would fold every Port Authority vacancy into
    a single entry, and only the first would ever be reported.

    Certificate verification is off for this host alone. patnt.com serves an
    incomplete chain - it omits the intermediate, which browsers fetch on
    their own and requests does not - so verification fails against an
    otherwise valid certificate. The page is public job adverts and nothing
    is sent to the server, so the exposure is limited to reading a tampered
    vacancy list; the alternative is dropping the source entirely."""
    url = "https://www.patnt.com/about/vacancies/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if res.status_code != 200:
            print(f"Port Authority returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        body = soup.find("main") or soup
        listings, seen = [], set()

        for heading in body.find_all(["h2", "h3", "h4"]):
            title = _one_line(heading.get_text(" ", strip=True))
            title = re.sub(r"^vacanc(?:y|ies)\s*:\s*", "", title, flags=re.I)

            # "Vacancies" itself is the section header, not a job.
            if not title or len(title) > 120 or PATT_SECTION_HEADING.match(title):
                continue
            if not ROLE_WORD.search(title):
                continue

            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            if slug in seen:
                continue
            seen.add(slug)

            listings.append(
                f"Source: Port Authority | Listing: {title} | "
                f"Details: Employer: Port Authority of Trinidad and Tobago | "
                f"Location: Trinidad & Tobago | URL: {url}#{slug}"
            )

        print(f"Port Authority: {len(listings)} vacancies found.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping Port Authority: {e}")
        return []


NP_FILENAME_PREFIX = re.compile(r"^(NP|LFCTT)[-_]?Employment[-_]?(Opp|Opportunity)[a-z]*[-_]?", re.I)
NP_FILENAME_SUFFIX = re.compile(r"[-_](new[-_]?date|final|revised|edited)([-_]?\d+)?$", re.I)


def scrape_np():
    """Scrapes National Petroleum's vacancy adverts.

    Every advert is a PDF whose link text is just "Download", so the title has
    to be recovered from the filename - NP-Employment-Opp-ICT-Manager-new-date
    yields "ICT Manager". The results are rougher than the other sources and a
    few come through as the employer's own shorthand."""
    url = "https://www.np.co.tt/careers/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"National Petroleum returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        seen, listings = set(), []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.lower().endswith(".pdf") or href in seen:
                continue
            seen.add(href)

            stem = href.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            stem = NP_FILENAME_PREFIX.sub("", stem)
            stem = NP_FILENAME_SUFFIX.sub("", stem)
            stem = re.sub(r"[-_.]+", " ", stem)
            title = _one_line(re.sub(r"\s+\d+$", "", stem))
            if not title or len(title) < 3:
                continue

            full_link = href if href.startswith("http") else "https://www.np.co.tt" + href
            listings.append(
                f"Source: National Petroleum | Listing: {title} | "
                f"Details: Employer: National Petroleum (NP) | "
                f"Location: Trinidad & Tobago | URL: {full_link}"
            )

        print(f"National Petroleum: {len(listings)} vacancy adverts found.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping National Petroleum: {e}")
        return []


def scrape_us_embassy():
    """Scrapes vacancies at the US Embassy in Port of Spain.

    tt.usembassy.gov publishes nothing machine-readable; the mission's actual
    vacancies sit on the State Department's ERA board under the Trinidad and
    Tobago country path, which is server-rendered and already scoped to the
    country, so no location filtering is needed here."""
    url = "https://erajobs.state.gov/dos-era/tto/vacancysearch/searchVacancies.hms"
    try:
        res = requests.get(url, headers=HEADERS, timeout=20)
        if res.status_code != 200:
            print(f"US Embassy (ERA) returned status {res.status_code}, skipping.")
            return []

        soup = BeautifulSoup(res.text, "html.parser")
        seen, listings = set(), []

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "viewvacancydetail" not in href.lower() or href in seen:
                continue

            title = _one_line(a.get_text(" ", strip=True))
            if not title:
                continue
            seen.add(href)

            row = a.find_parent("tr")
            details = _one_line(row.get_text(" | ", strip=True))[:250] if row else ""
            full_link = href if href.startswith("http") else "https://erajobs.state.gov" + href

            listings.append(
                f"Source: US Embassy | Listing: {title} | "
                f"Details: Employer: U.S. Embassy Port of Spain | "
                f"Location: Port of Spain, Trinidad & Tobago | {details} | "
                f"URL: {full_link}"
            )

        print(f"US Embassy: {len(listings)} vacancies found.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping US Embassy: {e}")
        return []


def summarize_and_filter(raw_data_string, max_retries=3):
    """Uses Gemini to dedup listings and split them into IT vs. other,
    returned as structured JSON (not markdown) so the HTML page can render
    it reliably. Date/location filtering already happened in Python."""
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
    You are an automated career assistant. Below is raw scraped job data,
    all of it Trinidad & Tobago listings.

    From these job portals, already filtered to the last 24 hours:
    1. IslandJobHunt.com
    2. CaribbeanJobs.com
    3. JobsTT.com
    4. Pin.tt
    5. EmployTT (employtt.gov.tt)
    6. Eve Caribbean (evecaribbean.com)

    And from these employers' own career sites, which list whatever is
    currently open rather than what was posted today, so they carry no date
    filter (a later step drops any that were reported on an earlier run):
    7. Guardian Group
    8. Atlantic LNG
    9. Maritime Financial
    10. Hadco
    11. Digicel
    12. NIDCO
    13. Port Authority
    14. National Petroleum
    15. US Embassy

    YOUR TASK:
    1. Remove duplicate listings (same title + company appearing more than
       once). Employers routinely post the same vacancy to several of these
       portals, so collapse cross-posted duplicates too: keep one entry and
       prefer the one with the most complete company/location detail.
    2. Split every listing into one of two lists: "it_jobs" (Software
       Engineering, Web Development, IT Support, Systems Administration,
       Networking, Database/Data, Cybersecurity, Cloud, Tech Lead, Helpdesk,
       etc.) or "other_jobs" (everything else).
    3. Do not invent jobs that aren't in the raw data. Do not re-filter by
       date - that's already done.

    Respond with ONLY valid JSON, no markdown fences, no preamble, matching
    exactly this shape:
    {{
      "it_jobs": [
        {{"title": "...", "company": "...", "location": "...", "source": "IslandJobHunt", "link": "..."}}
      ],
      "other_jobs": [
        {{"title": "...", "company": "...", "location": "...", "source": "CaribbeanJobs", "link": "..."}}
      ]
    }}

    Use "" for company/location if genuinely not present in the raw text.
    For "source", copy the value after "Source:" on that listing's raw line
    verbatim (IslandJobHunt, CaribbeanJobs, JobsTT, Pin.tt, EmployTT,
    EveCaribbean, Guardian Group, Atlantic LNG, Maritime Financial, Hadco,
    Digicel, NIDCO, Port Authority, National Petroleum or US Embassy) - do
    not invent or reword it. Listings from an employer's own career site name
    that employer after "Employer:" in their details - use it as "company".

    RAW SCRAPED DATA:
    {raw_data_string}
    """

    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model="gemini-3.5-flash",
                contents=prompt,
                config={"response_mime_type": "application/json"},
            )
            break
        except errors.ServerError as e:
            print(f"Gemini server error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                print("Gemini unavailable after retries - giving up on this run.")
                return {"it_jobs": [], "other_jobs": []}
            wait = 5 * attempt
            print(f"Retrying in {wait}s...")
            time.sleep(wait)

    text = response.text.strip()
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"Gemini returned unparseable JSON: {e}")
        print(text[:500])
        return {"it_jobs": [], "other_jobs": []}


# ---------------------------------------------------------------- archive

def job_key(job):
    """Stable identity for a listing, so re-scraping the same job across
    runs doesn't create a duplicate archive entry."""
    link = (job.get("link") or "").strip().lower().rstrip("/")
    if link and link != "#":
        return link
    title = (job.get("title") or "").strip().lower()
    company = (job.get("company") or "").strip().lower()
    return f"{title}|{company}"


def load_archive(path=ARCHIVE_PATH):
    """Reads every job ever seen. Missing/corrupt file -> empty archive."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"Archive at {path} unreadable ({e}) - starting a fresh one.")
        return []

    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return [j for j in jobs if isinstance(j, dict)]


def save_archive(jobs, path=ARCHIVE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(jobs),
        "jobs": jobs,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Archive: {len(jobs)} jobs saved to {path}")


def merge_into_archive(archive, digest, run_iso):
    """Adds this run's listings to the archive without ever removing old
    ones. Returns the merged archive plus just the entries that are new."""
    seen = {job_key(j) for j in archive}
    new_jobs = []

    for category, jobs in (("it", digest.get("it_jobs", [])),
                           ("other", digest.get("other_jobs", []))):
        for job in jobs:
            key = job_key(job)
            if not key or key in seen:
                continue
            seen.add(key)
            new_jobs.append({
                "title": job.get("title", "Untitled"),
                "company": job.get("company", "") or "",
                "location": job.get("location", "") or "",
                "source": job.get("source", "") or "",
                "link": job.get("link", "") or "",
                "category": category,
                "first_seen": run_iso,
            })

    return archive + new_jobs, new_jobs


# ----------------------------------------------------------------- render

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def _seen_date(job):
    """YYYY-MM-DD of when a job first entered the archive."""
    return (job.get("first_seen") or "")[:10]


def _pretty_date(iso_day):
    try:
        d = datetime.strptime(iso_day, "%Y-%m-%d")
    except ValueError:
        return iso_day or "Undated"
    return f"{d.strftime('%A')}, {d.day} {MONTHS[d.month - 1]} {d.year}"


def _sorted_for_display(jobs):
    """Tech first, then alphabetical - the reader's eye goes to the top."""
    return sorted(jobs, key=lambda j: (j.get("category") != "it",
                                       (j.get("title") or "").lower()))


def _entry_html(job, show_day=False):
    title = html_lib.escape(job.get("title") or "Untitled")
    company = html_lib.escape(job.get("company") or "")
    location = html_lib.escape(job.get("location") or "")
    source = html_lib.escape(job.get("source") or "")
    link = html_lib.escape(job.get("link") or "#", quote=True)
    is_tech = job.get("category") == "it"

    meta_bits = []
    if company:
        meta_bits.append(f"<em>{company}</em>")
    if location:
        meta_bits.append(location)
    meta = " &middot; ".join(meta_bits) or "<em>Employer not stated</em>"

    haystack = " ".join(p for p in (title, company, location, source,
                                    "tech" if is_tech else "") if p).lower()
    haystack = html_lib.escape(haystack, quote=True)

    day = ""
    if show_day:
        iso = _seen_date(job)
        try:
            d = datetime.strptime(iso, "%Y-%m-%d")
            day = f'<span class="entry-day">{d.day} {MONTHS[d.month - 1][:3]}</span>'
        except ValueError:
            pass

    stamp = '<span class="stamp">Tech</span>' if is_tech else ""

    # Starring and binning are remembered in the reader's browser against this
    # key, so it has to survive the page being regenerated from scratch twice a
    # day. The link is what job_key uses for the same reason; the title/company
    # fallback covers the handful of listings that never carried a URL.
    key = html_lib.escape(
        job.get("link") or f"{job.get('title', '')}|{job.get('company', '')}",
        quote=True)

    return f"""        <div class="entry{' is-tech' if is_tech else ''}"
             data-cat="{'it' if is_tech else 'other'}" data-q="{haystack}" data-key="{key}">
          <a class="entry-link" href="{link}" target="_blank" rel="noopener">
            <span class="entry-body">
              <span class="entry-title">{title}</span>
              <span class="entry-meta">{meta}</span>
            </span>
            <span class="entry-side">{stamp}<span class="entry-src">{source}</span>{day}</span>
            <span class="entry-go">Read&nbsp;&rarr;</span>
          </a>
          <span class="entry-acts">
            <button class="act act-fav" type="button" data-act="fav" aria-pressed="false"
                    aria-label="Save to favourites" title="Save to favourites">
              <svg width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M8 1.6l1.9 3.9 4.3.6-3.1 3 .7 4.3L8 11.4l-3.8 2 .7-4.3-3.1-3 4.3-.6z"
                      fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
              </svg>
            </button>
            <button class="act act-bin" type="button" data-act="bin"
                    aria-label="Move to trash" title="Move to trash">
              <svg class="i-bin" width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M2.8 4.3h10.4M6.2 4.3V2.9h3.6v1.4M4.2 4.3l.6 8.4h6.4l.6-8.4M6.6 6.6v4M9.4 6.6v4"
                      fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>
              </svg>
              <svg class="i-undo" width="15" height="15" viewBox="0 0 16 16" aria-hidden="true">
                <path d="M3 8a5 5 0 1 1 1.5 3.6" fill="none" stroke="currentColor"
                      stroke-width="1.4" stroke-linecap="round"/>
                <path d="M2.6 4.6V8h3.4" fill="none" stroke="currentColor"
                      stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          </span>
        </div>"""


def _archive_groups_html(jobs):
    """Back issues: one collapsible bundle per day, newest first."""
    by_day = {}
    for job in jobs:
        by_day.setdefault(_seen_date(job), []).append(job)

    if not by_day:
        return ('<p class="void">The archive is empty &mdash; it fills up from '
                'the first run onward, and nothing is ever removed.</p>')

    chunks = []
    for i, day in enumerate(sorted(by_day, reverse=True)):
        entries = "\n".join(_entry_html(j, show_day=False)
                            for j in _sorted_for_display(by_day[day]))
        count = len(by_day[day])
        chunks.append(f"""      <details class="group"{' open' if i == 0 else ''}>
        <summary>
          <span class="g-mark"></span>
          <span class="g-date">{_pretty_date(day)}</span>
          <span class="g-count">{count} posting{'' if count == 1 else 's'}</span>
        </summary>
        <div class="entries">
{entries}
        </div>
      </details>""")
    return "\n".join(chunks)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>The Job Digest &mdash; Trinidad &amp; Tobago</title>
<meta name="description" content="A daily broadsheet of every job posted in Trinidad &amp; Tobago, archived in full.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,400..900;1,6..96,400..700&family=Newsreader:ital,opsz,wght@0,6..72,300..700;1,6..72,300..600&family=Courier+Prime:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
<style>
  :root {
    --paper:      #efe8d8;
    --paper-warm: #e7dfcb;
    --paper-deep: #ddd3ba;
    --ink:        #17140f;
    --ink-soft:   #554c3c;
    --ink-faint:  #8a7f6b;
    --rule:       #bfb298;
    --rule-soft:  #d3c8b1;
    --red:        #a71622;
    --gold:       #9a6f13;
  }

  * { box-sizing: border-box; }

  html { -webkit-text-size-adjust: 100%; }

  body {
    margin: 0;
    background-color: var(--paper);
    background-image:
      radial-gradient(ellipse 120% 60% at 50% -10%, rgba(255,252,242,.85), transparent 60%),
      radial-gradient(ellipse 90% 50% at 100% 100%, rgba(167,22,34,.05), transparent 65%);
    color: var(--ink);
    font-family: 'Newsreader', Georgia, serif;
    font-size: 17px;
    line-height: 1.5;
    padding: 0 18px 90px;
  }

  /* Newsprint tooth. Fixed overlay so it never scrolls with the type. */
  body::after {
    content: "";
    position: fixed;
    inset: 0;
    pointer-events: none;
    z-index: 9;
    opacity: .5;
    mix-blend-mode: multiply;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='4' stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter><rect width='180' height='180' filter='url(%23n)' opacity='.42'/></svg>");
  }

  .sheet { max-width: 860px; margin: 0 auto; position: relative; z-index: 1; }

  ::selection { background: var(--red); color: var(--paper); }

  a { color: inherit; }

  /* ---------------------------------------------------------- masthead */

  .masthead { padding-top: 34px; }

  .ears {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 12px;
    font-family: 'Courier Prime', monospace;
    font-size: 10.5px;
    letter-spacing: .22em;
    text-transform: uppercase;
    color: var(--ink-faint);
    padding-bottom: 8px;
  }
  .ears .price { color: var(--red); }

  .hairline { height: 1px; background: var(--rule); }
  .thickline { height: 5px; background: var(--ink); margin-top: 3px; }

  h1 {
    font-family: 'Bodoni Moda', 'Bodoni 72', Didot, serif;
    font-weight: 900;
    font-size: clamp(46px, 13.2vw, 128px);
    line-height: .84;
    letter-spacing: -.025em;
    margin: 20px 0 0;
    text-align: center;
    text-transform: uppercase;
  }
  h1 .the {
    display: block;
    font-size: .2em;
    font-weight: 400;
    font-style: italic;
    letter-spacing: .42em;
    text-transform: uppercase;
    margin-bottom: .55em;
    margin-left: .42em;
    color: var(--ink-soft);
  }
  h1 em {
    font-style: italic;
    font-weight: 400;
    color: var(--red);
  }

  .dateline {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 6px 22px;
    font-family: 'Courier Prime', monospace;
    font-size: 11px;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--ink-soft);
    padding: 14px 0 12px;
  }
  .dateline b { font-weight: 700; color: var(--ink); }

  /* ------------------------------------------------------------ lede */

  .lede {
    display: grid;
    grid-template-columns: 1fr;
    gap: 18px 34px;
    padding: 26px 0 30px;
    border-bottom: 1px solid var(--rule);
  }
  @media (min-width: 720px) { .lede { grid-template-columns: 1.35fr 1fr; } }

  .lede p {
    margin: 0;
    font-size: 18.5px;
    color: var(--ink-soft);
  }
  .lede p::first-letter {
    float: left;
    font-family: 'Bodoni Moda', Didot, serif;
    font-size: 3.35em;
    line-height: .78;
    font-weight: 700;
    color: var(--red);
    padding: .06em .1em 0 0;
  }

  .tally {
    display: flex;
    gap: 26px;
    align-items: flex-start;
    border-left: 1px solid var(--rule);
    padding-left: 26px;
  }
  @media (max-width: 719px) {
    .tally { border-left: 0; padding-left: 0; border-top: 1px solid var(--rule-soft); padding-top: 18px; }
  }
  .tally div { line-height: 1; }
  .tally .n {
    font-family: 'Bodoni Moda', Didot, serif;
    font-size: 44px;
    font-weight: 700;
    display: block;
  }
  .tally .n.hot { color: var(--red); }
  .tally .l {
    font-family: 'Courier Prime', monospace;
    font-size: 10px;
    letter-spacing: .18em;
    text-transform: uppercase;
    color: var(--ink-faint);
    display: block;
    margin-top: 9px;
  }

  /* ---------------------------------------------------------- controls */

  .controls {
    position: sticky;
    top: 0;
    z-index: 5;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px 16px;
    padding: 13px 0 12px;
    margin-bottom: 4px;
    background: linear-gradient(var(--paper) 78%, rgba(239,232,216,0));
    border-bottom: 1px solid var(--rule);
  }

  .search {
    flex: 1 1 230px;
    display: flex;
    align-items: center;
    gap: 9px;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 4px;
  }
  .search svg { flex-shrink: 0; }
  .search input {
    flex: 1;
    min-width: 0;
    border: 0;
    background: transparent;
    font-family: 'Courier Prime', monospace;
    font-size: 13px;
    letter-spacing: .06em;
    color: var(--ink);
    padding: 2px 0;
  }
  .search input::placeholder { color: var(--ink-faint); letter-spacing: .14em; text-transform: uppercase; font-size: 11px; }
  .search input:focus { outline: none; }
  .search:focus-within { border-bottom-color: var(--red); }

  .chips { display: flex; gap: 0; }
  .chip {
    font-family: 'Courier Prime', monospace;
    font-size: 10.5px;
    letter-spacing: .16em;
    text-transform: uppercase;
    background: transparent;
    color: var(--ink-soft);
    border: 1px solid var(--rule);
    margin-left: -1px;
    padding: 7px 13px;
    cursor: pointer;
    transition: background .15s ease, color .15s ease;
  }
  .chip:first-child { margin-left: 0; }
  .chip:hover { background: var(--paper-deep); }
  .chip[aria-pressed="true"] {
    background: var(--ink);
    color: var(--paper);
    border-color: var(--ink);
  }

  .chip .n { font-variant-numeric: tabular-nums; opacity: .75; }
  .chip.view { margin-left: 8px; }
  .chip.view:first-of-type { margin-left: 14px; }
  .chip.view[aria-pressed="true"] { background: var(--gold); border-color: var(--gold); color: var(--paper); }
  #view-bin[aria-pressed="true"] { background: var(--ink-soft); border-color: var(--ink-soft); }

  .refresh {
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    color: var(--ink);
  }
  .refresh svg { display: block; }
  .refresh[disabled] { cursor: progress; color: var(--ink-soft); }
  .refresh[disabled] svg { animation: spin .9s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Raised by the poller below when a later edition lands on the server
     while this tab is sitting open. */
  .stop-press {
    position: fixed;
    left: 50%;
    bottom: 22px;
    transform: translate(-50%, 140%);
    z-index: 40;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 11px 16px;
    background: var(--ink);
    color: var(--paper);
    border: 1px solid var(--ink);
    box-shadow: 0 6px 22px rgba(23, 20, 15, .35);
    font-family: 'Courier Prime', monospace;
    font-size: 12px;
    letter-spacing: .04em;
    transition: transform .35s ease;
  }
  .stop-press[hidden] { display: none; }
  .stop-press.up { transform: translate(-50%, 0); }
  .stop-press b { letter-spacing: .12em; text-transform: uppercase; }
  .stop-press button {
    font: inherit;
    letter-spacing: .1em;
    text-transform: uppercase;
    padding: 5px 12px;
    cursor: pointer;
    background: var(--gold);
    color: var(--paper);
    border: 0;
  }
  .stop-press .dismiss {
    background: none;
    color: var(--paper);
    opacity: .6;
    padding: 5px 2px;
  }
  @media (max-width: 520px) {
    .stop-press { left: 12px; right: 12px; bottom: 12px; transform: translateY(140%); }
    .stop-press.up { transform: translateY(0); }
  }

  /* ---------------------------------------------------------- sections */

  section { padding-top: 40px; }

  .sec-head {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin: 0 0 4px;
  }
  .sec-head h2 {
    font-family: 'Bodoni Moda', Didot, serif;
    font-size: clamp(22px, 4vw, 31px);
    font-weight: 700;
    letter-spacing: -.01em;
    margin: 0;
    white-space: nowrap;
  }
  .sec-head .fill { flex: 1; height: 1px; background: var(--ink); opacity: .28; }
  .sec-head .n {
    font-family: 'Courier Prime', monospace;
    font-size: 11px;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--ink-faint);
    white-space: nowrap;
  }
  .sec-sub {
    font-style: italic;
    font-size: 15px;
    color: var(--ink-faint);
    margin: 0 0 16px;
  }

  /* ----------------------------------------------------------- entries */

  .entries { counter-reset: post; }

  /* The row is a container, not the link itself: the star and bin buttons
     have to sit outside the anchor rather than nested inside it. */
  .entry {
    position: relative;
    display: flex;
    align-items: center;
    border-bottom: 1px solid var(--rule-soft);
    transition: background .18s ease, padding-left .18s ease;
  }
  .entry-link {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: baseline;
    gap: 16px;
    padding: 15px 6px 15px 46px;
    text-decoration: none;
    color: inherit;
    transition: padding-left .18s ease;
  }
  .entry::before {
    counter-increment: post;
    content: counter(post, decimal-leading-zero);
    position: absolute;
    left: 8px;
    top: 17px;
    font-family: 'Courier Prime', monospace;
    font-size: 11px;
    color: var(--ink-faint);
    transition: color .18s ease;
  }
  .entry:hover, .entry:focus-within {
    background: var(--paper-warm);
    outline: none;
  }
  .entry:hover .entry-link, .entry:focus-within .entry-link { padding-left: 52px; }
  .entry-link:focus-visible { outline: none; }
  .entry:focus-within { box-shadow: inset 3px 0 0 var(--red); }
  .entry:hover::before, .entry:focus-within::before { color: var(--red); }

  .entry-body { flex: 1; min-width: 0; }
  .entry-title {
    display: block;
    font-size: 18px;
    font-weight: 600;
    line-height: 1.25;
    background-image: linear-gradient(var(--red), var(--red));
    background-repeat: no-repeat;
    background-size: 0% 1px;
    background-position: 0 1.15em;
    transition: background-size .3s cubic-bezier(.2,.7,.3,1);
  }
  .entry:hover .entry-title { background-size: 100% 1px; }
  .entry-meta {
    display: block;
    font-size: 14.5px;
    color: var(--ink-faint);
    margin-top: 2px;
  }
  .entry-meta em { font-style: italic; color: var(--ink-soft); }

  .entry-side {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-shrink: 0;
    font-family: 'Courier Prime', monospace;
    font-size: 10px;
    letter-spacing: .13em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  .entry-src { display: none; }
  .entry-day { color: var(--ink-soft); }

  .stamp {
    font-family: 'Courier Prime', monospace;
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: .2em;
    text-transform: uppercase;
    color: var(--red);
    border: 1.5px solid var(--red);
    border-radius: 2px;
    padding: 2.5px 6px 2px;
    transform: rotate(-3.5deg);
    opacity: .82;
  }
  .entry:hover .stamp { opacity: 1; }

  /* ------------------------------------------------- star & bin buttons */

  .entry-acts {
    display: flex;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
    padding-right: 8px;
  }
  .act {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    border: 0;
    border-radius: 3px;
    background: transparent;
    color: var(--ink-faint);
    cursor: pointer;
    opacity: .45;
    transition: opacity .15s ease, color .15s ease, background .15s ease;
  }
  .entry:hover .act, .entry:focus-within .act { opacity: 1; }
  .act:hover { background: var(--paper-deep); color: var(--ink); }
  .act:focus-visible { outline: 1.5px solid var(--red); outline-offset: 1px; opacity: 1; }

  /* A starred row keeps its mark lit even when the pointer is elsewhere. */
  .entry.is-fav .act-fav { opacity: 1; color: var(--gold); }
  .entry.is-fav .act-fav svg path { fill: var(--gold); }

  .act-bin .i-undo { display: none; }
  .entry.is-binned .act-bin .i-bin { display: none; }
  .entry.is-binned .act-bin .i-undo { display: block; }
  .entry.is-binned { opacity: .62; }
  .entry.is-binned .entry-title { text-decoration: line-through; }

  /* Touch devices have no hover to reveal the controls, so leave them up. */
  @media (hover: none) {
    .act { opacity: .8; }
  }

  .entry-go {
    flex-shrink: 0;
    font-family: 'Courier Prime', monospace;
    font-size: 10.5px;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--red);
    opacity: 0;
    transform: translateX(-5px);
    transition: opacity .18s ease, transform .18s ease;
    display: none;
  }
  .entry:hover .entry-go, .entry:focus-visible .entry-go { opacity: 1; transform: none; }

  @media (min-width: 640px) {
    .entry-src { display: inline; }
    .entry-go { display: inline; }
  }

  /* Staggered set of the day's fresh postings, like type dropping in. */
  #fresh .entry {
    opacity: 0;
    animation: settle .5s cubic-bezier(.2,.7,.3,1) forwards;
  }
  #fresh .entry:nth-child(1) { animation-delay: .04s; }
  #fresh .entry:nth-child(2) { animation-delay: .09s; }
  #fresh .entry:nth-child(3) { animation-delay: .14s; }
  #fresh .entry:nth-child(4) { animation-delay: .19s; }
  #fresh .entry:nth-child(5) { animation-delay: .24s; }
  #fresh .entry:nth-child(6) { animation-delay: .29s; }
  #fresh .entry:nth-child(n+7) { animation-delay: .33s; }
  @keyframes settle {
    from { opacity: 0; transform: translateY(-7px); }
    to   { opacity: 1; transform: none; }
  }

  /* ----------------------------------------------------------- archive */

  .group { border-bottom: 1px solid var(--rule); }
  .group summary {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 13px 2px;
    cursor: pointer;
    list-style: none;
    font-family: 'Courier Prime', monospace;
    font-size: 11.5px;
    letter-spacing: .14em;
    text-transform: uppercase;
  }
  .group summary::-webkit-details-marker { display: none; }
  .group summary:hover .g-date { color: var(--red); }
  .g-mark {
    width: 9px; height: 9px;
    border: 1.5px solid var(--ink-soft);
    flex-shrink: 0;
    position: relative;
    transition: transform .25s ease, background .2s ease;
  }
  .g-mark::before {
    content: "";
    position: absolute;
    inset: 2px;
    background: var(--red);
    opacity: 0;
    transition: opacity .2s ease;
  }
  .group[open] .g-mark { transform: rotate(45deg); }
  .group[open] .g-mark::before { opacity: 1; }
  .g-date { flex: 1; color: var(--ink); transition: color .18s ease; }
  .g-count { color: var(--ink-faint); }
  .group .entries { padding-bottom: 10px; }
  .group .entry:last-child { border-bottom: 0; }

  /* ------------------------------------------------------------- misc */

  .void {
    font-style: italic;
    color: var(--ink-faint);
    padding: 22px 2px;
    margin: 0;
    border-bottom: 1px solid var(--rule-soft);
  }

  footer {
    margin-top: 62px;
    padding-top: 20px;
    border-top: 5px solid var(--ink);
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    gap: 8px 20px;
    font-family: 'Courier Prime', monospace;
    font-size: 10.5px;
    letter-spacing: .14em;
    text-transform: uppercase;
    color: var(--ink-faint);
  }
  footer a { color: var(--ink-soft); }

  [hidden] { display: none !important; }

  @media (prefers-reduced-motion: reduce) {
    * { animation: none !important; transition: none !important; }
    #fresh .entry { opacity: 1; }
  }
</style>
</head>
<body>
  <div class="sheet">

    <header class="masthead">
      <div class="ears">
        <span>Vol. I &middot; No. __ISSUE__</span>
        <span class="price">Free to read</span>
      </div>
      <div class="hairline"></div>
      <div class="thickline"></div>

      <h1><span class="the">The Trinidad &amp; Tobago</span>Job <em>Digest</em></h1>

      <div class="dateline">
        <span>__DATELINE__</span>
        <span>Filed <b>__TIME__</b></span>
        <span>__TOTAL__ postings on file</span>
      </div>
      <div class="thickline"></div>
      <div class="hairline" style="margin-top:3px"></div>
    </header>

    <div class="lede">
      <p>Every vacancy this bulletin has ever seen is kept below &mdash; today's
      catch is set at the top, and each earlier day is bundled into the archive
      beneath it. Nothing is thrown out. Scraped twice daily from six local job
      portals and the career sites of nine T&amp;T employers, sorted by machine,
      printed without comment.</p>
      <div class="tally">
        <div><span class="n hot">__NEW_COUNT__</span><span class="l">New today</span></div>
        <div><span class="n">__TECH_TOTAL__</span><span class="l">Tech on file</span></div>
        <div><span class="n">__TOTAL__</span><span class="l">All on file</span></div>
      </div>
    </div>

    <div class="controls">
      <label class="search">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
          <circle cx="6" cy="6" r="4.6" stroke="currentColor" stroke-width="1.4"/>
          <path d="M9.5 9.5L13 13" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
        </svg>
        <input id="q" type="search" placeholder="Search the classifieds" autocomplete="off" aria-label="Search postings">
      </label>
      <div class="chips" role="group" aria-label="Filter by field">
        <button class="chip" data-cat="all" aria-pressed="true">All</button>
        <button class="chip" data-cat="it" aria-pressed="false">Tech</button>
        <button class="chip" data-cat="other" aria-pressed="false">Everything else</button>
      </div>
      <div class="chips" role="group" aria-label="Saved and discarded postings">
        <button id="view-fav" class="chip view" type="button" aria-pressed="false">
          Starred <span class="n" id="fav-n">0</span>
        </button>
        <button id="view-bin" class="chip view" type="button" aria-pressed="false">
          Trash <span class="n" id="bin-n">0</span>
        </button>
      </div>
      <button id="refresh" class="chip refresh" type="button"
              title="Fetch the latest edition, bypassing your browser's cache">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true">
          <path d="M10.4 6a4.4 4.4 0 1 1-1.3-3.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
          <path d="M10.6 1v2.4H8.2" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <span class="refresh-label">Refresh</span>
      </button>
    </div>

    <section data-section id="today">
      <div class="sec-head">
        <h2>Posted Today</h2>
        <span class="fill"></span>
        <span class="n"><span class="sec-count">__NEW_COUNT__</span> listed</span>
      </div>
      <p class="sec-sub">Fresh to the archive as of this morning's run.</p>
      <div class="entries" id="fresh">
__NEW_ENTRIES__
      </div>
      <p class="void sec-empty" hidden>Nothing here matches that.</p>
    </section>

    <section data-section id="archive">
      <div class="sec-head">
        <h2>Back Issues</h2>
        <span class="fill"></span>
        <span class="n"><span class="sec-count">__ARCHIVE_COUNT__</span> kept</span>
      </div>
      <p class="sec-sub">Every previous day, in full. Older links may have expired at the source.</p>
__ARCHIVE_GROUPS__
      <p class="void sec-empty" hidden>Nothing in the archive matches that.</p>
    </section>

    <footer>
      <span>Compiled daily at 06:00 AST</span>
      <span>Sources: IslandJobHunt &middot; CaribbeanJobs</span>
      <span>Set in Bodoni &amp; Newsreader</span>
    </footer>

  </div>

  <div class="stop-press" id="stop-press" role="status" hidden>
    <span><b>Stop press</b> &mdash; a later edition has been filed.</span>
    <button type="button" class="read">Read it</button>
    <button type="button" class="dismiss" aria-label="Dismiss">&times;</button>
  </div>

<script>
  (function () {
    var q = document.getElementById('q');
    // Only the category chips - the starred, trash and refresh buttons share
    // the .chip look but must not be treated as category filters.
    var chips = Array.prototype.slice.call(document.querySelectorAll('.chip[data-cat]'));
    var entries = Array.prototype.slice.call(document.querySelectorAll('.entry'));
    var groups = Array.prototype.slice.call(document.querySelectorAll('.group'));
    var sections = Array.prototype.slice.call(document.querySelectorAll('[data-section]'));
    var favBtn = document.getElementById('view-fav');
    var binBtn = document.getElementById('view-bin');
    var favCount = document.getElementById('fav-n');
    var binCount = document.getElementById('bin-n');
    var cat = 'all';
    var view = 'all';   // 'all' hides binned postings, 'fav' and 'bin' show only those

    // Stars and discards live in the reader's own browser - there is no server
    // to keep them on. They are keyed by job link so they survive the page
    // being rebuilt from scratch on every scrape.
    var STORE = 'jobdigest.marks.v1';
    var marks = {fav: {}, bin: {}};

    function loadMarks() {
      try {
        var raw = window.localStorage.getItem(STORE);
        if (!raw) return;
        var saved = JSON.parse(raw);
        marks.fav = saved.fav || {};
        marks.bin = saved.bin || {};
      } catch (e) {
        // Private browsing and disabled storage both throw here. The page is
        // still perfectly usable without memory, so carry on unmarked.
      }
    }

    function saveMarks() {
      try {
        window.localStorage.setItem(STORE, JSON.stringify(marks));
      } catch (e) {}
    }

    function visible(root) {
      return root.querySelectorAll('.entry:not([hidden])').length;
    }

    function apply() {
      var term = q.value.trim().toLowerCase();
      var fav = 0, binned = 0;

      entries.forEach(function (e) {
        var key = e.dataset.key;
        var isFav = !!marks.fav[key];
        var isBin = !!marks.bin[key];
        if (isFav && !isBin) fav++;
        if (isBin) binned++;

        e.classList.toggle('is-fav', isFav);
        e.classList.toggle('is-binned', isBin);

        var favBtnEl = e.querySelector('.act-fav');
        favBtnEl.setAttribute('aria-pressed', String(isFav));
        favBtnEl.title = isFav ? 'Remove from favourites' : 'Save to favourites';
        favBtnEl.setAttribute('aria-label', favBtnEl.title);

        var binBtnEl = e.querySelector('.act-bin');
        binBtnEl.title = isBin ? 'Restore from trash' : 'Move to trash';
        binBtnEl.setAttribute('aria-label', binBtnEl.title);

        var okView = view === 'bin' ? isBin
                   : view === 'fav' ? (isFav && !isBin)
                   : !isBin;
        var okCat = cat === 'all' || e.dataset.cat === cat;
        var okTerm = !term || e.dataset.q.indexOf(term) !== -1;
        e.hidden = !(okView && okCat && okTerm);
      });

      favCount.textContent = fav;
      binCount.textContent = binned;

      groups.forEach(function (g) {
        var n = visible(g);
        g.hidden = n === 0;
        var label = g.querySelector('.g-count');
        if (label) label.textContent = n + (n === 1 ? ' posting' : ' postings');
        if (term && n) g.open = true;
      });

      var filtering = Boolean(term) || cat !== 'all' || view !== 'all';

      // A search or category filter that catches nothing is a different
      // situation from an empty trash, and deserves different wording.
      var narrowed = Boolean(term) || cat !== 'all';
      var emptyNote = narrowed ? 'Nothing here matches that.'
                    : view === 'bin' ? 'The trash is empty.'
                    : view === 'fav' ? 'No favourites yet — tap a star to keep one here.'
                    : 'Nothing here matches that.';

      sections.forEach(function (s) {
        var n = visible(s);
        // Only speak up about "no matches" while a filter is actually on -
        // an empty day already prints its own notice.
        var empty = s.querySelector('.sec-empty');
        if (empty) {
          empty.hidden = !(filtering && n === 0);
          empty.textContent = emptyNote;
        }
        var count = s.querySelector('.sec-count');
        if (count) count.textContent = n;
      });
    }

    q.addEventListener('input', apply);
    chips.forEach(function (c) {
      c.addEventListener('click', function () {
        cat = c.dataset.cat;
        chips.forEach(function (o) { o.setAttribute('aria-pressed', String(o === c)); });
        apply();
      });
    });

    // Starring and binning, delegated so the handlers survive nothing - every
    // entry on the page shares these two.
    document.addEventListener('click', function (ev) {
      var btn = ev.target.closest ? ev.target.closest('.act') : null;
      if (!btn) return;

      var entry = btn.closest('.entry');
      var key = entry.dataset.key;
      var bag = btn.dataset.act === 'fav' ? marks.fav : marks.bin;

      if (bag[key]) {
        delete bag[key];
      } else {
        bag[key] = 1;
        // Binning a posting retires its star: it should not still be counted
        // among the ones worth coming back to.
        if (bag === marks.bin) delete marks.fav[key];
      }

      saveMarks();
      apply();
    });

    function setView(next) {
      view = view === next ? 'all' : next;
      favBtn.setAttribute('aria-pressed', String(view === 'fav'));
      binBtn.setAttribute('aria-pressed', String(view === 'bin'));
      apply();
    }

    favBtn.addEventListener('click', function () { setView('fav'); });
    binBtn.addEventListener('click', function () { setView('bin'); });

    // Paints the stars and hides anything binned on a previous visit.
    loadMarks();
    apply();

    // The page is a plain static file behind a CDN that holds it for minutes,
    // so a normal reload can still hand back yesterday's edition. Reloading
    // with a fresh query string misses both the browser and the edge cache.
    var refresh = document.getElementById('refresh');
    var label = refresh.querySelector('.refresh-label');

    function hardReload() {
      location.replace(location.pathname + '?v=' + new Date().getTime() + location.hash);
    }

    refresh.addEventListener('click', function () {
      refresh.disabled = true;
      label.textContent = 'Refreshing';
      hardReload();
    });

    // Having done its job, the cache-buster is scrubbed from the address bar
    // so the URL people copy or bookmark stays clean.
    if (location.search.indexOf('v=') !== -1 && window.history && history.replaceState) {
      history.replaceState(null, '', location.pathname + location.hash);
    }

    // ------------------------------------------------ watching for a new edition
    // This page is a static file, so a tab left open overnight keeps showing
    // whichever edition it was served. The archive it was built from is stamped
    // below; jobs.json carries the same stamp and is rewritten by every run that
    // finds something, so a stamp newer than this one means a fresh edition is
    // already sitting on the server.
    var BUILT_FROM = '__UPDATED__';
    var POLL_MS = 4 * 60 * 1000;
    var press = document.getElementById('stop-press');
    var pressed = false;

    function newerEditionExists(stamp) {
      return typeof stamp === 'string' && stamp > BUILT_FROM;
    }

    function announce() {
      if (pressed) return;
      pressed = true;
      // Nobody is reading a backgrounded tab, so it can just swap itself out
      // and be current by the time it is looked at again.
      if (document.hidden) { hardReload(); return; }
      press.hidden = false;
      // Painted hidden first so the slide-up transition actually runs.
      requestAnimationFrame(function () { press.classList.add('up'); });
    }

    function poll() {
      if (pressed || !window.fetch) return;
      // Both caches have to be stepped around: the query string defeats the
      // CDN, no-store defeats the browser.
      fetch('jobs.json?t=' + new Date().getTime(), { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (data && newerEditionExists(data.updated)) announce();
        })
        .catch(function () { /* offline or mid-deploy; the next tick retries */ });
    }

    if (press) {
      press.querySelector('.read').addEventListener('click', hardReload);
      press.querySelector('.dismiss').addEventListener('click', function () {
        press.classList.remove('up');
        setTimeout(function () { press.hidden = true; }, 350);
      });

      setInterval(poll, POLL_MS);
      // Timers are throttled hard in background tabs, so coming back to one is
      // the moment most likely to be showing something stale.
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) poll();
      });
      poll();
    }
  })();
</script>
</body>
</html>"""


def generate_html(new_jobs, archive, run_dt, output_path="docs/index.html"):
    """Renders the whole archive - today's additions on top, every previous
    day kept below in collapsible back issues."""
    new_keys = {job_key(j) for j in new_jobs}
    older = [j for j in archive if job_key(j) not in new_keys]

    new_entries = "\n".join(_entry_html(j) for j in _sorted_for_display(new_jobs))
    if not new_entries:
        new_entries = ('        <p class="void">No fresh postings turned up this '
                       'morning. The back issues below are still open for business.</p>')

    tech_total = sum(1 for j in archive if j.get("category") == "it")
    issue_no = len({_seen_date(j) for j in archive if _seen_date(j)}) or 1
    dateline = f"{run_dt.strftime('%A')}, {run_dt.day} {MONTHS[run_dt.month - 1]} {run_dt.year}"

    page = (PAGE_TEMPLATE
            .replace("__NEW_ENTRIES__", new_entries)
            .replace("__ARCHIVE_GROUPS__", _archive_groups_html(older))
            .replace("__NEW_COUNT__", str(len(new_jobs)))
            .replace("__ARCHIVE_COUNT__", str(len(older)))
            .replace("__TECH_TOTAL__", str(tech_total))
            .replace("__TOTAL__", str(len(archive)))
            .replace("__ISSUE__", str(issue_no))
            .replace("__DATELINE__", dateline)
            .replace("__TIME__", run_dt.strftime("%H:%M UTC"))
            # Stamped a moment after save_archive() wrote jobs.json, so this is
            # always >= the stamp in the file this page was built from. The
            # in-page poller compares the two and only speaks up when a *later*
            # run has rewritten jobs.json.
            .replace("__UPDATED__", run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {output_path}")


def _notification_text(new_jobs, archive):
    """Body of the morning push. Always leads with how many jobs were added
    to the archive on this run."""
    if not new_jobs:
        return (f"0 new jobs added this morning.\n"
                f"{len(archive)} postings still on file.")

    tech = [j for j in new_jobs if j.get("category") == "it"]
    lines = [f"{len(new_jobs)} new job{'' if len(new_jobs) == 1 else 's'} added this morning"
             f" ({len(tech)} tech) | {len(archive)} on file", ""]

    for job in _sorted_for_display(new_jobs)[:8]:
        mark = "[tech] " if job.get("category") == "it" else ""
        company = job.get("company") or job.get("source") or ""
        lines.append(f"- {mark}{job['title']}" + (f" - {company}" if company else ""))

    if len(new_jobs) > 8:
        lines.append(f"...and {len(new_jobs) - 8} more")

    return "\n".join(lines)


def main():
    run_dt = datetime.now(timezone.utc)
    run_iso = run_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    archive = load_archive()
    print(f"Archive: {len(archive)} jobs already on file.")

    print("Scraping local job portals...")
    # One scraper failing must not sink the run, so each returns [] on error
    # and the breakdown below makes a silently-empty source obvious in the logs.
    sources = [
        ("IslandJobHunt", scrape_islandjobhunt),
        ("JobsTT", scrape_jobstt),
        ("CaribbeanJobs", scrape_caribbeanjobs),
        ("Pin.tt", scrape_pintt),
        ("EmployTT", scrape_employtt),
        ("EveCaribbean", scrape_evecaribbean),
        # Employer career sites. Unlike the portals above these are not
        # date-filtered - see the comment block above scrape_bamboohr.
        ("Guardian Group", lambda: scrape_oracle_recruiting(
            "fa-eqnr-saasfaprod1.fa.ocs.oraclecloud.com", "CX_1020", "Guardian Group")),
        ("Atlantic LNG", lambda: scrape_oracle_recruiting(
            "emkf.fa.us2.oraclecloud.com", "CX_1001", "Atlantic LNG")),
        ("Maritime Financial", lambda: scrape_bamboohr(
            "maritimefinancial", "Maritime Financial")),
        ("Hadco", lambda: scrape_bamboohr("hadcogroup", "Hadco")),
        ("Digicel", scrape_digicel),
        ("NIDCO", scrape_nidco),
        ("Port Authority", scrape_patt),
        ("National Petroleum", scrape_np),
        ("US Embassy", scrape_us_embassy),
    ]

    all_listings = []
    breakdown = []
    for name, scraper in sources:
        try:
            found = scraper()
        except Exception as e:  # a parser change shouldn't kill the whole digest
            print(f"{name}: scraper raised {type(e).__name__}: {e}")
            found = []
        breakdown.append(f"{name}: {len(found)}")
        all_listings.extend(found)

    print("Breakdown -> " + " | ".join(breakdown))

    if not all_listings:
        print("No listing data could be fetched.")
        # Still refresh the page so the archive stays reachable and dated.
        generate_html([], archive, run_dt)
        notify(f"Scrape came back empty - no listings fetched this run.\n"
               f"{len(archive)} postings still on file.")
        return

    print(f"Found {len(all_listings)} listings within the last 24 hours.")
    raw_combined_text = "\n".join(all_listings)

    print("Processing with Gemini (categorizing + deduping)...")
    digest = summarize_and_filter(raw_combined_text)

    print(f"IT jobs: {len(digest.get('it_jobs', []))} | Other jobs: {len(digest.get('other_jobs', []))}")

    archive, new_jobs = merge_into_archive(archive, digest, run_iso)
    print(f"{len(new_jobs)} of those are new to the archive.")

    save_archive(archive)
    generate_html(new_jobs, archive, run_dt)
    notify(_notification_text(new_jobs, archive),
           title=f"T&T Job Digest - {len(new_jobs)} new")


if __name__ == "__main__":
    main()
