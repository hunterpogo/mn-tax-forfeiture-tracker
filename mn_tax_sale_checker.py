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
    LOCATION_TRIGGERS,
    MAX_PDF_PAGES,
    MAX_PDFS_PER_COUNTY,
    MIN_PAGE_TEXT_LENGTH,
    MIN_PDF_TEXT_LENGTH,
    PROJECT_ROOT,
    RATE_LIMIT_DELAY,
    MAX_RETRIES,
    REPORTS_DIR,
    LOGS_DIR,
    TEMP_DIR,
    REQUEST_TIMEOUT,
    SALE_KEYWORDS,
    SALE_TYPE_INDICATORS,
    SOURCES_CSV,
    TIME_PATTERN,
    USER_AGENT,
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

FetchResult = namedtuple("FetchResult", ["content", "content_type", "status_code", "error"])


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

def fetch_page(url: str) -> FetchResult:
    """Fetch a URL and return its text content. Handles HTML and PDF."""
    headers = {"User-Agent": USER_AGENT}
    last_error = None

    for attempt in range(1 + MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "").lower()

            if "pdf" in content_type or url.lower().endswith(".pdf"):
                text = extract_pdf_from_bytes(resp.content)
                return FetchResult(text, "pdf", resp.status_code, None)

            encoding = resp.apparent_encoding or "utf-8"
            try:
                html = resp.content.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = resp.content.decode("utf-8", errors="replace")

            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            return FetchResult(text, "html", resp.status_code, None)

        except requests.exceptions.Timeout:
            last_error = "timeout"
            log.debug("Timeout on %s (attempt %d)", url, attempt + 1)
        except requests.exceptions.SSLError as e:
            try:
                resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                                    allow_redirects=True, verify=False)
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "").lower()
                if "pdf" in content_type or url.lower().endswith(".pdf"):
                    text = extract_pdf_from_bytes(resp.content)
                    return FetchResult(text, "pdf", resp.status_code, "ssl_warning")
                encoding = resp.apparent_encoding or "utf-8"
                try:
                    html = resp.content.decode(encoding, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    html = resp.content.decode("utf-8", errors="replace")
                soup = BeautifulSoup(html, "lxml")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                return FetchResult(text, "html", resp.status_code, "ssl_warning")
            except Exception:
                last_error = f"ssl_error: {e}"
                break
        except requests.exceptions.ConnectionError as e:
            last_error = f"connection_error: {e}"
            log.debug("Connection error on %s (attempt %d)", url, attempt + 1)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            last_error = f"http_{status}"
            return FetchResult(None, None, status, last_error)
        except Exception as e:
            last_error = f"error: {e}"
            break

        if attempt < MAX_RETRIES:
            time.sleep(2 ** (attempt + 1))

    return FetchResult(None, None, None, last_error)


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
    """Extract dates from text, returning (parsed_date, raw_string) pairs."""
    results = []
    seen_raw = set()
    today = date.today()
    cutoff_past = today - timedelta(days=7)

    for pattern in DATE_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = match.group(1)
            if raw in seen_raw:
                continue
            seen_raw.add(raw)
            parsed = try_parse_date(raw)
            if parsed and parsed >= cutoff_past:
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


def extract_time(text: str) -> Optional[str]:
    """Extract the first time mention from text."""
    m = re.search(TIME_PATTERN, text, re.IGNORECASE)
    return m.group(1).strip() if m else None


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
            return loc[:120]
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


def search_for_sales(text: str, county: str, url: str, source_type: str) -> List[SaleRecord]:
    """Search page text for tax-forfeited sale announcements."""
    if not text:
        return []

    text_lower = text.lower()
    matched_keywords = [kw for kw in SALE_KEYWORDS if kw.lower() in text_lower]

    if not matched_keywords:
        return []

    all_windows = []
    for kw in matched_keywords:
        all_windows.extend(extract_context_windows(text, kw))

    if not all_windows:
        all_windows = [text[:2000]]

    combined_context = "\n".join(all_windows)
    dates_found = extract_dates(combined_context)
    time_found = extract_time(combined_context)
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
        for sale_date, raw_date in dates_found:
            rec = SaleRecord(
                county=county,
                sale_date=sale_date,
                sale_date_raw=raw_date,
                sale_time=time_found,
                location=location_found,
                sale_type=sale_type,
                description=description_snippet,
                source_url=url,
                source_type=source_type,
                online_url=online_url,
            )
            rec.dedup_hash = compute_dedup_hash(county, sale_date, location_found)
            records.append(rec)
    else:
        rec = SaleRecord(
            county=county,
            sale_date=None,
            sale_date_raw="",
            sale_time=time_found,
            location=location_found,
            sale_type=sale_type,
            description=description_snippet,
            source_url=url,
            source_type=source_type,
            online_url=online_url,
            deadlines="Date not extracted - manual review needed",
        )
        rec.dedup_hash = compute_dedup_hash(county, None, location_found)
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

    if result.content_type == "html" and not sales:
        raw_html = requests.get(url, headers={"User-Agent": USER_AGENT},
                                timeout=REQUEST_TIMEOUT, allow_redirects=True).text
        pdf_links = find_pdf_links(raw_html, url)
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


def generate_markdown_report(
    sales: List[SaleRecord],
    log_entries: List[CheckLogEntry],
    all_counties: List[str],
    run_date: date,
) -> str:
    """Generate the weekly Markdown report."""

    error_entries = [e for e in log_entries if "error" in e.result or e.result == "needs_manual_review"]
    sale_counties = {s.county for s in sales}
    error_counties = {e.county for e in error_entries}
    no_sale_counties = [c for c in all_counties if c not in sale_counties and c not in error_counties]

    lines = []
    lines.append(f"# Minnesota Tax-Forfeited Land Sale Report")
    lines.append(f"**Generated:** {run_date.isoformat()} {datetime.now().strftime('%H:%M')}")
    lines.append(f"**Counties Checked:** {len(all_counties)}")
    lines.append(f"**Sales Found:** {len(sales)}")
    lines.append(f"**Errors/Manual Review:** {len(error_entries)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Upcoming sales table
    lines.append("## Upcoming Sales Found")
    lines.append("")
    if sales:
        lines.append("| County / Municipality | Sale Type | Date | Time | Location / Platform | Source |")
        lines.append("|---|---|---|---|---|---|")
        for s in sales:
            date_str = s.sale_date.strftime("%Y-%m-%d") if s.sale_date else "TBD"
            time_str = s.sale_time or "TBD"
            loc_str = s.location or s.online_url or "See source"
            type_str = format_sale_type(s.sale_type)
            lines.append(f"| {s.county} | {type_str} | {date_str} | {time_str} | {loc_str} | [Link]({s.source_url}) |")
        lines.append("")
    else:
        lines.append("No upcoming sales found this week.")
        lines.append("")

    # Details
    lines.append("---")
    lines.append("")
    lines.append("## Details")
    lines.append("")
    if sales:
        for s in sales:
            lines.append(f"### {s.county}")
            lines.append(f"- **Sale type:** {format_sale_type(s.sale_type)}")
            lines.append(f"- **Date:** {s.sale_date_raw or 'TBD'}")
            lines.append(f"- **Time:** {s.sale_time or 'TBD'}")
            lines.append(f"- **Location:** {s.location or 'See source'}")
            if s.online_url:
                lines.append(f"- **Online link:** {s.online_url}")
            lines.append(f"- **Source:** [{s.source_url}]({s.source_url})")
            if s.deadlines:
                lines.append(f"- **Deadlines/Notes:** {s.deadlines}")
            lines.append(f"- **Excerpt:** {s.description[:200]}...")
            lines.append("")
    else:
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
        lines.append("| County | URL | Issue |")
        lines.append("|---|---|---|")
        for e in error_entries:
            issue = e.error_detail or e.result
            lines.append(f"| {e.county} | [Link]({e.url}) | {issue} |")
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
    """Write the CSV report of found sales."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"weekly_report_{run_date.isoformat()}.csv"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "county", "sale_date", "sale_time", "sale_type", "location",
            "online_url", "description", "source_url", "source_type", "deadlines",
        ])
        for s in sales:
            writer.writerow([
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

        # Check primary URL
        sales, log_entry = check_single_url(source_url, county, source_type)
        all_sales.extend(sales)
        all_log_entries.append(log_entry)
        time.sleep(RATE_LIMIT_DELAY)

        # Check secondary URL if present
        if pd.notna(source_url_2) and str(source_url_2).strip():
            url2 = str(source_url_2).strip()
            if url2 != source_url:
                log.info("  Checking secondary URL...")
                sales2, log_entry2 = check_single_url(url2, county, source_type)
                all_sales.extend(sales2)
                all_log_entries.append(log_entry2)
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
