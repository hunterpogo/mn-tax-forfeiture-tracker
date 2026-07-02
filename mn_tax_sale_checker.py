#!/usr/bin/env python3
"""Minnesota Tax-Forfeited Land Auction Checker.

Checks all 87 MN county websites (plus DNR, MnDOT, Wisconsin) for upcoming
tax-forfeited land sales and generates weekly reports in Markdown and CSV.
"""

import csv
import hashlib
import io
import logging
import os
import re
import sys
import tempfile
import time
from collections import namedtuple
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import pandas as pd
import pdfplumber
import requests
from bs4 import BeautifulSoup

from config import (
    DATE_PATTERNS,
    LOCATION_EXCLUSIONS,
    LOCATION_PLACE_TOKENS,
    LOCATION_TRIGGERS,
    MAX_PDF_PAGES,
    MAX_PDFS_PER_COUNTY,
    MIN_PAGE_TEXT_LENGTH,
    MIN_PDF_TEXT_LENGTH,
    NO_SALE_PATTERNS,
    PROJECT_ROOT,
    RATE_LIMIT_DELAY,
    MAX_RETRIES,
    REPORTS_DIR,
    LOGS_DIR,
    TEMP_DIR,
    REQUEST_HEADERS,
    REQUEST_TIMEOUT,
    SALE_KEYWORDS,
    SALE_TYPE_INDICATORS,
    SOURCES_CSV,
    TIME_PATTERN,
    USER_AGENT,
)

# The SSL fallback path deliberately retries with verify=False; keep the log
# readable instead of printing one InsecureRequestWarning per retry.
requests.packages.urllib3.disable_warnings(
    requests.packages.urllib3.exceptions.InsecureRequestWarning
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# `raw` carries the original HTML so PDF-link discovery doesn't need a second request.
FetchResult = namedtuple("FetchResult", ["content", "content_type", "status_code", "error", "raw"])


@dataclass
class SaleRecord:
    county: str
    sale_date: Optional[date] = None
    sale_date_raw: str = ""
    sale_time: Optional[str] = None
    location: Optional[str] = None
    sale_type: str = "unknown"
    description: str = ""
    source_url: str = ""
    source_type: str = ""
    online_url: Optional[str] = None
    deadlines: Optional[str] = None
    dedup_hash: str = ""


@dataclass
class CheckLogEntry:
    county: str
    url: str
    check_time: str = ""
    http_status: Optional[int] = None
    result: str = ""
    keywords_found: str = ""
    error_detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(run_date: date) -> logging.Logger:
    logger = logging.getLogger("mn_tax_checker")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    logger.addHandler(console)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOGS_DIR / f"checker_{run_date.isoformat()}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    logger.addHandler(fh)

    return logger


log = logging.getLogger("mn_tax_checker")

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _response_to_result(resp: requests.Response, url: str, error: Optional[str]) -> FetchResult:
    """Convert an HTTP response into extracted text (HTML or PDF)."""
    content_type = resp.headers.get("Content-Type", "").lower()

    if "pdf" in content_type or url.lower().endswith(".pdf"):
        text = extract_pdf_from_bytes(resp.content)
        return FetchResult(text, "pdf", resp.status_code, error, None)

    encoding = resp.apparent_encoding or "utf-8"
    try:
        html = resp.content.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        html = resp.content.decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return FetchResult(text, "html", resp.status_code, error, html)


def fetch_page(url: str) -> FetchResult:
    """Fetch a URL and return its text content. Handles HTML and PDF."""
    last_error = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT,
                                allow_redirects=True)
            resp.raise_for_status()
            return _response_to_result(resp, url, None)

        except requests.exceptions.Timeout:
            last_error = "timeout"
            log.debug("Timeout on %s (attempt %d)", url, attempt + 1)
        except requests.exceptions.SSLError as e:
            try:
                resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT,
                                    allow_redirects=True, verify=False)
                resp.raise_for_status()
                return _response_to_result(resp, url, "ssl_warning")
            except Exception:
                last_error = f"ssl_error: {e}"
                break
        except requests.exceptions.ConnectionError as e:
            last_error = f"connection_error: {e}"
            log.debug("Connection error on %s (attempt %d)", url, attempt + 1)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            last_error = f"http_{status}"
            return FetchResult(None, None, status, last_error, None)
        except Exception as e:
            last_error = f"error: {e}"
            break

        if attempt < MAX_RETRIES:
            time.sleep(2 ** (attempt + 1))

    return FetchResult(None, None, None, last_error, None)


