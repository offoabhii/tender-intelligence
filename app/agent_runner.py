"""
Real Tender Intelligence Pipeline.

Tavily -> Gemini -> strict validation -> JSON -> Streamlit.
"""

import json
import os
import base64
import traceback
from datetime import datetime, timezone
from pathlib import Path

from app.auditor import IntelligentAuditor
from app.config import (
    CATEGORY_KEYWORDS,
    EPROCURE_MAX_PAGES,
    EPROCURE_RESULTS_PER_CATEGORY,
)
from app.db import (
    get_previously_open_tenders,
    init_db,
    log_system_status,
    save_tender,
)
from app.notifier import send_alert
from app.scraper import fetch_tender_sources


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

LIVE_TENDERS_FILE = DATA_DIR / "live_tenders.json"
HEALTH_FILE = DATA_DIR / "health.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(file_path: Path, payload: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with file_path.open("w", encoding="utf-8") as file:
        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )


def write_health(
    status: str,
    message: str,
    details: list[dict] | None = None,
):
    payload = {
        "status": status,
        "message": message,
        "updated_at": utc_now(),
        "details": details or [],
    }

    write_json(HEALTH_FILE, payload)

    try:
        log_system_status(status, message)
    except Exception as error:
        print(f"[HEALTH WARNING] SQLite log failed: {error}")


def stable_tender_key(source_url: str) -> str:
    """
    Build a dedup key that's stable across repeat encounters of the
    SAME tender, even though eprocure.gov.in's detail URL embeds a
    volatile per-request timestamp token that makes two fetches of the
    identical tender look like different URLs.

    Confirmed by manual decoding: the detail URL is a series of
    base64-encoded segments joined by "A13h1". For two URLs observed
    pointing at the exact same tender, every segment matched exactly
    except one — which decoded to two Unix timestamps 44 seconds
    apart (a click/view-tracking token), while the LAST segment (the
    real tender reference, e.g. "2026_MES_787323_1") and the document
    number segment just before it were identical in both.

    So the key uses the last two decoded segments (document number +
    tender reference) when the URL matches this format, which is the
    part of the URL that's actually stable across duplicate sightings
    of the same tender. Falls back to the raw URL for any link that
    doesn't match — safer to under-merge than to wrongly collapse two
    different tenders together.
    """

    parts = source_url.strip().split("A13h1")

    if len(parts) < 2:
        return source_url.strip().lower()

    decoded_tail = []

    for part in parts[-2:]:
        try:
            padded = part + "=" * (-len(part) % 4)
            decoded_tail.append(base64.b64decode(padded).decode())
        except Exception:
            # Not the expected format — bail out to the raw URL
            # rather than risk merging unrelated tenders.
            return source_url.strip().lower()

    return "|".join(decoded_tail).strip().lower()


def deduplicate_tenders(tenders: list[dict]) -> list[dict]:
    unique = []
    seen = set()

    for tender in tenders:
        title = str(tender.get("title", "")).strip().lower()
        source_url = str(tender.get("source_url", "")).strip()

        if not title or not source_url:
            continue

        key = (title, stable_tender_key(source_url))

        if key in seen:
            continue

        seen.add(key)
        unique.append(tender)

    return unique


