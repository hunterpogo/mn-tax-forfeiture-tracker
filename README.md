# Minnesota Tax-Forfeited Land Auction Tracker

Checks 87 Minnesota county websites (plus DNR, MnDOT, and Wisconsin Surplus) for upcoming tax-forfeited land sales. Generates weekly reports in Markdown and CSV.

## Quick Start

```bash
# Install dependencies
pip3 install -r requirements.txt

# Run the checker
python3 mn_tax_sale_checker.py

# View the report
open reports/weekly_report_$(date +%Y-%m-%d).md
```

## Files

| File | Description |
|---|---|
| `mn_county_tax_forfeiture_sources.csv` | Source database — all 90 tracked URLs with metadata |
| `mn_tax_sale_checker.py` | Main checker script |
| `config.py` | All configurable settings (timeouts, keywords, patterns) |
| `reports/weekly_report_YYYY-MM-DD.md` | Markdown report with sales found, details, and review list |
| `reports/weekly_report_YYYY-MM-DD.csv` | CSV export of found sales |
| `logs/check_log_YYYY-MM-DD.csv` | Audit log — every URL checked, status, keywords found |
| `logs/checker_YYYY-MM-DD.log` | Full debug log |

## How It Works

1. Reads the county source CSV
2. Fetches each URL (HTML pages and PDFs)
3. Searches page text for tax-forfeited sale keywords
4. Extracts dates, times, locations, and sale types
5. If an HTML page links to PDFs, follows those links too (up to 5 per county)
6. Deduplicates sales that appear on multiple pages
7. Generates Markdown and CSV reports plus a check log

## Configuration

Edit `config.py` to adjust:

- **RATE_LIMIT_DELAY** — seconds between requests (default: 2)
- **REQUEST_TIMEOUT** — per-request timeout in seconds (default: 30)
- **SALE_KEYWORDS** — terms that trigger a "sale found" match
- **DATE_PATTERNS** — regex patterns for extracting dates
- **MAX_PDFS_PER_COUNTY** — max PDF links to follow per county (default: 5)

## Adding or Updating Counties

Edit `mn_county_tax_forfeiture_sources.csv` directly:

- Add a new row with the county name, URL, source type, and confidence level
- To update a broken URL, find the county row and change `source_url`
- Set `source_type` to one of: `county_webpage`, `pdf_page`, `auction_platform`, `public_notice_page`, `board_packet`, `other`
- Set `confidence_level` to `high`, `medium`, or `low`

## Scheduling Weekly Runs

### Option A: macOS crontab

```bash
crontab -e
# Add this line (runs every Monday at 8 AM):
0 8 * * 1 cd "/Users/hunterpogatchnik/Property Tax Sales" && /usr/bin/python3 mn_tax_sale_checker.py >> logs/cron.log 2>&1
```

### Option B: macOS launchd

Create `~/Library/LaunchAgents/com.user.taxsalechecker.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.taxsalechecker</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/hunterpogatchnik/Property Tax Sales/mn_tax_sale_checker.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>8</integer>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/hunterpogatchnik/Property Tax Sales</string>
</dict>
</plist>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/com.user.taxsalechecker.plist
```

## Limitations

- **JavaScript-rendered pages**: Auction platforms like K-Bid, GovDeals, and Proxibid render content with JavaScript. The script uses plain HTTP requests, so these pages may appear empty and get flagged for manual review.
- **Scanned PDFs**: PDFs that are scanned images (no embedded text) cannot be read. These are flagged for manual review.
- **Date extraction**: Regex-based — may miss unusual date formats. When in doubt, the script flags the entry for manual review rather than silently missing it.
- **Not real-time**: Designed for weekly batch runs.

## Troubleshooting

**"Sources CSV not found"** — Run from the project directory, or check that `mn_county_tax_forfeiture_sources.csv` exists.

**Many 404 errors** — County websites change URLs. Update the source CSV with current URLs.

**SSL warnings** — Some county sites have expired or misconfigured SSL certificates. The script retries without verification and logs a warning.

**Empty reports** — If no upcoming sales are found, the report will still be generated with all counties listed under "No Upcoming Sales" or "Manual Review."
