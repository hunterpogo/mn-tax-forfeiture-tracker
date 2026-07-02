from pathlib import Path

# ---- Network Settings ----
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 2.0
MAX_RETRIES = 2
# Browser-like UA: several county WAFs 403 obvious bot UAs. Volume stays low
# (one pass/week, rate-limited), so this remains polite scraping.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# Full browser-like header set: Akamai-protected county sites (e.g. Carver)
# 403 requests that carry only a User-Agent.
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="126", "Not/A)Brand";v="8", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}

# ---- Paths ----
PROJECT_ROOT = Path(__file__).parent
SOURCES_CSV = PROJECT_ROOT / "mn_county_tax_forfeiture_sources.csv"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOGS_DIR = PROJECT_ROOT / "logs"
TEMP_DIR = PROJECT_ROOT / "temp"

# ---- Keywords ----
SALE_KEYWORDS = [
    "tax forfeited",
    "tax-forfeited",
    "forfeited land",
    "land sale",
    "public auction",
    "sealed bid",
    "auction sale",
    "county land sale",
    "forfeiture sale",
    "over-the-counter",
    "classification sale",
    "forfeited property",
    "forfeited parcels",
    "tax forfeit",
]

# ---- Date Extraction Patterns ----
DATE_PATTERNS = [
    r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b",
    r"\b(\d{1,2}-\d{1,2}-\d{4})\b",
    r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})\b",
    r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2},?\s+\d{4})\b",
    r"\b(\d{4}-\d{2}-\d{2})\b",
]

# [ \t] only: \s would let a newline into the captured value and break
# markdown table rows. am/pm required so bare clock fragments don't match.
TIME_PATTERN = r"\b(\d{1,2}:\d{2}[ \t]*(?:AM|PM|am|pm|a\.m\.|p\.m\.))"

# ---- Sale Type Keyword Maps ----
SALE_TYPE_INDICATORS = {
    "online": [
        "online auction", "online bidding", "online sale",
        "k-bid", "kbid", "proxibid", "govdeals", "publicsurplus",
        "bid online", "internet auction",
    ],
    "sealed_bid": [
        "sealed bid", "sealed-bid", "written bid",
        "submit bids", "mail bids",
    ],
    "in_person": [
        "in-person", "in person", "public auction",
        "courthouse", "auditor's office", "county board room",
        "auction held at", "oral auction",
    ],
    "over_the_counter": [
        "over-the-counter", "over the counter", "otc sale",
        "appraised value", "immediate sale",
    ],
}

# ---- Location Trigger Phrases ----
LOCATION_TRIGGERS = [
    r"(?:held at|auction (?:at|held)|sale (?:at|held)|location:)\s*(?:the\s+)?([A-Za-z][^.|;\n]{8,100}?)(?:\s*\.|\s*;|\n|$)",
    r"((?:county\s+)?courthouse[^.|\n]{0,60})",
    r"(auditor.s\s+office[^.|\n]{0,60})",
    r"(county\s+(?:board\s+room|building|government\s+center)[^.|\n]{0,60})",
]

# A candidate location must contain at least one of these place-indicating
# tokens; the trigger patterns alone capture sentence fragments like
# "the rate of $3" or "an initial public auction".
LOCATION_PLACE_TOKENS = [
    "courthouse", "court house", "office", "center", "centre", "room",
    "hall", "building", "floor", "chambers", "library", "annex",
    "street", " st ", " st.", "avenue", " ave", "road", " rd", "drive",
    " dr ", " dr.", "boulevard", "blvd", "highway", " hwy", "lane",
    "suite", "campus", "online", "k-bid", "kbid", "govdeals",
    "publicsurplus", "proxibid", "zoom", "virtual",
]

# ---- Location Exclusion Terms (navigation/boilerplate/junk) ----
LOCATION_EXCLUSIONS = [
    "skip to", "main content", "search", "menu", "navigation",
    "click here", "read more", "learn more", "home page",
    "cookie", "privacy", "sign in", "log in",
    "this time", "that time", "no sale", "not available",
    "subscribe", "volunteer", "apply for",
    "pic -", "cropped", "image", "photo", "logo", "screenshot",
    "rate of", "market value", "reduced amount", "did not sell",
    "regular business hours",
]

# ---- Negative Phrases (page explicitly says nothing is for sale) ----
# Suppresses the dateless fallback record; dated records are kept.
NO_SALE_PATTERNS = [
    r"(?:there are\s+)?currently\s+no\s+tax[- ]?forfeit\w*\s+(?:propert|parcel|land)",
    r"no\s+(?:propert\w+|parcels?|lands?)\s+(?:are\s+)?(?:currently\s+)?(?:available|for sale|listed)",
    r"no\s+(?:auction|sale)s?\s+(?:are\s+)?(?:currently\s+)?scheduled",
    r"next\s+sale\s+has\s+not\s+been\s+scheduled",
]

# ---- PDF Settings ----
MAX_PDFS_PER_COUNTY = 5
MAX_PDF_PAGES = 20
MIN_PDF_TEXT_LENGTH = 50

# ---- JS-Rendered Page Detection ----
MIN_PAGE_TEXT_LENGTH = 200
