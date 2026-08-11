# Employer career-site scrapability audit

**All viable sources below are now implemented in `main.py`** and wired into the twice-daily
run. Figures in this document were re-checked against the finished scrapers; where the first
pass was wrong, the corrected number is what appears here.

Tested 2026-08-10 against the list in `agent-work.md`. Every verdict below comes from
actually fetching the page with the project's `HEADERS` user agent and trying to pull
job titles out of the response — not from eyeballing the site. Where a verdict says
"N jobs", those titles were extracted successfully.

Bottom line: **8 sources are worth adding**, 4 of which return clean structured data.
Most of the ~100 names either have no careers page, hide postings behind JavaScript,
or block datacenter IPs outright.

---

## Tier 1 — structured data, add these first

These return parseable job data from a single request. No HTML guesswork.

| Employer | Endpoint | Verified |
|---|---|---|
| **Guardian Group** | Oracle `recruitingCEJobRequisitions`, site `CX_1020` | **29 T&T roles** of 40 regional |
| **Maritime Financial** | `https://maritimefinancial.bamboohr.com/careers/list` | 7 jobs, all T&T |
| **Hadco** | `https://hadcogroup.bamboohr.com/careers/list` | 1 job (Arima) |
| **Digicel** | `https://careers.digicelgroup.com/search/?q=&locationsearch=Trinidad` | 3 T&T jobs (25 unfiltered) |
| **Atlantic LNG** | Oracle `recruitingCEJobRequisitions`, site `CX_1001` | endpoint valid, 0 openings today |

Notes for implementation:

- **Oracle Recruiting (Guardian Group)** returns JSON with `Title` and `PrimaryLocation`
  per requisition — filter on `PrimaryLocation` containing "Trinidad". This is the single
  best new source found: real titles, real locations, no HTML parsing.
- **BambooHR** returns JSON at `/careers/list` with `jobOpeningName` and `location.city`.
  Both T&T tenants are single-country, so no filtering needed. One generic scraper
  parameterised by subdomain covers both, and any future BambooHR employer.
- **Digicel** runs a SuccessFactors site that *is* server-rendered — job links match
  `/job/<slug>/<id>/`. The `locationsearch=Trinidad` parameter works server-side.

## Tier 2 — scrapable HTML, needs modest parsing

| Employer | Page | Verified |
|---|---|---|
| **NIDCO** | `https://www.nidco.co.tt/careers/` | 27 adverts on file, **0 current** — see below |
| **National Petroleum (NP)** | `https://www.np.co.tt/careers/` | 9 adverts (11 links, 2 duplicated) |
| **Port Authority (PATT)** | `https://www.patnt.com/about/vacancies/` | **2** vacancies in HTML |
| **US Embassy Port of Spain** | `https://erajobs.state.gov/dos-era/tto/vacancysearch/searchVacancies.hms` | 1 vacancy, server-rendered |

Notes:

- **NIDCO** posts vacancies as PDF adverts whose link text carries the title. It never
  takes expired ads down — the page still lists 2022 roles — so the scraper reads the
  date out of the filename prefix (`2025-12-03_HR_VAC_...`) and drops anything older
  than 180 days. **Worth knowing: NIDCO's newest advert is dated 2025-12-03, eight
  months old, so the scraper correctly yields nothing today.** It is in the run to catch
  the next real posting; without the date filter it would have injected 27 stale adverts.
- **NP** link text is just "Download" — the title has to come from the filename
  (`NP-Employment-Opp-ICT-Manager-new-date.pdf` → "ICT Manager"). Workable but lossy:
  one advert reduces to "CIA", which is the employer's own shorthand. No dates are
  available on this page, so staleness cannot be checked the way NIDCO's can.
- **PATT** lists vacancies as headings with no per-job link, so each is keyed by the
  page URL plus a slug fragment — sharing one URL would have collapsed them into a
  single archive entry. Its certificate chain is also incomplete (it omits the
  intermediate), so this one host is fetched with verification off; the reasoning is
  recorded in the scraper's docstring.
- **US Embassy**: `tt.usembassy.gov/jobs/` itself has nothing machine-readable; the
  actual vacancies live on the State Department ERA board under the `tto` country path,
  which is server-rendered and already country-scoped.

## Blocked by bot protection — not viable from GitHub Actions

All returned **HTTP 403** (Cloudflare "Access denied") to a normal browser user agent
from a datacenter IP. The scheduled workflow runs on GitHub's IP ranges and will be
blocked the same way. These would need a headless browser plus a residential proxy,
which is disproportionate for this project.

First Citizens Group · ANSA McAL · COLFIRE · Guardian Media (also CNC3) · SM Jaleel