def extract_pdf_from_bytes(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = pdf.pages[:MAX_PDF_PAGES]
            texts = []
            for page in pages:
                t = page.extract_text()
                if t:
                    texts.append(t)
            return "\n".join(texts)
    except Exception as e:
        log.debug("PDF parse error: %s", e)
        return ""


def find_pdf_links(html_text: str, base_url: str) -> List[str]:
    """Find PDF links in HTML content."""
    soup = BeautifulSoup(html_text, "lxml")
    pdf_links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if href.lower().endswith(".pdf"):
            full_url = urljoin(base_url, href)
            if full_url not in pdf_links:
                pdf_links.append(full_url)
    return pdf_links[:MAX_PDFS_PER_COUNTY]


# ---------------------------------------------------------------------------
# Parsing / extraction
# ---------------------------------------------------------------------------

def extract_dates(text: str) -> List[Tuple[Optional[date], str]]:
    """Extract future dates from text, returning (parsed_date, raw_string) pairs.

    Only today-or-later dates qualify: a "sale" whose only date is in the past
    is stale content, not an upcoming sale. An 18-month horizon guards against
    typos (e.g. 2062) and copyright years matched out of context.
    """
    results = []
    seen_raw = set()
    today = date.today()
    horizon = today + timedelta(days=550)

    for pattern in DATE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = match.group(1)
            if raw in seen_raw:
                continue
            seen_raw.add(raw)
            parsed = try_parse_date(raw)
            if parsed and today <= parsed <= horizon:
                results.append((parsed, raw))

    results.sort(key=lambda x: x[0] or today)
    return results


def try_parse_date(raw: str) -> Optional[date]:
    """Try multiple date formats to parse a raw date string."""
    formats = [
        "%m/%d/%Y", "%m/%d/%y",
        "%m-%d-%Y",
        "%B %d, %Y", "%B %d %Y",
        "%b. %d, %Y", "%b. %d %Y",
        "%b %d, %Y", "%b %d %Y",
        "%Y-%m-%d",
    ]
    raw_clean = raw.strip().replace("  ", " ")
    for fmt in formats:
        try:
            return datetime.strptime(raw_clean, fmt).date()
        except ValueError:
            continue
    return None


def _first_standalone_time(segment: str) -> Optional[str]:
    """First time in segment that is not part of an hours range like 8:00am-4:30pm."""
    for m in re.finditer(TIME_PATTERN, segment, re.IGNORECASE):
        tail = segment[m.end():m.end() + 12]
        if re.match(r"\s*(?:[-–—]|to\b)\s*\d{1,2}:\d{2}", tail, re.IGNORECASE):
            continue  # start of a range
        head = segment[max(0, m.start() - 12):m.start()]
        if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm|a\.m\.|p\.m\.)?\s*(?:[-–—]|to)\s*$", head, re.IGNORECASE):
            continue  # end of a range
        return " ".join(m.group(1).split())
    return None


def extract_time(text: str, near: Optional[str] = None) -> Optional[str]:
    """Extract a sale time from text.

    When `near` (a raw date string) is given, only a time close to that date
    counts — an unanchored search pairs page-render timestamps and
    business-hours boilerplate with unrelated dates. Text after the date is
    preferred ("on June 5, 2026 at 10:00 am" is the dominant phrasing).
    """
    if near:
        idx = text.find(near)
        if idx == -1:
            return None
        after = text[idx + len(near):idx + len(near) + 150]
        before = text[max(0, idx - 150):idx]
        return _first_standalone_time(after) or _first_standalone_time(before)
    return _first_standalone_time(text)


def extract_location(text: str) -> Optional[str]:
    """Extract a location from text using trigger phrases."""
    for pattern in LOCATION_TRIGGERS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            loc = (m.group(1) if m.lastindex else m.group(0)).strip()
            loc = re.sub(r"\s+", " ", loc)
            if len(loc) < 8:
                continue
            loc_lower = loc.lower()
            if any(excl in loc_lower for excl in LOCATION_EXCLUSIONS):
                continue
            if not any(tok in f" {loc_lower} " for tok in LOCATION_PLACE_TOKENS):
                continue
            if len(loc) > 120:
                cut = loc[:120]
                space = cut.rfind(" ")
                loc = cut[:space] if space > 80 else cut
            return loc
    return None


