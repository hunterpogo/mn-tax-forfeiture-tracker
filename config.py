from pathlib import Path

# ---- Network Settings ----
REQUEST_TIMEOUT = 30
RATE_LIMIT_DELAY = 2.0
MAX_RETRIES = 2
USER_AGENT = "MN-Tax-Forfeiture-Tracker/1.0 (personal research)"

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

TIME_PATTERN = r"\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm|a\.m\.|p\.m\.)?)\b"

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
    r"(?:held at|auction (?:at|held)|sale (?:at|held)|location:\s*)(?:the\s+)?(.{10,120}?)(?:\.|;|\n|$)",
    r"((?:county\s+)?courthouse[^.\n]{0,60})",
    r"(auditor.s\s+office[^.\n]{0,60})",
    r"(county\s+(?:board\s+room|building|government\s+center)[^.\n]{0,60})",
]

# ---- Location Exclusion Terms (navigation/boilerplate) ----
LOCATION_EXCLUSIONS = [
    "skip to", "main content", "search", "menu", "navigation",
    "click here", "read more", "learn more", "home page",
    "cookie", "privacy", "sign in", "log in",
    "this time", "that time", "no sale", "not available",
    "subscribe", "volunteer", "apply for", "department",
]

# ---- PDF Settings ----
MAX_PDFS_PER_COUNTY = 5
MAX_PDF_PAGES = 20
MIN_PDF_TEXT_LENGTH = 50

# ---- JS-Rendered Page Detection ----
MIN_PAGE_TEXT_LENGTH = 200