def run_pipeline() -> int:
    print("=" * 70)
    print("TENDER INTELLIGENCE AGENT — REAL LIVE DATA SCAN")
    print("Build: agent_runner v3 (pagination fix + retry/backoff + "
          "issued_by override active)")
    print(f"Started: {utc_now()}")
    print("=" * 70)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        init_db()

        write_health(
            "RUNNING",
            "Live tender scan started.",
        )

        auditor = IntelligentAuditor()

    except Exception as error:
        message = f"Pipeline initialization failed: {error}"

        write_health(
            "FAILED",
            message,
        )

        send_alert(message, severity="CRITICAL")
        raise

    all_tenders = []
    source_details = []
    failed_categories = 0

    for category, keywords in CATEGORY_KEYWORDS.items():
        print(f"\n[SEARCH] Category: {category}")
        print(f"[SEARCH] Keywords: {keywords}")

        category_detail = {
            "category": category,
            "keywords": keywords,
            "status": "UNKNOWN",
            "documents_found": 0,
            "verified_tenders": 0,
            "errors": [],
        }

        try:
            documents = fetch_tender_sources(
                category=category,
                keywords=keywords,
                max_results=EPROCURE_RESULTS_PER_CATEGORY,
                max_pages=EPROCURE_MAX_PAGES,
            )

            category_detail["documents_found"] = len(documents)

            print(
                f"[SEARCH] eprocure.gov.in matched "
                f"{len(documents)} document(s)."
            )

            if not documents:
                category_detail["status"] = "NO_RESULTS"
                source_details.append(category_detail)
                continue

            category_detail["status"] = "SUCCESS"

            for document in documents:
                source_url = document.get("url", "")
                source_content = document.get("content", "")
                known_organisation = str(
                    document.get("organisation", "")
                ).strip()

                if not source_url or not source_content:
                    category_detail["errors"].append(
                        "Document had no URL or content."
                    )
                    continue

                print(f"[AUDIT] Checking: {source_url}")

                try:
                    extracted = auditor.analyze_document(
                        document_content=source_content,
                        document_url=source_url,
                        target_category=category,
                    )

                    # issued_by is already known with certainty from the
                    # listing table itself — no need to trust Gemini's
                    # extraction of it from free text (it was observed
                    # inconsistently returning "NOT SURE" even when the
                    # organisation name was explicitly present in the
                    # content it was given).
                    if known_organisation:
                        for tender in extracted:
                            print(
                                f"[FIX] Overriding issued_by -> "
                                f"{known_organisation!r}"
                            )
                            tender["issued_by"] = known_organisation

                    category_detail["verified_tenders"] += len(extracted)
                    all_tenders.extend(extracted)

                except Exception as error:
                    error_message = (
                        f"{type(error).__name__}: {error}"
                    )

                    category_detail["errors"].append(
                        f"{source_url}: {error_message}"
                    )

                    print(f"[AUDIT ERROR] {error_message}")

            if category_detail["errors"]:
                category_detail["status"] = "WARNING"

        except Exception as error:
            failed_categories += 1
            category_detail["status"] = "FAILED"
            category_detail["errors"].append(
                f"{type(error).__name__}: {error}"
            )

            print(
                f"[SEARCH ERROR] {category}: "
                f"{type(error).__name__}: {error}"
            )

        source_details.append(category_detail)

        # Write progress after each category, not just at the very
        # end. If the process is killed partway through (terminal
        # closed, Ctrl+C, crash outside the per-category try/except
        # above), health.json still reflects real, recent progress
        # instead of being frozen on the single "Live tender scan
        # started" message written before category 1 even began.
        write_health(
            "RUNNING",
            f"Scanning... {len(source_details)}/{len(CATEGORY_KEYWORDS)} "
            f"categories done. Last: {category} "
            f"({category_detail['status']}).",
            source_details,
        )

    # Everything from here on used to be unguarded: if any single line
    # below raised (e.g. a JSON-serialization error while writing
    # live_tenders.json), the exception would escape run_pipeline()
    # entirely and health.json would stay stuck on "RUNNING" forever,
    # with live_tenders.json never written. Wrapping it means a crash
    # here always ends with an honest "FAILED" status instead of a
    # silently stuck pipeline.
    try:
        verified_tenders = deduplicate_tenders(all_tenders)

        for tender in verified_tenders:
            try:
                save_tender(tender)
            except Exception as error:
                print(
                    f"[DATABASE WARNING] Could not save "
                    f"{tender.get('title', 'Unknown')}: {error}"
                )

        # Merge in tenders found in EARLIER runs that are still open
        # today, not just ones re-discovered in this specific run. A
        # tender can scroll past the scan window as newer, unrelated
        # tenders get posted on a high-volume portal, while remaining
        # perfectly valid and open — it shouldn't vanish from the
        # dashboard just because this run didn't happen to re-find it.
        try:
            today_iso = datetime.now(timezone.utc).date().isoformat()
            previously_open = get_previously_open_tenders(today_iso)
        except Exception as error:
            print(f"[DATABASE WARNING] Could not load prior open tenders: {error}")
            previously_open = []

        verified_tenders = deduplicate_tenders(
            verified_tenders + previously_open
        )

        live_payload = {
            "data_source": "LIVE_FETCHED_DATA",
            "generated_at": utc_now(),
            "record_count": len(verified_tenders),
            "tenders": verified_tenders,
            "source_summary": source_details,
        }

        write_json(LIVE_TENDERS_FILE, live_payload)

        if failed_categories == len(CATEGORY_KEYWORDS):
            message = (
                "All live tender searches failed. "
                "The dashboard must not treat this as a normal empty result."
            )

            write_health(
                "FAILED",
                message,
                source_details,
            )

            send_alert(
                message,
                severity="CRITICAL",
            )

            raise RuntimeError(message)

        if not verified_tenders:
            message = (
                "Scan completed, but no verified open tenders met "
                "all strict rules."
            )

            write_health(
                "SUCCESS_ZERO_RESULTS",
                message,
                source_details,
            )

        elif any(
            detail["status"] in {"WARNING", "FAILED"}
            for detail in source_details
        ):
            message = (
                f"Scan completed with warnings. "
                f"{len(verified_tenders)} verified tender(s) found."
            )

            write_health(
                "SUCCESS_WITH_WARNINGS",
                message,
                source_details,
            )

        else:
            message = (
                f"Scan completed successfully. "
                f"{len(verified_tenders)} verified tender(s) found."
            )

            write_health(
                "SUCCESS",
                message,
                source_details,
            )

        print("\n" + "=" * 70)
        print(message)
        print(f"Saved to: {LIVE_TENDERS_FILE}")
        print("=" * 70)

        return len(verified_tenders)

    except RuntimeError:
        # Already handled above (health + alert already written).
        raise

    except Exception as error:
        tb = traceback.format_exc()
        message = (
            f"Pipeline crashed after search phase: "
            f"{type(error).__name__}: {error}"
        )

        print(f"\n[FATAL] {message}\n{tb}")

        # Best-effort: still try to leave a live_tenders.json behind so
        # the dashboard shows an honest empty state instead of a
        # missing file, then always mark health as FAILED.
        try:
            write_json(
                LIVE_TENDERS_FILE,
                {
                    "data_source": "LIVE_FETCHED_DATA",
                    "generated_at": utc_now(),
                    "record_count": 0,
                    "tenders": [],
                    "source_summary": source_details,
                },
            )
        except Exception as write_error:
            print(f"[FATAL] Could not write fallback live data: {write_error}")

        write_health(
            "FAILED",
            message,
            source_details + [{"traceback": tb[-2000:]}],
        )

        send_alert(message, severity="CRITICAL")

        raise