def classify_sale_type(text: str) -> str:
    """Classify the sale type based on keyword presence."""
    text_lower = text.lower()
    scores = {}
    for sale_type, keywords in SALE_TYPE_INDICATORS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[sale_type] = scores.get(sale_type, 0) + 1

    if not scores:
        return "unknown"
    return max(scores, key=scores.get)


def compute_dedup_hash(county: str, sale_date: Optional[date], location: Optional[str]) -> str:
    key = f"{county.lower().strip()}|{sale_date.isoformat() if sale_date else 'none'}|{(location or '').lower().strip()}"
    return hashlib.md5(key.encode()).hexdigest()


def extract_context_windows(text: str, keyword: str, window: int = 500) -> List[str]:
    """Get text snippets surrounding each keyword match."""
    windows = []
    text_lower = text.lower()
    kw_lower = keyword.lower()
    start = 0
    while True:
        idx = text_lower.find(kw_lower, start)
        if idx == -1:
            break
        begin = max(0, idx - window)
        end = min(len(text), idx + len(keyword) + window)
        windows.append(text[begin:end])
        start = idx + len(keyword)
    return windows


def is_stale_source(url: str) -> Optional[int]:
    """Return the year if the URL is an archived notice from a prior year.

    Newspaper archives and dated notice pages (e.g. /2023/11/notice-of-...)
    keep matching sale keywords forever; any date they yield comes from
    sidebar/boilerplate, not the notice itself.
    """
    m = re.search(r"/(20\d{2})/", urlparse(url).path)
    if m and int(m.group(1)) < date.today().year:
        return int(m.group(1))
    return None


def search_for_sales(text: str, county: str, url: str, source_type: str) -> List[SaleRecord]:
    """Search page text for tax-forfeited sale announcements."""
    if not text:
        return []

    text_lower = text.lower()
    matched_keywords = [kw for kw in SALE_KEYWORDS if kw.lower() in text_lower]

    if not matched_keywords:
        return []

    stale_year = is_stale_source(url)
    if stale_year:
        log.info("  %s: source is an archived %d notice, skipping (%s)", county, stale_year, url)
        return []

    all_windows = []
    for kw in matched_keywords:
        all_windows.extend(extract_context_windows(text, kw))

    if not all_windows:
        all_windows = [text[:2000]]

    combined_context = "\n".join(all_windows)
    page_says_no_sale = any(
        re.search(p, combined_context, re.IGNORECASE) for p in NO_SALE_PATTERNS
    )
    dates_found = extract_dates(combined_context)
    location_found = extract_location(combined_context)
    sale_type = classify_sale_type(combined_context)

    online_url = None
    for platform in ["k-bid", "kbid", "proxibid", "govdeals", "publicsurplus"]:
        if platform in text_lower:
            url_match = re.search(
                r'https?://[^\s<>"\']+(?:' + re.escape(platform.replace("-", "")) + r')[^\s<>"\']*',
                text, re.IGNORECASE
            )
            if url_match:
                online_url = url_match.group(0)
                break

    best_snippet = ""
    for kw in matched_keywords:
        idx = combined_context.lower().find(kw.lower())
        if idx != -1:
            start = max(0, idx - 50)
            best_snippet = combined_context[start:start + 300].replace("\n", " ").strip()
            break
    description_snippet = best_snippet or combined_context[:300].replace("\n", " ").strip()

    records = []
    if dates_found:
        # Cap runaway extraction: a page yielding many dates is usually
        # matching meeting calendars, not listing that many distinct sales.
        for sale_date, raw_date in dates_found[:5]:
            rec = SaleRecord(
                county=county,
                sale_date=sale_date,
                sale_date_raw=raw_date,
                sale_time=extract_time(combined_context, near=raw_date),
                location=location_found,
                sale_type=sale_type,
                description=description_snippet,
                source_url=url,
                source_type=source_type,
                online_url=online_url,
            )
            rec.dedup_hash = compute_dedup_hash(county, sale_date, location_found)
            records.append(rec)
    elif page_says_no_sale:
        # Page has sale keywords but explicitly says nothing is for sale.
        log.info("  %s: page states no properties currently for sale", county)
    else:
        rec = SaleRecord(
            county=county,
            sale_date=None,
            sale_date_raw="",
            sale_time=extract_time(combined_context),
            location=location_found,
            sale_type=sale_type,
            description=description_snippet,
            source_url=url,
            source_type=source_type,
            online_url=online_url,
            deadlines="Date not extracted - manual review needed",
        )
        # Dateless records dedup per county+type: two URLs describing the same
        # program with different boilerplate would otherwise both survive.
        rec.dedup_hash = compute_dedup_hash(county, None, sale_type)
        records.append(rec)

    return records