## JavaScript-only — postings never appear in the HTML

The careers page loads, but job data arrives via client-side JS, so `requests` +
BeautifulSoup sees an empty shell. Would need Playwright/Selenium.

- **SuccessFactors portals that don't server-render:** Unit Trust Corporation,
  National Energy, Sagicor, BAT/West Indian Tobacco. (Contrast with Digicel, which does.)
- **Others:** Shell, Trinidad Cement, TATT, SuperPharm, Angostura, Caribbean Airlines,
  Agostini (Zoho Recruit — page is a 1.7 MB JS shell with 39 characters of text),
  British High Commission (the FCDO `fco.tal.net` board is session-driven; the Trinidad
  filter returns nothing without a live session).
- **Atlantic LNG** deserves a footnote: its Oracle Recruiting endpoint *works*
  (`emkf.fa.us2.oraclecloud.com`, site `CX_1001`) but currently returns 0 openings.
  Worth adding later — it's a Tier 1 source whenever they're hiring.

## Reachable, but nothing to scrape

Careers page exists and was parsed successfully; there are simply no postings on it, or
postings are handled off-site: T&TEC, MovieTowne, Touchstone Exploration, Massy Group,
Massy Stores, NFM, TATIL, EXIMBANK, iGovTT, Label House, Carib Brewery, Bryden pi,
Proman, Heritage Petroleum, JMMB, TT Stock Exchange, Beacon Insurance, Bourse Securities,
Peake, AATT, Unicomer.

**Corrections to earlier automated passes** — these looked promising to a keyword
heuristic but are false positives on inspection:

- **NGC** — 118 "job titles" were page prose, not postings. 0 job links.
- **Central Bank** — the 12 PDFs are policy reports (Monetary Policy Report, AML/CFT
  statement), not vacancies.
- **ADB** — the list is agri-business *sectors* ("Hatchery Manager", "Nursery Operator"
  as example careers in agriculture), not open roles.
- **Southern Sales** — "Audi Sales Reps", "Kia Sales Reps" are showroom staff directory
  links, not vacancies.
- **PATT's third vacancy** — "Cargo Accounts Cashier" is an opening-hours line for the
  cashier's counter, not a job. The real count is 2.

## No website or no careers page found

Domain does not resolve, is parked, or has no careers section anywhere on it:
Massy Technologies InfoCom, Paria Fuel Trading, Blue Waters, Prestige Holdings,
Universal Foods, WITCO (own site — see BAT above), TSTT/bmobile, Kiss Baking, WASA,
PLIPDECO, Sacha Cosmetics, K.C. Confectionery, Flavorite, Xtra Foods, Chaguaramas
Development Authority, PPGPL, TTMF, Gulf Insurance, Home Construction, Laughlin & de
Gannes, TruValu, Mario's Pizzeria, Associated Brands, A.S. Bryden, Bermudez, One
Caribbean Media, DeNovo Energy, Nestlé T&T, High Commission of Canada.

## Multinationals — global portals only

RBC, PwC, EY, Deloitte, KPMG, BP, Woodside, Methanex, Nutrien, Yara, Perenco, EOG,
Fujitsu, Scotiabank. Each runs a global careers portal (mostly Workday or Phenom) where
T&T roles are rare and buried behind a country filter. Low yield for the effort; skip
unless one is specifically wanted.

## Embassies

- **US** — viable, via the State Dept ERA board (Tier 2 above).
- **UK** — FCDO uses `fco.tal.net`, session-driven, not scrapable without a browser.
- **Canada** — no scrapable careers page found for the Port of Spain mission; Canadian
  missions post through VidCruiter/GC Jobs, which is not country-addressable by URL.

---

## What was built

Nine new sources in `main.py`, registered alongside the six portals:

- `scrape_bamboohr(subdomain, employer)` — Maritime Financial, Hadco. Any future
  BambooHR employer costs one line.
- `scrape_oracle_recruiting(host, site_number, employer, country)` — Guardian Group,
  Atlantic LNG. Filters on `PrimaryLocationCountry`, since the human-readable location
  field is free text.
- `scrape_digicel`, `scrape_nidco`, `scrape_patt`, `scrape_np`, `scrape_us_embassy`.

**52 live postings** across them at the time of writing — Guardian Group 29, NP 9,
Maritime Financial 7, Digicel 3, PATT 2, Hadco 1, US Embassy 1 — with NIDCO and
Atlantic LNG correctly returning nothing.

Unlike the portals, employer boards are **not** date-filtered: they publish what is
currently open, mostly without posting dates, so every open role is returned and the
archive's link-based dedup decides what counts as new. The practical effect is that the
first run after this change reports the current backlog in one go, and steady state
resumes immediately after.
