"""
eprocure.gov.in (Central Public Procurement Portal / GeM-CPPP) scraper.

The portal's public "Latest Active Tenders" listing is plain
server-rendered HTML, sorted newest-first, and requires no login or
CAPTCHA to browse. Only the on-site keyword-*search form* is
CAPTCHA-protected — this module avoids that entirely by paging through
the public listing and filtering locally by keyword instead.

Every result returned contains a real source URL on eprocure.gov.in.
This module never creates sample/fake tender data.
"""

import base64
import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://eprocure.gov.in"
LISTING_URL = f"{BASE_URL}/cppp/latestactivetendersnew/cpppdata"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 30

# Government server — be polite between requests.
REQUEST_DELAY_SECONDS = 1.0

# The portal periodically drops the connection under sustained load
# (observed: "RemoteDisconnected" after ~90 pages in one run). These
# are transient — retry a few times with backoff before giving up on
# a page.
MAX_PAGE_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3.0


def _get_with_retry(session: requests.Session, url: str, params: dict):
    """
    GET with a few retries on transient connection errors (dropped
    connections, resets, timeouts). Raises the last error if every
    attempt fails.
    """

    last_error = None

    for attempt in range(1, MAX_PAGE_RETRIES + 1):
        try:
            return session.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            last_error = error
            if attempt < MAX_PAGE_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)

    raise last_error


def _page_request_params(page: int) -> dict:
    """
    Build the request params for a given listing page.

    IMPORTANT: eprocure.gov.in does NOT support a plain ?page=N query
    param for page > 1 — sending one directly gets silently redirected
    by the server to a completely different (effectively arbitrary,
    observed to land near the last/oldest page) page instead of the
    one requested. Confirmed by direct testing: requesting ?page=2
    returned page ~2985 of 2985 (a 2014-dated tender), not page 2.

    The site's own "next page" links instead use a wrapped, base64-
    encoded copy of the full target URL as a ?url= param, e.g. page 3
    is linked as:
        ?url=base64("https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata?page=3")
    This has been verified to return the correct page. Page 1 (the
    default/landing page) works fine with a plain request and needs
    no wrapping.
    """

    if page <= 1:
        return {}

    target_url = f"{LISTING_URL}?page={page}"
    encoded = base64.b64encode(target_url.encode()).decode()
    return {"url": encoded}


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    text_lower = text.lower()
    return any(keyword.lower() in text_lower for keyword in keywords)


def _parse_listing_page(html: str) -> list[dict]:
    """
    Parse one page of the "Latest Active Tenders" table.

    Table columns on eprocure.gov.in are:
    Sl.No | e-Published Date | Bid Submission Closing Date |
    Tender Opening Date | Title/Ref.No./Tender Id | Organisation Name |
    Corrigendum
    """

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")

    if table is None:
        return []

    rows = []

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")

        # Need at least Sl.No, 3 dates, title link, organisation.
        if len(cells) < 6:
            continue

        link = cells[4].find("a")

        if not link or not link.get("href"):
            continue

        title_text = link.get_text(strip=True)
        detail_url = link["href"]

        if detail_url.startswith("/"):
            detail_url = BASE_URL + detail_url

        rows.append(
            {
                "title": title_text,
                "published_date": cells[1].get_text(strip=True),
                "closing_date": cells[2].get_text(strip=True),
                "opening_date": cells[3].get_text(strip=True),
                "organisation": cells[5].get_text(strip=True),
                "detail_url": detail_url,
            }
        )

    return rows


def _fetch_detail_content(session: requests.Session, detail_url: str) -> str:
    """
    Best-effort fetch of the full tender detail page, using the SAME
    session that loaded the listing page — the detail links on this
    portal are session-scoped and return "Invalid Url" when followed
    from a fresh/different session.

    Returns an empty string on any failure so the caller can fall back
    to listing-row text only, instead of losing the tender entirely.
    """

    try:
        response = _get_with_retry(session, detail_url, params=None)
    except requests.RequestException:
        return ""

    if response.status_code != 200:
        return ""

    text = response.text

    if "invalid url" in text.lower():
        return ""

    soup = BeautifulSoup(text, "html.parser")
    main = soup.find("main") or soup.find("body") or soup
    content = main.get_text(separator=" ", strip=True)

    return content[:14000]


def fetch_tender_sources(
    category: str,
    keywords: list[str],
    max_results: int = 10,
    max_pages: int = 15,
) -> list[dict]:
    """
    Scan the most recent pages of eprocure.gov.in's public tender
    listing (newest first) and return documents for tenders whose
    title matches one of the given keywords.

    Returns:
        [
            {
                "title": "...",
                "url": "https://eprocure.gov.in/cppp/tendersfullview/...",
                "content": "listing info + (if reachable) full detail page text",
            }
        ]

    Raises:
        RuntimeError only if page 1 itself is unreachable/errors — that
        means the portal is down or blocking us entirely, which is
        worth surfacing as a real failure rather than "no results".

        For any later page (2+), a connection error or bad status
        after retries is treated as "the portal cut us off partway
        through" rather than a hard failure: scanning stops there, but
        every match already found on the pages scanned so far is still
        returned rather than discarded. A genuine "scanned everything
        available, no matching tenders" case also returns an empty
        list, and is not an error either way.
    """

    session = requests.Session()
    documents = []

    for page in range(1, max_pages + 1):
        if len(documents) >= max_results:
            break

        try:
            response = _get_with_retry(
                session, LISTING_URL, _page_request_params(page)
            )
        except requests.RequestException as error:
            if page == 1:
                raise RuntimeError(
                    f"eprocure.gov.in connection error "
                    f"(page {page}, category {category}): {error}"
                ) from error
            # Later page: keep whatever we already found instead of
            # losing it to one flaky page near the end of a long scan.
            break

        if response.status_code != 200:
            if page == 1:
                raise RuntimeError(
                    f"eprocure.gov.in returned HTTP {response.status_code} "
                    f"on page {page} (category {category})"
                )
            break

        rows = _parse_listing_page(response.text)

        if not rows:
            # No table found / ran past the last page — stop scanning.
            break

        for row in rows:
            if not _matches_keywords(row["title"], keywords):
                continue

            time.sleep(REQUEST_DELAY_SECONDS)
            detail_content = _fetch_detail_content(session, row["detail_url"])

            content = (
                f"Tender Title: {row['title']}\n"
                f"Organisation: {row['organisation']}\n"
                f"Published Date: {row['published_date']}\n"
                f"Bid Submission Closing Date: {row['closing_date']}\n"
                f"Tender Opening Date: {row['opening_date']}\n"
            )

            if detail_content:
                content += f"\nFull Tender Details:\n{detail_content}"

            documents.append(
                {
                    "title": row["title"],
                    "url": row["detail_url"],
                    "content": content,
                    "organisation": row["organisation"],
                }
            )

            if len(documents) >= max_results:
                break

        time.sleep(REQUEST_DELAY_SECONDS)

    return documents