# ---------------------------------------------------------------------------
# Per-URL check
# ---------------------------------------------------------------------------

def check_single_url(url: str, county: str, source_type: str) -> Tuple[List[SaleRecord], CheckLogEntry]:
    """Check a single URL for tax-forfeited sale information."""
    entry = CheckLogEntry(
        county=county,
        url=url,
        check_time=datetime.now().isoformat(timespec="seconds"),
    )

    result = fetch_page(url)

    if result.error and result.content is None:
        entry.http_status = result.status_code
        entry.result = f"error_{result.error.split(':')[0] if ':' in (result.error or '') else result.error}"
        entry.error_detail = result.error
        log.warning("  %s: %s", county, result.error)
        return [], entry

    entry.http_status = result.status_code

    if result.content_type == "html" and len(result.content or "") < MIN_PAGE_TEXT_LENGTH:
        entry.result = "needs_manual_review"
        entry.error_detail = "Page content too short - may require JavaScript"
        log.info("  %s: Page too short, flagged for manual review", county)
        return [], entry

    sales = search_for_sales(result.content, county, url, source_type)

    if result.content_type == "html" and not sales and result.raw:
        try:
            pdf_links = find_pdf_links(result.raw, url)
        except Exception as e:
            log.debug("  %s: PDF link discovery failed: %s", county, e)
            pdf_links = []
        if pdf_links:
            log.debug("  %s: Found %d PDF links, checking...", county, len(pdf_links))
            for pdf_url in pdf_links:
                time.sleep(RATE_LIMIT_DELAY / 2)
                pdf_result = fetch_page(pdf_url)
                if pdf_result.content:
                    pdf_sales = search_for_sales(pdf_result.content, county, pdf_url, "pdf_page")
                    sales.extend(pdf_sales)

    if sales:
        matched_kw = [kw for kw in SALE_KEYWORDS if kw.lower() in (result.content or "").lower()]
        entry.result = "sale_found"
        entry.keywords_found = "; ".join(matched_kw[:5])
        log.info("  %s: Found %d potential sale(s)", county, len(sales))
    else:
        entry.result = "no_sale_found"
        log.info("  %s: No upcoming sales found", county)

    if result.error == "ssl_warning":
        entry.error_detail = "SSL certificate warning - fetched with verify=False"

    return sales, entry


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

SOURCE_TYPE_PRIORITY = {"county_webpage": 0, "pdf_page": 1, "public_notice_page": 2,
                        "auction_platform": 3, "board_packet": 4, "other": 5}


def deduplicate_sales(sales: List[SaleRecord]) -> List[SaleRecord]:
    """Remove duplicate sale records, keeping the most complete one."""
    groups = {}
    for rec in sales:
        groups.setdefault(rec.dedup_hash, []).append(rec)

    deduped = []
    for _hash, recs in groups.items():
        recs.sort(key=lambda r: (
            SOURCE_TYPE_PRIORITY.get(r.source_type, 99),
            -sum(1 for v in [r.sale_date, r.sale_time, r.location, r.online_url] if v is not None),
        ))
        deduped.append(recs[0])

    deduped.sort(key=lambda r: (r.sale_date or date.max, r.county))
    return deduped


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def format_sale_type(st: str) -> str:
    return st.replace("_", " ").title()


def md_cell(value) -> str:
    """Make a value safe for a markdown table cell: no pipes, no newlines."""
    return " ".join(str(value if value is not None else "").split()).replace("|", "\\|")


