import os
import re
import json
import time
import html as html_lib
import requests
from google.genai import errors
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}


def _within_24h(relative_text):
    """Parses IslandJobHunt-style relative time strings like '23min ago',
    '10h ago', '2d ago', '1w ago', '2m ago' (months) and returns True if
    the listing is within the last 24 hours."""
    if not relative_text:
        return False
    text = relative_text.lower().strip()

    m = re.search(r"(\d+)\s*min", text)
    if m:
        return True  # minutes ago is always within 24h

    m = re.search(r"(\d+)\s*h(our)?s?\b", text)
    if m:
        return int(m.group(1)) <= 24

    m = re.search(r"(\d+)\s*d(ay)?s?\b", text)
    if m:
        return int(m.group(1)) < 1  # "Xd ago" where X >= 1 is already >24h

    if "today" in text:
        return True

    # weeks/months ago are always outside 24h
    return False


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

            if not _within_24h(time_str):
                continue

            full_link = "https://islandjobhunt.com" + href
            listings.append(
                f"Source: IslandJobHunt | Listing: {title} | Details: {block_text} | URL: {full_link}"
            )

        print(f"IslandJobHunt: {len(matches)} total listings found, {len(listings)} within last 24h.")
        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping IslandJobHunt: {e}")
        return []


def scrape_jobstt():
    """JobsTT.com currently sits behind a JavaScript/bot-detection challenge
    (its homepage returns 'You are being redirected... Javascript is
    required' to plain HTTP clients). requests + BeautifulSoup cannot get
    past this - it would need a headless browser like Playwright. Skipping
    for now rather than silently returning garbage."""
    print("JobsTT: skipped (site requires JS / blocks non-browser requests).")
    return []


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
        listings = []

        # Job links look like /Some-Job-Title-Job-233666.aspx
        title_links = soup.find_all("a", href=re.compile(r"-Job-\d+\.aspx"))

        cutoff = datetime.now() - timedelta(hours=24)

        for link in title_links:
            title = link.get_text(strip=True)
            if not title:
                continue

            # The "Updated DD/MM/YYYY" text and company name live in a
            # nearby sibling block; walk up a few levels and grab all text.
            container = link
            for _ in range(4):
                if container.parent is None:
                    break
                container = container.parent

            block_text = " | ".join(container.stripped_strings)

            date_match = re.search(r"Updated (\d{2}/\d{2}/\d{4})", block_text)
            if not date_match:
                continue

            try:
                updated_date = datetime.strptime(date_match.group(1), "%d/%m/%Y")
            except ValueError:
                continue

            if updated_date < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
                continue

            full_link = link["href"]
            if not full_link.startswith("http"):
                full_link = "https://www.caribbeanjobs.com" + full_link

            listings.append(
                f"Source: CaribbeanJobs | Listing: {title} | Details: {block_text[:250]} | URL: {full_link}"
            )

        return listings[:40]
    except requests.RequestException as e:
        print(f"Error scraping CaribbeanJobs: {e}")
        return []


def summarize_and_filter(raw_data_string, max_retries=3):
    """Uses Gemini to dedup listings and split them into IT vs. other,
    returned as structured JSON (not markdown) so the HTML page can render
    it reliably. Date/location filtering already happened in Python."""
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
    You are an automated career assistant. Below is raw scraped job data,
    already filtered to Trinidad & Tobago listings posted in the last 24
    hours from these portals:
    1. IslandJobHunt.com
    2. CaribbeanJobs.com

    YOUR TASK:
    1. Remove duplicate listings (same title + company appearing more than once).
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


def _row_html(job, is_tech):
    title = html_lib.escape(job.get("title", "Untitled"))
    company = html_lib.escape(job.get("company", "") or "")
    location = html_lib.escape(job.get("location", "") or "")
    source = html_lib.escape(job.get("source", "") or "")
    link = html_lib.escape(job.get("link", "") or "#", quote=True)
    sub = " · ".join(p for p in (company, location) if p)
    tag_class = "tag-tech" if is_tech else "tag-gen"
    tag_label = "TECH" if is_tech else "ROLE"

    return f"""
      <a class="row" href="{link}" target="_blank" rel="noopener">
        <span class="tag {tag_class}">{tag_label}</span>
        <span class="row-main">
          <span class="row-title">{title}</span>
          <span class="row-sub">{sub if sub else "&nbsp;"}</span>
        </span>
        <span class="row-source">{source}</span>
        <span class="row-arrow">&rarr;</span>
      </a>"""


