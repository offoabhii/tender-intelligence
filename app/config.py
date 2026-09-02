"""
Central configuration.

This file is inside app/ so imports work consistently on:
- Windows
- GitHub Actions
- Streamlit Cloud
"""

from datetime import date

TODAY = date.today()
CURRENT_YEAR = TODAY.year

# Employer-approved categories only.
CATEGORIES_ALLOWED = [
    "Charging point operations",
    "Solar",
    "Bus operations (gross cost only)",
    "Bus body building",
]

BUS_OPERATIONS_CATEGORY = "Bus operations (gross cost only)"

# The company profile was not supplied.
# Therefore eligibility must stay NOT SURE unless it can be proven.
COMPANY_PROFILE = """
Company capability information is not available.

Rules:
- Never claim the company is eligible unless the source clearly proves it.
- Never invent turnover, certificates, fleet size, registrations,
  licences, experience, project capacity, or financial data.
- If eligibility cannot be proven, return NOT SURE.
"""

# Source: eprocure.gov.in (Central Public Procurement Portal / GeM-CPPP).
#
# The site's "Latest Active Tenders" listing is plain server-rendered
# HTML, sorted newest-first, and does NOT require login or solving a
# CAPTCHA to browse (only the on-site keyword-search *form* is
# CAPTCHA-gated, so we avoid that entirely and filter locally instead).
#
# Each category below maps to a list of lowercase keywords matched
# against tender titles on that listing. app/scraper.py consumes this
# directly — do not rename these without updating scraper.py too.
CATEGORY_KEYWORDS = {
    "Charging point operations": [
        "ev charging",
        "e-vehicle charging",
        "electric vehicle charging",
        "charging station",
        "charging point",
        "charging infrastructure",
        "ev charging station",
        "battery charging station",
        "charging kiosk",
    ],
    "Solar": [
        "solar",
    ],
    "Bus operations (gross cost only)": [
        "bus operation",
        "gross cost contract",
        "gcc bus",
        "state road transport",
        "srtc",
        "city bus service",
        "bus service operation",
        "hiring of buses",
        "hiring of ac buses",
        "hiring of non-ac buses",
        "operation and maintenance of buses",
        "o&m of buses",
        "plying of buses",
        "bus fleet operation",
        "operation of city buses",
        "electric bus operation",
        "e-bus operation",
    ],
    "Bus body building": [
        "bus body building",
        "bus body fabrication",
        "bus body",
        "body building of bus",
        "fabrication of bus body",
        "manufacture and supply of bus body",
        "bus body manufacturing",
    ],
}

# How many of the most recent listing pages (10 tenders/page, sorted
# newest-first) to scan per category, per run.
#
# The portal currently has roughly 30,000 total active tenders. Niche
# categories (Solar, EV charging) may simply have no *brand new*
# listing within a narrow recent window, so this needs to be large
# enough to give them a realistic chance without making each run take
# unreasonably long. 100 pages = 1,000 most-recent tenders (~3% of all
# active tenders) per category, per run. Raise this further if a
# category still comes up empty across several consecutive runs and
# you want deeper coverage — each +50 pages costs roughly +1 minute of
# runtime per category with no matches.
EPROCURE_MAX_PAGES = 100

# Stop scanning a category early once this many keyword-matching
# tenders have been found for it, to keep runs fast and polite to the
# government server.
EPROCURE_RESULTS_PER_CATEGORY = 10