# Human-readable explanation + suggested action per error class, used in the
# manual-review table so a reader knows what to do without decoding e.g. "http_403".
ERROR_GUIDANCE = {
    "error_http_404": ("Page not found (404)", "URL is dead or moved - find the county's current tax-forfeiture page and update the sources CSV"),
    "error_http_403": ("Access denied (403)", "Site is blocking automated checks - open the URL in a browser; if it works there, the county's firewall blocks this checker"),
    "error_http_500": ("Server error (500)", "County server problem - usually temporary, check again next week"),
    "error_http_429": ("Rate limited (429)", "Too many requests - usually temporary"),
    "error_timeout": ("Request timed out", "Server too slow - open the URL manually; if it never loads, find a replacement source"),
    "error_connection_error": ("Could not connect", "DNS or network failure - the domain may have changed; search for the county's new website"),
    "error_ssl_error": ("SSL certificate problem", "Open in a browser to verify the site; certificate may be expired"),
    "needs_manual_review": ("Page loads but has no readable text", "Content is likely rendered by JavaScript - check the URL in a browser"),
}


def error_guidance(result_code: str, detail: Optional[str]) -> Tuple[str, str]:
    if result_code in ERROR_GUIDANCE:
        return ERROR_GUIDANCE[result_code]
    return (detail or result_code, "Open the source URL manually to investigate")


