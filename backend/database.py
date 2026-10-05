"""
database.py — PostgreSQL persistence for the Legal Metrology checker.
"""

import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from models import (
    ComplianceResult,
    ExtractedData,
    InspectionDetail,
    InspectionSummary,
    Stats,
    ViolationCount,
)

BASE_DIR = Path(__file__).resolve().parent

# Uploaded files are still stored locally.
# Database records themselves are persistent in Neon.
UPLOADS_DIR = BASE_DIR / "uploads"

SOURCE_LIVE_AI = "live_ai"
SOURCE_DEMO_CACHED = "demo_cached"
SOURCE_SEED = "seed"


def get_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    return psycopg.connect(
        database_url,
        row_factory=dict_row,
    )


def init_db() -> None:
    """Create the inspections table if it does not exist."""

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inspections (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                product_name TEXT NOT NULL,
                manufacturer TEXT NOT NULL,
                scan_date TEXT NOT NULL,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                image_path TEXT,
                extracted_json TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                explanation TEXT,
                source TEXT NOT NULL,
                model_used TEXT
            )
            """
        )


def save_inspection(
    extracted: dict,
    compliance: ComplianceResult,
    image_filename: str | None,
    scan_date: str,
    source: str,
    model_used: str | None = None,
    explanation: str | None = None,
) -> int:

    product_name = (
        extracted.get("product_name")
        or extracted.get("common_generic_name")
        or "Unidentified product"
    )

    manufacturer = (
        extracted.get("manufacturer_name")
        or extracted.get("packer_name")
        or extracted.get("importer_name")
        or "Not declared"
    )

    with get_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO inspections (
                product_name,
                manufacturer,
                scan_date,
                score,
                status,
                image_path,
                extracted_json,
                checks_json,
                explanation,
                source,
                model_used
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
            """,
            (
                product_name,
                manufacturer,
                scan_date,
                compliance.score,
                compliance.status,
                image_filename,
                json.dumps(extracted),
                compliance.model_dump_json(),
                explanation,
                source,
                model_used,
            ),
        ).fetchone()

        return row["id"]


def _row_to_summary(row) -> InspectionSummary:
    return InspectionSummary(
        id=row["id"],
        product_name=row["product_name"],
        manufacturer=row["manufacturer"],
        scan_date=row["scan_date"],
        score=row["score"],
        status=row["status"],
        source=row["source"],
    )


def _row_to_detail(row) -> InspectionDetail:
    return InspectionDetail(
        **_row_to_summary(row).model_dump(),
        image_url=(
            f"/uploads/{row['image_path']}"
            if row["image_path"]
            else None
        ),
        extracted=ExtractedData(**json.loads(row["extracted_json"])),
        compliance=ComplianceResult(**json.loads(row["checks_json"])),
        explanation=row["explanation"],
        model_used=row["model_used"],
    )


def get_inspection(inspection_id: int) -> InspectionDetail | None:

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM inspections
            WHERE id = %s
            """,
            (inspection_id,),
        ).fetchone()

    return _row_to_detail(row) if row else None


def list_inspections(
    limit: int = 100,
    status: str | None = None,
) -> list[InspectionSummary]:

    query = "SELECT * FROM inspections"
    params = []

    if status:
        query += " WHERE status = %s"
        params.append(status)

    query += " ORDER BY id DESC LIMIT %s"
    params.append(limit)

    with get_connection() as connection:
        rows = connection.execute(query, params).fetchall()

    return [_row_to_summary(row) for row in rows]


def get_stats() -> Stats:

    with get_connection() as connection:

        totals = connection.execute(
            """
            SELECT
                COUNT(*) AS total,

                COUNT(*) FILTER (
                    WHERE status = 'COMPLIANT'
                ) AS compliant,

                COUNT(*) FILTER (
                    WHERE status = 'NEEDS_REVIEW'
                ) AS needs_review,

                COUNT(*) FILTER (
                    WHERE status = 'POTENTIAL_VIOLATION'
                ) AS potential_violations,

                AVG(score) AS average_score,

                COUNT(*) FILTER (
                    WHERE source = 'seed'
                ) AS seeded

            FROM inspections
            """
        ).fetchone()

        recent_rows = connection.execute(
            """
            SELECT *
            FROM inspections
            ORDER BY id DESC
            LIMIT 6
            """
        ).fetchall()

        check_rows = connection.execute(
            """
            SELECT checks_json
            FROM inspections
            """
        ).fetchall()

    total = totals["total"] or 0
    compliant = totals["compliant"] or 0

    failure_counts: dict[str, dict] = {}

    for row in check_rows:

        for check in json.loads(row["checks_json"])["checks"]:

            if check["result"] != "FAIL":
                continue

            entry = failure_counts.setdefault(
                check["rule_id"],
                {
                    "rule_id": check["rule_id"],
                    "name": check["name"],
                    "count": 0,
                },
            )

            entry["count"] += 1

    common_violations = sorted(
        failure_counts.values(),
        key=lambda item: item["count"],
        reverse=True,
    )[:5]

    return Stats(
        total=total,
        compliant=compliant,
        needs_review=totals["needs_review"] or 0,
        potential_violations=totals["potential_violations"] or 0,
        compliance_percentage=(
            round(100 * compliant / total)
            if total
            else 0
        ),
        average_score=(
            round(totals["average_score"])
            if totals["average_score"] is not None
            else 0
        ),
        includes_sample_data=bool(totals["seeded"]),
        common_violations=[
            ViolationCount(**item)
            for item in common_violations
        ],
        recent=[
            _row_to_summary(row)
            for row in recent_rows
        ],
    )
