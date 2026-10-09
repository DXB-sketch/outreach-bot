# outreach-bot

Lead finder for Monolith Web Studio. Finds local businesses, audits their websites, scores them 0–100 as
potential clients, and writes outreach drafts for the best ones. **It never sends anything.** You review every
draft and make contact yourself.

```
discover → dedupe → find-websites → audit → score (+ LLM review) → research → draft → report
```

This is the small test version: run it, contact the top 10–15 leads by hand, and see what responds before
automating more.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,browser]"
playwright install chromium   # lets the audit read JavaScript-built sites (Wix, Squarespace) properly
cp .env.example .env          # then fill it in
```

### Connecting the LLM (FreeLLMAPI)

The LLM is optional. Without it, scoring is rules-only, research profiles are facts-only and drafts come
from a plain template. With it:

1. Run your FreeLLMAPI router (it serves `http://localhost:3001/v1` by default).
2. Copy the unified key from the router dashboard's **Keys** page into `.env` as `LLM_API_KEY`.
   If the router runs on a different machine, set `LLM_BASE_URL` to its address.
3. Run `outreach llm-test`. It checks the key, lists the models the router can serve right now, makes
   one call on each tier, and runs a sample review of your top lead.

Two tiers are used. Both default to FreeLLMAPI's own routing, which picks a model and falls back between
providers by itself:

| Tier | Default | Used for |
|---|---|---|
| fast | `auto:fast` | lead reviews (short, many) |
| strong | `auto:smart` | research profiles and outreach drafts |

To pin specific models, set comma-separated fallback lists in `LLM_FAST_MODELS` / `LLM_STRONG_MODELS`,
using model ids from `outreach llm-test`. `outreach usage` shows tokens used this month by task and by the
model that actually served each call.

Rough token use: about 2k tokens per lead review, 10–15k per research profile and 2k per draft. At 10
researched leads a day that's around 5M tokens a month, a small share of a 170M budget.

## Usage

```bash
outreach run --area "Wamuran QLD" --radius-km 25        # everything, end to end
```

Or one stage at a time:

```bash
outreach discover --area "Caboolture QLD" --radius-km 15 --source osm
outreach discover --area "Caboolture QLD" --source places --query plumber --query dentist
outreach import-csv my-leads.csv --lat -27.04 --lon 152.87
outreach dedupe           # merge duplicates already in the database
outreach find-websites    # look for websites the lead sources didn't list
outreach audit --limit 100
outreach score            # --no-llm to skip LLM reviews
outreach research --top 10           # client profiles for the best leads
outreach research --id 42 --refresh  # (re)research one business
outreach draft --top 10
outreach report           # data/reports/YYYY-MM-DD.md and .csv

outreach list --min-score 60
outreach show 42
outreach mark 42 contacted --note "Emailed 9 Oct, follow up Thu"
```

Each stage only processes new work, so `run` is safe to repeat. To redo everything for businesses already
processed (for example after updating the tool), use `outreach run --skip-discover --refresh`. Use `mark` to record outcomes
(`good | bad | contacted | won | skip`). Leads marked `bad`, `skip` or `won` drop out of the shortlist. The
`good`/`bad` marks are what you'll use to tune the scoring weights later.

Output (all under `data/`, which is git-ignored because it holds business contact details):

- `outreach.db`: SQLite database with every business, audit, score, draft and LLM token count
- `research/NNNNN-name.md`: client profile per researched lead, covering services, areas, selling points,
  verified customer quotes, website problems, pitch angles, mockup headline and call to action, logo,
  brand colours, fonts, photos and unknowns
- `drafts/NNNNN-name.md`: per lead, with the reasons for its score, audit issues, an email draft and a
  phone/walk-in script
- `reports/YYYY-MM-DD.md` / `.csv`: the ranked shortlist and near misses

## Lead sources

| Source | Cost | Notes |
|---|---|---|
| OpenStreetMap (Overpass) | Free | Patchy in rural areas, and few phone numbers or reviews |
| Google Places API (New) | Paid beyond a free allowance | Much better coverage, plus review counts. Check pricing first |
| CSV import | Free | Directories, networking events, referrals, anything you collect by hand |

## How websites are checked

Lead sources often don't list a website, especially OpenStreetMap, so "not in the data" never counts as
"no website". `find-websites` searches, in order:

1. the business's email domain
2. Google Places (if `GOOGLE_PLACES_API_KEY` is set)
3. Brave Search (if `BRAVE_API_KEY` is set)
4. common domain guesses (`bobsplumbing.com.au`, `bobs-plumbing.com.au`, ...)

A found site is only accepted if the page shows the business's name or phone number. Only a business that
was searched for and not found gets the full "no website" score. The drafts say "I couldn't find a
website", never "you don't have one".

The audit then tries the listed address, the site root, with and without `www`, and HTTPS then HTTP,
retrying errors once. It reports one of these results:

| Result | Meaning | Need points |
|---|---|---|
| ok | homepage loaded and analysed (re-read in a real browser if it looks empty) | per issue found |
| blocked / robots | site refused automated checks or asked not to be crawled | 8, so check it yourself |
| dns_failed | the domain no longer exists | 35 |
| parked | domain shows a for-sale, expired or placeholder page | 35 |
| broken | every address returned 404/5xx | 35 |

## Who is excluded

Not realistic clients, so they're dropped at discovery and disqualified if already stored:

- hotels, motels, resorts, hostels
- lawyers, solicitors, conveyancers
- hospitals, aged care
- schools, universities, councils, government, police, churches
- supermarkets, banks, fuel, pharmacies, car dealers
- chains and franchises, and closed businesses

Names are checked too ("Royal Hotel", "Smith Lawyers"). Private businesses like driving or dance schools
are kept. Add your own with `EXCLUDE_KEYWORDS` in `.env`, or edit `EXCLUDED` in `outreach/score.py`.

## Research

`outreach research` runs on the top shortlisted leads, plus anything you've marked `good`. It reads the
homepage and up to four key pages (about, services, contact, reviews/gallery) and collects:

- the logo, photos, brand colours and fonts
- phone numbers and email addresses
- schema.org business data
- the text of each page

The strong model turns that into a client profile, which then feeds the drafts and, next, the mockups.
Customer quotes are kept only if they appear word for word on the business's own site, so nothing invented
can end up in a pitch or mockup. A lead is re-researched automatically when its audit changes.

## How scoring works

`outreach/score.py` holds every rule and weight. The score has three parts:

- **Need (0–45):** no website, site down, not mobile-friendly, no HTTPS, old copyright year, outdated tech,
  DIY builder, slow, missing SEO basics.
- **Value (0–35):** how much the category usually spends (trades, health, professional services score
  highest), review count and rating, opening hours, phone, address.
- **Fit (0–20):** distance from you, and whether there's a public email or phone.

Chains, franchises, closed businesses and categories that rarely buy (supermarkets, banks, fuel, government)
are disqualified. If an LLM is configured, leads within 10 points of the threshold get a short review that
can adjust their score by up to ±10, with the reason recorded. Each lead is only reviewed once.

The recommended channel is a walk-in for nearby businesses with no website, email when a public address
exists, otherwise phone.

## Rules this tool follows

- **Nothing is sent automatically.** Drafts are files for you to edit and send.
- **Spam Act 2003:** only email addresses a business publishes itself, about their business. Every draft
  identifies you and includes an opt-out line. Honour opt-outs with `outreach mark <id> skip`.
- **Websites:** respects `robots.txt` (as `MonolithOutreachBot`), fetches only the homepage of each
  business, and waits 1 second between sites. It sends a normal browser User-Agent, because many small
  hosting firewalls reject unknown bots outright, and identifies you via the HTTP `From` header
  (`SENDER_EMAIL`).
- **Platforms:** doesn't scrape Google Maps, Facebook or freelancing sites, whose terms forbid it. Use the
  Places API or CSV import instead.
- **Honesty:** drafts are written as a one-person studio and must not invent clients, results or awards.

## Running it daily on your home server

Cron example (07:00 every day, then read the report with your morning coffee):

```cron
0 7 * * * cd /path/to/outreach-bot && .venv/bin/outreach run --area "Wamuran QLD" --radius-km 25 >> data/run.log 2>&1
```

## Tests

```bash
pytest
```

## Not built yet (next steps once the test shows results)

- Mockup generator: 3–4 Claude Code-designed templates filled from the research profile, built only for leads you
  mark `good`
- Google reviews in research (via Places)
- Daily digest by email or Telegram
- Reddit monitoring for "looking for a web developer" posts
- PageSpeed Insights for real mobile performance scores