def generate_markdown_report(
    sales: List[SaleRecord],
    log_entries: List[CheckLogEntry],
    all_counties: List[str],
    run_date: date,
) -> str:
    """Generate the weekly Markdown report."""

    # Past-dated records are dropped up front: a reader opening the report
    # days after generation should never see a finished sale under "Upcoming".
    current = [s for s in sales if s.sale_date is None or s.sale_date >= run_date]

    error_entries = [e for e in log_entries if "error" in e.result or e.result == "needs_manual_review"]
    sale_counties = {s.county for s in current}
    error_counties = {e.county for e in error_entries}
    no_sale_counties = [c for c in all_counties if c not in sale_counties and c not in error_counties]

    lines = []
    lines.append(f"# Minnesota Tax-Forfeited Land Sale Report")
    lines.append(f"**Generated:** {run_date.isoformat()} {datetime.now().strftime('%H:%M')}")
    lines.append(f"**Counties Checked:** {len(all_counties)}")
    lines.append(f"**Sales Found:** {len(current)}")
    lines.append(f"**Errors/Manual Review:** {len(error_entries)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Split into auctions vs OTC sales, each sorted by soonest date first.
    # OTC = over_the_counter only; Auctions = online / in_person / sealed_bid / unknown.
    def _sort_key(rec):
        return (rec.sale_date or date.max, rec.county)
    otc_sales = sorted(
        [s for s in current if s.sale_type == "over_the_counter"],
        key=_sort_key,
    )
    auction_sales = sorted(
        [s for s in current if s.sale_type != "over_the_counter"],
        key=_sort_key,
    )

    def _render_table(records):
        out = []
        out.append("| County / Municipality | Sale Type | Date | Time | Location / Platform | Source |")
        out.append("|---|---|---|---|---|---|")
        for s in records:
            date_str = s.sale_date.strftime("%Y-%m-%d") if s.sale_date else "TBD"
            time_str = s.sale_time or "TBD"
            loc_str = s.location or s.online_url or "See source"
            type_str = format_sale_type(s.sale_type)
            out.append(
                f"| {md_cell(s.county)} | {md_cell(type_str)} | {md_cell(date_str)} "
                f"| {md_cell(time_str)} | {md_cell(loc_str)} | [Link]({s.source_url}) |"
            )
        return out

    def _render_split_section(records, title, blurb):
        """Render a sale bucket as two tables: dated sales first, then undated."""
        dated = [s for s in records if s.sale_date]
        undated = [s for s in records if not s.sale_date]
        out = []
        out.append(f"## {title} ({len(records)})")
        out.append(f"_{blurb}_")
        out.append("")
        if dated:
            out.extend(_render_table(dated))
            out.append("")
        else:
            out.append("None with confirmed dates this week.")
            out.append("")
        if undated:
            out.append(f"**No date extracted ({len(undated)})** — sale activity detected but no "
                       "specific date found; check the source link:")
            out.append("")
            out.extend(_render_table(undated))
            out.append("")
        return out

    def _render_details(records):
        out = []
        for s in records:
            out.append(f"### {s.county}")
            out.append(f"- **Sale type:** {format_sale_type(s.sale_type)}")
            out.append(f"- **Date:** {s.sale_date_raw or 'TBD'}")
            out.append(f"- **Time:** {s.sale_time or 'TBD'}")
            out.append(f"- **Location:** {s.location or 'See source'}")
            if s.online_url:
                out.append(f"- **Online link:** {s.online_url}")
            out.append(f"- **Source:** [{s.source_url}]({s.source_url})")
            if s.deadlines:
                out.append(f"- **Deadlines/Notes:** {s.deadlines}")
            out.append(f"- **Excerpt:** {md_cell(s.description[:200])}...")
            out.append("")
        return out

    lines.extend(_render_split_section(
        auction_sales, "Upcoming Auctions",
        "Online, in-person, and sealed-bid sales — scheduled dates first, soonest at top.",
    ))
    lines.append("---")
    lines.append("")
    lines.extend(_render_split_section(
        otc_sales, "Over-the-Counter (OTC) Sales",
        "Ongoing/repurchase sales available at fixed price — scheduled dates first, soonest at top.",
    ))

    # Details split into the same two buckets
    lines.append("---")
    lines.append("")
    lines.append("## Details")
    lines.append("")
    if auction_sales:
        lines.append("### Auctions")
        lines.append("")
        lines.extend(_render_details(auction_sales))
    if otc_sales:
        lines.append("### OTC Sales")
        lines.append("")
        lines.extend(_render_details(otc_sales))
    if not (auction_sales or otc_sales):
        lines.append("No sale details to display.")
        lines.append("")

    # No sales
    lines.append("---")
    lines.append("")
    lines.append(f"## Counties Checked With No Upcoming Sales ({len(no_sale_counties)} counties)")
    lines.append("")
    if no_sale_counties:
        lines.append(", ".join(no_sale_counties))
    else:
        lines.append("All counties had either a sale found or an error.")
    lines.append("")

    # Manual review
    lines.append("---")
    lines.append("")
    lines.append(f"## Counties Needing Manual Review ({len(error_entries)} entries)")
    lines.append("")
    if error_entries:
        lines.append("| County | URL | Issue | Suggested Action |")
        lines.append("|---|---|---|---|")
        for e in error_entries:
            explanation, action = error_guidance(e.result, e.error_detail)
            lines.append(f"| {md_cell(e.county)} | [Link]({e.url}) | {md_cell(explanation)} | {md_cell(action)} |")
        lines.append("")
    else:
        lines.append("No counties need manual review.")
        lines.append("")

    # Statistics
    lines.append("---")
    lines.append("")
    lines.append("## Check Statistics")
    lines.append("")
    result_counts = {}
    for e in log_entries:
        result_counts[e.result] = result_counts.get(e.result, 0) + 1
    for result_type, count in sorted(result_counts.items()):
        lines.append(f"- {result_type}: {count}")
    lines.append("")

    return "\n".join(lines)


def generate_csv_report(sales: List[SaleRecord], run_date: date) -> None:
    """Write the CSV report of found sales.

    Sorted: auctions first (by soonest date), then OTC sales (by soonest date).
    A `bucket` column distinguishes the two for easy filtering in a spreadsheet.
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"weekly_report_{run_date.isoformat()}.csv"

    def _sort_key(rec):
        return (rec.sale_date or date.max, rec.county)

    current = [s for s in sales if s.sale_date is None or s.sale_date >= run_date]
    auction_sales = sorted(
        [s for s in current if s.sale_type != "over_the_counter"],
        key=_sort_key,
    )
    otc_sales = sorted(
        [s for s in current if s.sale_type == "over_the_counter"],
        key=_sort_key,
    )

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "bucket", "county", "sale_date", "sale_time", "sale_type", "location",
            "online_url", "description", "source_url", "source_type", "deadlines",
        ])
        for bucket_name, group in (("auction", auction_sales), ("otc", otc_sales)):
            for s in group:
                writer.writerow([
                    bucket_name,
                    s.county,
                    s.sale_date.isoformat() if s.sale_date else "",
                    s.sale_time or "",
                    format_sale_type(s.sale_type),
                    s.location or "",
                    s.online_url or "",
                    s.description[:200],
                    s.source_url,
                    s.source_type,
                    s.deadlines or "",
                ])

    log.info("CSV report written to %s", path)


def generate_check_log(log_entries: List[CheckLogEntry], run_date: date) -> None:
    """Write the check log CSV."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = LOGS_DIR / f"check_log_{run_date.isoformat()}.csv"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "county", "url", "check_time", "http_status", "result",
            "keywords_found", "error_detail",
        ])
        for e in log_entries:
            writer.writerow([
                e.county, e.url, e.check_time, e.http_status or "",
                e.result, e.keywords_found, e.error_detail or "",
            ])

    log.info("Check log written to %s", path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    run_date = date.today()
    logger = setup_logging(run_date)
    global log
    log = logger

    log.info("=" * 60)
    log.info("Minnesota Tax-Forfeited Land Sale Checker")
    log.info("Run date: %s", run_date.isoformat())
    log.info("=" * 60)

    # Ensure directories
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Load sources
    if not SOURCES_CSV.exists():
        log.error("Sources CSV not found: %s", SOURCES_CSV)
        sys.exit(1)

    df = pd.read_csv(SOURCES_CSV, dtype={"last_checked_date": str})
    log.info("Loaded %d sources from CSV", len(df))

    all_sales: List[SaleRecord] = []
    all_log_entries: List[CheckLogEntry] = []
    all_counties: List[str] = df["county"].tolist()

    start_time = time.time()

    for idx, row in df.iterrows():
        county = row["county"]
        source_url = row["source_url"]
        source_url_2 = row.get("source_url_2")
        source_type = row.get("source_type", "county_webpage")

        log.info("[%d/%d] Checking %s...", idx + 1, len(df), county)

        # One county's failure must never kill the whole run.
        urls_to_check = [str(source_url).strip()]
        if pd.notna(source_url_2) and str(source_url_2).strip():
            url2 = str(source_url_2).strip()
            if url2 != urls_to_check[0]:
                urls_to_check.append(url2)

        for check_url in urls_to_check:
            try:
                sales, log_entry = check_single_url(check_url, county, source_type)
            except Exception as e:
                log.warning("  %s: unexpected error: %s", county, e)
                sales = []
                log_entry = CheckLogEntry(
                    county=county, url=check_url,
                    check_time=datetime.now().isoformat(timespec="seconds"),
                    result="error_other", error_detail=str(e)[:200],
                )
            all_sales.extend(sales)
            all_log_entries.append(log_entry)
            time.sleep(RATE_LIMIT_DELAY)

        # Update last_checked_date
        df.at[idx, "last_checked_date"] = run_date.isoformat()

    elapsed = time.time() - start_time
    log.info("=" * 60)
    log.info("Checking complete in %.1f minutes", elapsed / 60)

    # Deduplicate
    deduped_sales = deduplicate_sales(all_sales)
    log.info("Found %d unique sales (from %d raw matches)", len(deduped_sales), len(all_sales))

    # Update sources CSV with last_checked_date
    df.to_csv(SOURCES_CSV, index=False)
    log.info("Updated sources CSV with check dates")

    # Generate reports
    md_report = generate_markdown_report(deduped_sales, all_log_entries, all_counties, run_date)
    md_path = REPORTS_DIR / f"weekly_report_{run_date.isoformat()}.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_report)
    log.info("Markdown report written to %s", md_path)

    generate_csv_report(deduped_sales, run_date)
    generate_check_log(all_log_entries, run_date)

    # Cleanup temp
    if TEMP_DIR.exists():
        for f in TEMP_DIR.iterdir():
            try:
                f.unlink()
            except Exception:
                pass
        try:
            TEMP_DIR.rmdir()
        except Exception:
            pass

    # Summary
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  Total sources checked: %d", len(all_log_entries))
    log.info("  Unique sales found: %d", len(deduped_sales))
    result_counts = {}
    for e in all_log_entries:
        result_counts[e.result] = result_counts.get(e.result, 0) + 1
    for k, v in sorted(result_counts.items()):
        log.info("  %s: %d", k, v)
    log.info("  Elapsed time: %.1f minutes", elapsed / 60)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
