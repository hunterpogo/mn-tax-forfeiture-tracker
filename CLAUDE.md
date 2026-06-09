# MN Tax-Forfeiture Tracker — Claude Context

## What this project does

Monitors all 87 Minnesota county websites (plus DNR, MnDOT, and Wisconsin Surplus) for upcoming tax-forfeited land sales. Produces a weekly Markdown + CSV report and emails a summary draft to the recipient list.

## Key files

| File | Purpose |
|---|---|
| `mn_tax_sale_checker.py` | Main scraper — fetches county URLs, extracts sale dates/types/locations, writes reports |
| `config.py` | Keywords, timeouts, paths, sale-type classifiers |
| `mn_county_tax_forfeiture_sources.csv` | Source list (87 rows): county, URLs, source type, confidence |
| `.github/workflows/weekly-check.yml` | GitHub Actions workflow — runs the scraper every Monday 11:00 UTC, commits reports, opens a GitHub Issue |
| `reports/` | Generated Markdown + CSV reports (`weekly_report_YYYY-MM-DD.{md,csv}`) |
| `logs/` | Per-run audit logs (`check_log_YYYY-MM-DD.csv`, `checker_YYYY-MM-DD.log`) |

## How the automation works

```
Every Monday 11:00 UTC
        │
        ▼
GitHub Actions (ubuntu-latest, unrestricted outbound HTTP)
        │  runs mn_tax_sale_checker.py
        │  ~87 URLs × 2s delay ≈ 5–15 min
        ▼
Commits reports/ and logs/ back to main
        │
        ▼
Opens a GitHub Issue with the report summary
        │
        ▼
Claude reads the committed report  ← /weekly-check slash command
        │
        ▼
Gmail draft created → info@wimnre.com
```

## Important: do NOT use WebFetch for county URLs

The Claude Code cloud sandbox blocks outbound HTTP to county government domains (`.co.*.mn.us`, `.mn.gov`, etc.) — all requests return HTTP 403. The Python scraper in GitHub Actions has unrestricted outbound access and works correctly. Always use the GitHub Actions workflow for the actual scraping.

## Running the weekly check

Use the `/weekly-check` slash command. It will:
1. Check for today's report in the repo
2. If not found, trigger the GitHub Actions workflow and wait for it to finish
3. Read the report
4. Create a Gmail draft to info@wimnre.com

## Scheduling

The GitHub Actions workflow runs automatically every Monday at 11:00 UTC (6:00 AM CDT / 5:00 AM CST). After it completes (~15 min), run `/weekly-check` to pick up the report and create the email draft.

For fully-automated weekly delivery without manual Claude involvement, see `README.md` for SMTP-based email alternatives.

## Source list maintenance

Edit `mn_county_tax_forfeiture_sources.csv` to add/update county URLs. Columns:

| Column | Notes |
|---|---|
| `county` | County name (matches GitHub Issue labels) |
| `source_url` | Primary URL to check |
| `source_url_2` | Optional second URL (leave blank if not needed) |
| `source_type` | `county_webpage` / `pdf_page` / `public_notice_page` / `auction_platform` / `other` |
| `notes` | Human notes about the source |
| `last_checked_date` | Auto-updated by the scraper each run |
| `confidence_level` | `high` / `medium` / `low` |

Known sources needing updates:
- **Pope County** — current URL is a Nov 2023 newspaper article; replace with a current county auditor page
- **Rock County** — only homepage found; search for a dedicated tax-forfeiture page
