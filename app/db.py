"""
Optional SQLite audit storage.

The deployed Streamlit dashboard reads JSON from data/live_tenders.json.
SQLite is retained only as a local audit copy.
"""

import os
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./data/tenders.db",
)

if DATABASE_URL.startswith("sqlite:///"):
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    db_folder = os.path.dirname(db_path)

    if db_folder:
        os.makedirs(db_folder, exist_ok=True)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {},
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


class TenderRecord(Base):
    __tablename__ = "tenders"

    id = Column(Integer, primary_key=True)
    title = Column(String(1000), nullable=False)
    source_url = Column(String(2000), nullable=False)
    category = Column(String(200), nullable=False)
    closing_date = Column(String(30), nullable=False)
    issued_by = Column(String(500), default="NOT SURE")
    qualification_criteria = Column(Text, default="NOT SURE")
    eligibility_status = Column(String(50), default="NOT SURE")
    is_net_cost = Column(Boolean, default=False)
    is_open_now = Column(Boolean, default=False)
    confidence = Column(String(20), default="LOW")
    evidence = Column(Text, default="NOT SURE")
    found_at = Column(DateTime, default=datetime.utcnow)


class SystemLog(Base):
    __tablename__ = "system_logs"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    status = Column(String(40), nullable=False)
    message = Column(Text, nullable=False)


def init_db():
    Base.metadata.create_all(bind=engine)


def save_tender(tender: dict) -> bool:
    """Save once using title + original source URL as duplicate key."""

    session = SessionLocal()

    try:
        existing = session.query(TenderRecord).filter_by(
            title=tender["title"],
            source_url=tender["source_url"],
        ).first()

        if existing:
            return False

        session.add(
            TenderRecord(
                title=tender["title"],
                source_url=tender["source_url"],
                category=tender["category"],
                closing_date=tender["closing_date"],
                issued_by=tender.get("issued_by", "NOT SURE"),
                qualification_criteria=tender.get(
                    "qualification_criteria",
                    "NOT SURE",
                ),
                eligibility_status=tender.get(
                    "eligibility_status",
                    "NOT SURE",
                ),
                is_net_cost=tender.get("is_net_cost", False),
                is_open_now=tender.get("is_open_now", False),
                confidence=tender.get("confidence", "LOW"),
                evidence=tender.get("evidence", "NOT SURE"),
            )
        )

        session.commit()
        return True

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


def get_previously_open_tenders(today_iso: str) -> list:
    """
    Return tenders from earlier runs whose closing_date hasn't passed
    yet, in the same dict shape as a freshly-scraped/verified tender.

    Why this exists: each run only re-scans the ~1,000 most recent
    listings on eprocure.gov.in per category. On a high-volume portal
    that posts hundreds of new tenders daily across ALL categories
    (not just the four we care about), a tender found yesterday can
    easily scroll past that window within a day even though it is
    still genuinely open for bidding. Without this, live_tenders.json
    would silently drop a real, still-open tender the very next run
    it isn't re-discovered in — which directly undermines "open the
    page and there is a real tender on it that is open today".

    Comparing closing_date as an ISO string ("YYYY-MM-DD" style) works
    because that format sorts/compares correctly as plain text.
    """

    session = SessionLocal()

    try:
        records = (
            session.query(TenderRecord)
            .filter(TenderRecord.closing_date >= today_iso)
            .all()
        )

        return [
            {
                "title": record.title,
                "source_url": record.source_url,
                "category": record.category,
                "closing_date": record.closing_date,
                "issued_by": record.issued_by,
                "qualification_criteria": record.qualification_criteria,
                "eligibility_status": record.eligibility_status,
                "is_net_cost": record.is_net_cost,
                "is_open_now": record.is_open_now,
                "confidence": record.confidence,
                "evidence": record.evidence,
                "found_at": (
                    record.found_at.isoformat()
                    if record.found_at
                    else ""
                ),
            }
            for record in records
        ]

    finally:
        session.close()


def log_system_status(status: str, message: str):
    session = SessionLocal()

    try:
        session.add(
            SystemLog(
                status=status.upper(),
                message=message,
            )
        )
        session.commit()

    except Exception:
        session.rollback()

    finally:
        session.close()