def generate_html(digest, output_path="docs/index.html"):
    it_jobs = digest.get("it_jobs", [])
    other_jobs = digest.get("other_jobs", [])
    timestamp = datetime.now().strftime("%d %b %Y &middot; %H:%M")

    it_rows = "\n".join(_row_html(j, True) for j in it_jobs) or \
        '<p class="empty">No new tech jobs posted in the last 24 hours.</p>'
    other_rows = "\n".join(_row_html(j, False) for j in other_jobs) or \
        '<p class="empty">No other listings posted in the last 24 hours.</p>'

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>T&amp;T Job Digest</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Archivo+Black&family=Space+Mono:wght@400;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0b0b0d;
    --panel: #141417;
    --row: #1a1b1f;
    --row-hover: #212227;
    --text: #f2f1ec;
    --muted: #8b8d94;
    --red: #c8102e;
    --amber: #f2a900;
    --border: #2a2b30;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', sans-serif;
    padding: 32px 16px 64px;
  }}
  .wrap {{ max-width: 760px; margin: 0 auto; }}

  header {{
    border-bottom: 3px solid var(--red);
    padding-bottom: 20px;
    margin-bottom: 28px;
    display: flex;
    justify-content: space-between;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 8px;
  }}
  h1 {{
    font-family: 'Archivo Black', sans-serif;
    font-size: clamp(24px, 5vw, 34px);
    letter-spacing: 0.5px;
    margin: 0;
    line-height: 1.1;
  }}
  h1 span {{ color: var(--amber); }}
  .timestamp {{
    font-family: 'Space Mono', monospace;
    font-size: 13px;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .dot {{
    width: 8px; height: 8px; border-radius: 50%;
    background: var(--red);
    animation: pulse 2s infinite;
  }}
  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.35; }}
  }}

  .panel {{ margin-bottom: 32px; }}
  .panel-title {{
    font-family: 'Space Mono', monospace;
    font-size: 13px;
    letter-spacing: 2px;
    text-transform: uppercase;
    color: var(--muted);
    margin: 0 0 10px 2px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  .panel-title.tech {{ color: var(--amber); }}
  .board {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }}

  .row {{
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 14px 16px;
    text-decoration: none;
    color: var(--text);
    border-bottom: 1px solid var(--border);
    background: var(--row);
    transition: background 0.15s ease;
    opacity: 0;
    animation: flap 0.4s ease forwards;
  }}
  .row:last-child {{ border-bottom: none; }}
  .row:hover {{ background: var(--row-hover); }}
  .row:nth-child(1) {{ animation-delay: 0.02s; }}
  .row:nth-child(2) {{ animation-delay: 0.06s; }}
  .row:nth-child(3) {{ animation-delay: 0.10s; }}
  .row:nth-child(4) {{ animation-delay: 0.14s; }}
  .row:nth-child(5) {{ animation-delay: 0.18s; }}
  .row:nth-child(n+6) {{ animation-delay: 0.20s; }}
  @keyframes flap {{
    from {{ opacity: 0; transform: translateY(-6px); }}
    to {{ opacity: 1; transform: translateY(0); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .row {{ animation: none; opacity: 1; }}
    .dot {{ animation: none; }}
  }}

  .tag {{
    font-family: 'Space Mono', monospace;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 4px 7px;
    border-radius: 4px;
    flex-shrink: 0;
  }}
  .tag-tech {{ background: rgba(242,169,0,0.15); color: var(--amber); border: 1px solid rgba(242,169,0,0.4); }}
  .tag-gen {{ background: rgba(255,255,255,0.06); color: var(--muted); border: 1px solid var(--border); }}

  .row-main {{ flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }}
  .row-title {{
    font-family: 'Space Mono', monospace;
    font-size: 14px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .row-sub {{ font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .row-source {{ font-size: 11px; color: var(--muted); flex-shrink: 0; display: none; }}
  .row-arrow {{ color: var(--muted); flex-shrink: 0; }}

  @media (min-width: 520px) {{
    .row-source {{ display: block; }}
  }}

  .empty {{
    padding: 20px 16px;
    color: var(--muted);
    font-family: 'Space Mono', monospace;
    font-size: 13px;
    margin: 0;
  }}

  footer {{
    margin-top: 36px;
    text-align: center;
    font-family: 'Space Mono', monospace;
    font-size: 11px;
    color: var(--muted);
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1>T&amp;T JOB<span>DIGEST</span></h1>
      <div class="timestamp"><span class="dot"></span>UPDATED {timestamp}</div>
    </header>

    <div class="panel">
      <p class="panel-title tech">&#9889; Tech &amp; IT positions ({len(it_jobs)})</p>
      <div class="board">
        {it_rows}
      </div>
    </div>

    <div class="panel">
      <p class="panel-title">All other positions ({len(other_jobs)})</p>
      <div class="board">
        {other_rows}
      </div>
    </div>

    <footer>Auto-generated daily from IslandJobHunt &amp; CaribbeanJobs &middot; Trinidad &amp; Tobago, last 24h</footer>
  </div>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {output_path}")


def main():
    print("Scraping local job portals...")
    island_listings = scrape_islandjobhunt()
    jobstt_listings = scrape_jobstt()
    caribbean_listings = scrape_caribbeanjobs()

    print(f"Breakdown -> IslandJobHunt: {len(island_listings)} | JobsTT: {len(jobstt_listings)} | CaribbeanJobs: {len(caribbean_listings)}")

    all_listings = island_listings + jobstt_listings + caribbean_listings

    if not all_listings:
        print("No listing data could be fetched.")
        return

    print(f"Found {len(all_listings)} listings within the last 24 hours.")
    raw_combined_text = "\n".join(all_listings)

    print("Processing with Gemini (categorizing + deduping)...")
    digest = summarize_and_filter(raw_combined_text)

    print(f"IT jobs: {len(digest.get('it_jobs', []))} | Other jobs: {len(digest.get('other_jobs', []))}")

    generate_html(digest)


if __name__ == "__main__":
    main()