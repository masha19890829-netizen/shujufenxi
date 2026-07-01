#!/usr/bin/env python
"""Build the v0.5 Xinwei evidence ledger and formula gate snapshots.

This module is deliberately local-only. It consumes stored evidence rows and
research coverage, links S/A evidence to the six Xinwei dimensions, and writes
queryable gate snapshots. It does not upgrade any stock to buy-eligible unless
all six dimensions are manually verified against S/A evidence.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, XINWEI_DIMENSIONS, connect, init_db, now_cn, today_cn
from a_stock_opportunity import refresh_thesis_events


LEDGER_VERSION = "xinwei-evidence-ledger-v0.5"
SOURCE_ID = "evidence_gate_v0.5"
STALE_DAYS = 365
VERIFIED_STATUSES = {"supported", "verified", "pass"}
FAILED_STATUSES = {"failed", "rejected", "conflict"}
SA_GRADES = {"S", "A"}
CRITICAL_DIMENSIONS = {"scarcity_position", "leader_customer_binding"}


DIMENSION_KEYWORDS: dict[str, list[str]] = {
    "industry_inflection": [
        "AI",
        "\u4eba\u5de5\u667a\u80fd",
        "\u6298\u53e0",
        "\u5c04\u9891",
        "\u5929\u7ebf",
        "\u536b\u661f",
        "\u673a\u5668\u4eba",
        "\u7aef\u4fa7",
        "\u5546\u4e1a\u536b\u661f",
        "\u4eba\u5f62\u673a\u5668\u4eba",
        "\u4ea7\u4e1a\u5316",
        "\u5bfc\u5165",
    ],
    "scarcity_position": [
        "\u4e13\u5229",
        "\u8ba4\u8bc1",
        "\u72ec\u5bb6",
        "\u9996\u5bb6",
        "\u6280\u672f",
        "\u58c1\u5792",
        "\u9f99\u5934",
        "\u7a00\u7f3a",
        "\u7a00\u571f",
        "\u7a7a\u767d\u63a9\u6a21",
        "\u56fd\u4ea7\u66ff\u4ee3",
        "\u5e02\u5360",
    ],
    "leader_customer_binding": [
        "\u5ba2\u6237",
        "\u4f9b\u5e94",
        "\u4f9b\u5e94\u5546",
        "\u6838\u5fc3\u4f9b\u5e94\u94fe",
        "\u82f9\u679c",
        "\u534e\u4e3a",
        "\u5c0f\u7c73",
        "\u4e09\u661f",
        "\u82f1\u4f1f\u8fbe",
        "\u7279\u65af\u62c9",
        "\u6bd4\u4e9a\u8fea",
        "\u5b81\u5fb7\u65f6\u4ee3",
        "\u8ba2\u5355",
    ],
    "capacity_order_expansion": [
        "\u8ba2\u5355",
        "\u5408\u540c",
        "\u4e2d\u6807",
        "\u4ea7\u80fd",
        "\u6269\u4ea7",
        "\u52df\u6295",
        "\u5b9a\u589e",
        "\u9879\u76ee",
        "\u6295\u8d44",
        "\u5728\u5efa",
        "\u4ea4\u4ed8",
        "\u9884\u4ed8",
        "\u5408\u540c\u8d1f\u503a",
    ],
    "earnings_inflection": [
        "\u5e74\u5ea6\u62a5\u544a",
        "\u5b63\u5ea6\u62a5\u544a",
        "\u534a\u5e74\u5ea6\u62a5\u544a",
        "\u4e1a\u7ee9",
        "\u6263\u975e",
        "\u5229\u6da6",
        "\u8425\u6536",
        "\u6536\u5165",
        "\u6bdb\u5229",
        "\u626d\u4e8f",
        "\u589e\u957f",
    ],
}

BANNED_TEXT_MARKERS = [
    "\u4f20\u95fb",
    "\u7f51\u4f20",
    "\u636e\u4f20",
    "\u80a1\u5427",
    "\u5fae\u4fe1\u7fa4",
    "\u65e0\u6765\u6e90",
    "\u622a\u56fe",
    "\u5c0f\u4f5c\u6587",
    "\u4e1a\u5185\u996d\u5c40",
    "\u4e0d\u4fbf\u62ab\u9732",
]

TASK_TITLES = {
    "industry_inflection": "Verify industrial inflection timeline",
    "scarcity_position": "Verify scarcity position and barrier",
    "leader_customer_binding": "Verify leader customer binding",
    "capacity_order_expansion": "Verify capacity/order expansion",
    "earnings_inflection": "Verify non-recurring-adjusted earnings inflection",
    "expectation_gap": "Verify expectation gap and coverage dispersion",
}

TASK_DETAILS = {
    "industry_inflection": "Confirm policy, technical, customer-introduction, or order catalysts from S/A sources.",
    "scarcity_position": "Check market share, patents, certification, exclusive supply, or capacity barriers.",
    "leader_customer_binding": "Confirm formal customer/order/supply-chain disclosure. Sampling, rumors, and non-disclosure replies are not enough.",
    "capacity_order_expansion": "Separate planned capacity from actual shipment. Check capex, construction, contract liability, prepayments, and order visibility.",
    "earnings_inflection": "Use revenue structure, gross margin, operating cash flow context, and non-recurring-adjusted profit rather than one-off gains.",
    "expectation_gap": "Check analyst coverage, target-price dispersion, old-business valuation anchors, and market debate.",
}


@dataclass(frozen=True)
class GateRefreshResult:
    snapshot_date: str
    evidence_links: int
    gate_snapshots: int
    research_tasks: int


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def parse_json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def dimension_names() -> dict[str, str]:
    return {dimension_id: dimension_name for dimension_id, dimension_name in XINWEI_DIMENSIONS}


def resolve_snapshot_date(conn: sqlite3.Connection, requested: str | None = None) -> str:
    if requested and requested != "latest":
        return requested
    row = conn.execute(
        """
        SELECT COALESCE(
            (SELECT MAX(trade_date) FROM stock_model_scores),
            (SELECT MAX(trade_date) FROM watchlist_daily_metrics),
            (SELECT MAX(trade_date) FROM market_snapshot)
        ) AS snapshot_date
        """
    ).fetchone()
    return row["snapshot_date"] if row and row["snapshot_date"] else today_cn()


def load_codes(conn: sqlite3.Connection, codes: list[str] | None = None) -> list[sqlite3.Row]:
    if codes:
        placeholders = ",".join("?" for _ in codes)
        return conn.execute(
            f"""
            SELECT code, name
            FROM stock_watchlist
            WHERE code IN ({placeholders})
            ORDER BY code
            """,
            codes,
        ).fetchall()
    return conn.execute(
        """
        SELECT code, name
        FROM stock_watchlist
        ORDER BY
            CASE WHEN status = 'active' THEN 0 ELSE 1 END,
            first_recommended_date DESC,
            first_rank ASC
        """
    ).fetchall()


def clean_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts).strip()


def is_allowed_evidence(row: sqlite3.Row) -> bool:
    if row["evidence_grade"] not in SA_GRADES:
        return False
    text = clean_text(row["title"], row["summary"])
    return not any(marker in text for marker in BANNED_TEXT_MARKERS)


def matched_keywords(row: sqlite3.Row, dimension_id: str) -> list[str]:
    keywords = DIMENSION_KEYWORDS.get(dimension_id, [])
    if not keywords:
        return []
    text = clean_text(row["title"], row["summary"])
    text_lower = text.lower()
    found: list[str] = []
    for keyword in keywords:
        if keyword.lower() in text_lower:
            found.append(keyword)
    return found


def load_reviews(conn: sqlite3.Connection, code: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT *
        FROM stock_xinwei_reviews
        WHERE code = ?
        """,
        (code,),
    ).fetchall()
    return {row["dimension_id"]: row for row in rows}


def load_latest_coverage(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM stock_research_coverage
        WHERE code = ?
        ORDER BY as_of_date DESC
        LIMIT 1
        """,
        (code,),
    ).fetchone()


def load_evidence(conn: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM stock_evidence_items
        WHERE code = ?
          AND evidence_grade IN ('S', 'A')
        ORDER BY evidence_date DESC, id DESC
        LIMIT 160
        """,
        (code,),
    ).fetchall()


def days_between(left: str | None, right: str) -> int | None:
    if not left:
        return None
    try:
        left_date = date.fromisoformat(left[:10])
        right_date = date.fromisoformat(right[:10])
    except ValueError:
        return None
    return (right_date - left_date).days


def link_status_for_review(review: sqlite3.Row | None) -> tuple[str, int]:
    if review and review["status"] in VERIFIED_STATUSES and review["evidence_grade"] in SA_GRADES:
        return "verified", 1
    if review and review["status"] in FAILED_STATUSES:
        return "conflict", 0
    return "needs_review", 0


def build_evidence_links_for_code(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    now_text: str,
) -> int:
    reviews = load_reviews(conn, code)
    evidence_rows = [row for row in load_evidence(conn, code) if is_allowed_evidence(row)]
    inserted = 0
    for row in evidence_rows:
        for dimension_id, dimension_name in XINWEI_DIMENSIONS:
            if dimension_id == "expectation_gap":
                continue
            keywords = matched_keywords(row, dimension_id)
            if not keywords:
                continue
            review = reviews.get(dimension_id)
            status, confirmed = link_status_for_review(review)
            reason = f"Matched {dimension_id} keywords: {', '.join(keywords[:5])}"
            conn.execute(
                """
                INSERT INTO xinwei_evidence_links(
                    code, name, dimension_id, dimension_name, evidence_item_id,
                    evidence_grade, evidence_status, match_reason, match_keywords,
                    source, evidence_type, title, evidence_date, source_url,
                    is_manual_confirmed, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, dimension_id, evidence_item_id) DO UPDATE SET
                    name=excluded.name,
                    dimension_name=excluded.dimension_name,
                    evidence_grade=excluded.evidence_grade,
                    evidence_status=excluded.evidence_status,
                    match_reason=excluded.match_reason,
                    match_keywords=excluded.match_keywords,
                    source=excluded.source,
                    evidence_type=excluded.evidence_type,
                    title=excluded.title,
                    evidence_date=excluded.evidence_date,
                    source_url=excluded.source_url,
                    is_manual_confirmed=excluded.is_manual_confirmed,
                    updated_at=excluded.updated_at
                """,
                (
                    code,
                    name,
                    dimension_id,
                    dimension_name,
                    row["id"],
                    row["evidence_grade"],
                    status,
                    reason,
                    json_dumps(keywords),
                    row["source"],
                    row["evidence_type"],
                    row["title"],
                    row["evidence_date"],
                    row["source_url"],
                    confirmed,
                    now_text,
                    now_text,
                ),
            )
            inserted += 1
    return inserted


def load_links_by_dimension(conn: sqlite3.Connection, code: str) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        """
        SELECT *
        FROM xinwei_evidence_links
        WHERE code = ?
        ORDER BY
            CASE evidence_grade WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 9 END,
            evidence_date DESC,
            id DESC
        """,
        (code,),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["dimension_id"], []).append(row)
    return grouped


def coverage_dimension_status(coverage: sqlite3.Row | None) -> tuple[str, str | None, list[dict[str, Any]]]:
    if not coverage:
        return "pending", None, []
    detail = {
        "type": "research_coverage",
        "as_of_date": coverage["as_of_date"],
        "report_count_180d": coverage["report_count_180d"],
        "org_count": coverage["org_count"],
        "latest_report_date": coverage["latest_report_date"],
        "latest_rating": coverage["latest_rating"],
    }
    return "needs_review", "A", [detail]


def dimension_status(
    dimension_id: str,
    dimension_name: str,
    review: sqlite3.Row | None,
    links: list[sqlite3.Row],
    coverage: sqlite3.Row | None,
    snapshot_date: str,
) -> dict[str, Any]:
    review_status = review["status"] if review else "pending"
    review_grade = review["evidence_grade"] if review else None
    if review_status in FAILED_STATUSES:
        status = "failed"
    elif dimension_id == "expectation_gap":
        coverage_status, coverage_grade, coverage_chain = coverage_dimension_status(coverage)
        has_verified_review = review_status in VERIFIED_STATUSES and review_grade in SA_GRADES and coverage is not None
        status = "verified" if has_verified_review else coverage_status
        return {
            "dimension_id": dimension_id,
            "dimension_name": dimension_name,
            "status": status,
            "evidence_grade": review_grade or coverage_grade,
            "review_status": review_status,
            "link_count": 0,
            "latest_evidence_date": coverage["latest_report_date"] if coverage else None,
            "evidence_chain": coverage_chain,
        }
    elif review_status in VERIFIED_STATUSES and review_grade in SA_GRADES and links:
        latest_date = links[0]["evidence_date"]
        age = days_between(latest_date, snapshot_date)
        status = "stale" if age is not None and age > STALE_DAYS else "verified"
    elif links:
        latest_date = links[0]["evidence_date"]
        age = days_between(latest_date, snapshot_date)
        status = "stale" if age is not None and age > STALE_DAYS else "needs_review"
    else:
        status = "pending"

    evidence_chain = [
        {
            "evidence_item_id": link["evidence_item_id"],
            "evidence_grade": link["evidence_grade"],
            "evidence_status": link["evidence_status"],
            "evidence_date": link["evidence_date"],
            "source": link["source"],
            "evidence_type": link["evidence_type"],
            "title": link["title"],
            "source_url": link["source_url"],
            "match_reason": link["match_reason"],
            "match_keywords": parse_json(link["match_keywords"]) or [],
        }
        for link in links[:5]
    ]
    return {
        "dimension_id": dimension_id,
        "dimension_name": dimension_name,
        "status": status,
        "evidence_grade": review_grade or (links[0]["evidence_grade"] if links else None),
        "review_status": review_status,
        "link_count": len(links),
        "latest_evidence_date": links[0]["evidence_date"] if links else None,
        "evidence_chain": evidence_chain,
    }


def gate_status_from_counts(supported: int, needs_review: int, pending: int, failed: int, stale: int) -> str:
    if failed:
        return "rejected"
    if supported >= len(XINWEI_DIMENSIONS) and not needs_review and not pending and not stale:
        return "formula_supported"
    if needs_review or stale:
        return "needs_manual_review"
    return "missing_evidence"


def task_type_for_status(dimension_id: str, status: str) -> str:
    if status == "failed":
        return "resolve_conflict"
    if status == "stale":
        return "refresh_stale_evidence"
    if status == "needs_review":
        return "manual_verify_evidence"
    if dimension_id == "leader_customer_binding":
        return "verify_formal_customer_order"
    if dimension_id == "capacity_order_expansion":
        return "verify_actual_capacity_order"
    return "collect_sa_evidence"


def task_priority(dimension_id: str, status: str) -> int:
    if status == "failed":
        return 1
    if dimension_id in CRITICAL_DIMENSIONS:
        return 1
    if status in {"needs_review", "stale"}:
        return 2
    return 3


def write_research_tasks(
    conn: sqlite3.Connection,
    snapshot_date: str,
    code: str,
    name: str,
    dimensions: list[dict[str, Any]],
    now_text: str,
) -> int:
    conn.execute(
        """
        DELETE FROM research_tasks
        WHERE task_date = ?
          AND code = ?
          AND source = ?
          AND status = 'open'
        """,
        (snapshot_date, code, SOURCE_ID),
    )
    count = 0
    for dim in dimensions:
        if dim["status"] == "verified":
            continue
        dimension_id = dim["dimension_id"]
        dimension_name = dim["dimension_name"]
        task_type = task_type_for_status(dimension_id, dim["status"])
        title = TASK_TITLES.get(dimension_id, f"Verify {dimension_id}")
        detail = TASK_DETAILS.get(dimension_id, "Collect or verify S/A evidence.")
        if dim["status"] == "needs_review":
            detail = f"{detail} Current S/A links need manual confirmation."
        elif dim["status"] == "pending":
            detail = f"{detail} No valid S/A link is currently available."
        elif dim["status"] == "stale":
            detail = f"{detail} Existing evidence is stale and must be refreshed."
        elif dim["status"] == "failed":
            detail = f"{detail} Existing evidence has been marked as conflicting or rejected."
        conn.execute(
            """
            INSERT INTO research_tasks(
                task_date, code, name, dimension_id, dimension_name, task_type,
                priority, status, title, detail, source, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
            ON CONFLICT(task_date, code, dimension_id, task_type) DO UPDATE SET
                name=excluded.name,
                dimension_name=excluded.dimension_name,
                priority=excluded.priority,
                title=excluded.title,
                detail=excluded.detail,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            (
                snapshot_date,
                code,
                name,
                dimension_id,
                dimension_name,
                task_type,
                task_priority(dimension_id, dim["status"]),
                title,
                detail,
                SOURCE_ID,
                now_text,
                now_text,
            ),
        )
        count += 1
    return count


def build_gate_snapshot_for_code(
    conn: sqlite3.Connection,
    snapshot_date: str,
    code: str,
    name: str,
    now_text: str,
) -> tuple[int, int]:
    reviews = load_reviews(conn, code)
    links_by_dimension = load_links_by_dimension(conn, code)
    coverage = load_latest_coverage(conn, code)
    dimensions: list[dict[str, Any]] = []
    for dimension_id, dimension_name in XINWEI_DIMENSIONS:
        dimensions.append(
            dimension_status(
                dimension_id,
                dimension_name,
                reviews.get(dimension_id),
                links_by_dimension.get(dimension_id, []),
                coverage,
                snapshot_date,
            )
        )

    supported = sum(1 for dim in dimensions if dim["status"] == "verified")
    needs_review = sum(1 for dim in dimensions if dim["status"] == "needs_review")
    pending = sum(1 for dim in dimensions if dim["status"] == "pending")
    failed = sum(1 for dim in dimensions if dim["status"] == "failed")
    stale = sum(1 for dim in dimensions if dim["status"] == "stale")
    gate_status = gate_status_from_counts(supported, needs_review, pending, failed, stale)
    blocking = [dim["dimension_id"] for dim in dimensions if dim["status"] != "verified"]
    evidence_chain = {
        dim["dimension_id"]: dim["evidence_chain"]
        for dim in dimensions
    }

    conn.execute(
        """
        INSERT INTO xinwei_gate_snapshots(
            snapshot_date, code, name, gate_status, eligible_for_buy,
            supported_count, needs_review_count, pending_count, failed_count,
            stale_count, required_count, blocking_dimensions,
            dimension_status_json, evidence_chain_json, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_date, code) DO UPDATE SET
            name=excluded.name,
            gate_status=excluded.gate_status,
            eligible_for_buy=excluded.eligible_for_buy,
            supported_count=excluded.supported_count,
            needs_review_count=excluded.needs_review_count,
            pending_count=excluded.pending_count,
            failed_count=excluded.failed_count,
            stale_count=excluded.stale_count,
            required_count=excluded.required_count,
            blocking_dimensions=excluded.blocking_dimensions,
            dimension_status_json=excluded.dimension_status_json,
            evidence_chain_json=excluded.evidence_chain_json,
            updated_at=excluded.updated_at
        """,
        (
            snapshot_date,
            code,
            name,
            gate_status,
            1 if gate_status == "formula_supported" else 0,
            supported,
            needs_review,
            pending,
            failed,
            stale,
            len(XINWEI_DIMENSIONS),
            json_dumps(blocking),
            json_dumps(dimensions),
            json_dumps(evidence_chain),
            now_text,
            now_text,
        ),
    )
    task_count = write_research_tasks(conn, snapshot_date, code, name, dimensions, now_text)
    return 1, task_count


def refresh_evidence_gate(
    conn: sqlite3.Connection,
    snapshot_date: str | None = None,
    codes: list[str] | None = None,
) -> GateRefreshResult:
    snapshot_date = resolve_snapshot_date(conn, snapshot_date)
    rows = load_codes(conn, codes)
    now_text = now_cn().isoformat(timespec="seconds")
    if rows:
        placeholders = ",".join("?" for _ in rows)
        code_args = [row["code"] for row in rows]
        conn.execute(
            f"""
            DELETE FROM xinwei_evidence_links
            WHERE code IN ({placeholders})
              AND is_manual_confirmed = 0
            """,
            code_args,
        )
    link_count = 0
    snapshot_count = 0
    task_count = 0
    for row in rows:
        code = row["code"]
        name = row["name"]
        link_count += build_evidence_links_for_code(conn, code, name, now_text)
        snapshots, tasks = build_gate_snapshot_for_code(conn, snapshot_date, code, name, now_text)
        snapshot_count += snapshots
        task_count += tasks
    if rows:
        refresh_thesis_events(conn, snapshot_date, [row["code"] for row in rows])
    return GateRefreshResult(snapshot_date, link_count, snapshot_count, task_count)


def latest_gate_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(snapshot_date) AS snapshot_date FROM xinwei_gate_snapshots").fetchone()
    return row["snapshot_date"] if row and row["snapshot_date"] else None


def load_gate_snapshot(conn: sqlite3.Connection, code: str, snapshot_date: str | None = None) -> sqlite3.Row | None:
    snapshot_date = resolve_snapshot_date(conn, snapshot_date)
    return conn.execute(
        """
        SELECT *
        FROM xinwei_gate_snapshots
        WHERE snapshot_date = ?
          AND code = ?
        """,
        (snapshot_date, code),
    ).fetchone()


def command_refresh(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        result = refresh_evidence_gate(conn, args.date, args.code)
        conn.commit()
    print(
        "Refreshed "
        f"{result.gate_snapshots} gate snapshots for {result.snapshot_date}. "
        f"links={result.evidence_links}, tasks={result.research_tasks}, version={LEDGER_VERSION}"
    )


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        if not load_gate_snapshot(conn, args.code, args.date):
            result = refresh_evidence_gate(conn, args.date, [args.code])
            conn.commit()
            snapshot_date = result.snapshot_date
        else:
            snapshot_date = resolve_snapshot_date(conn, args.date)
        row = load_gate_snapshot(conn, args.code, snapshot_date)
        if not row:
            print(f"No gate snapshot found for {args.code}.")
            return
        dimensions = parse_json(row["dimension_status_json"]) or []
        print(
            f"{row['snapshot_date']} {row['code']} {row['name']} "
            f"status={row['gate_status']} eligible={bool(row['eligible_for_buy'])} "
            f"supported={row['supported_count']}/{row['required_count']} "
            f"needs_review={row['needs_review_count']} pending={row['pending_count']} "
            f"failed={row['failed_count']} stale={row['stale_count']}"
        )
        for dim in dimensions:
            chain = dim.get("evidence_chain") or []
            latest = chain[0].get("title") if chain else "-"
            print(
                f"- {dim['dimension_id']} {dim['dimension_name']}: "
                f"{dim['status']} grade={dim.get('evidence_grade') or '-'} "
                f"links={dim.get('link_count', 0)} latest={latest}"
            )


def command_report(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        snapshot_date = resolve_snapshot_date(conn, args.date)
        if not latest_gate_date(conn):
            refresh_evidence_gate(conn, snapshot_date)
            conn.commit()
        rows = conn.execute(
            """
            SELECT
                xgs.*,
                sm.priority_rank,
                sm.action_bucket,
                sm.total_score,
                sm.market_score,
                sm.evidence_score,
                sm.factor_score,
                sm.risk_score
            FROM xinwei_gate_snapshots xgs
            LEFT JOIN stock_model_scores sm
              ON sm.code = xgs.code
             AND sm.trade_date = xgs.snapshot_date
            WHERE xgs.snapshot_date = ?
            ORDER BY
                COALESCE(sm.priority_rank, 9999),
                xgs.code
            LIMIT ?
            """,
            (snapshot_date, args.top),
        ).fetchall()
        counts = conn.execute(
            """
            SELECT gate_status, COUNT(*) AS n
            FROM xinwei_gate_snapshots
            WHERE snapshot_date = ?
            GROUP BY gate_status
            ORDER BY n DESC
            """,
            (snapshot_date,),
        ).fetchall()
        tasks = conn.execute(
            """
            SELECT *
            FROM research_tasks
            WHERE task_date = ?
              AND status = 'open'
            ORDER BY priority ASC, code ASC, dimension_id ASC
            LIMIT ?
            """,
            (snapshot_date, args.top),
        ).fetchall()
    print(f"Xinwei evidence gate report date={snapshot_date} version={LEDGER_VERSION}")
    print("Gate counts: " + ", ".join(f"{row['gate_status']}={row['n']}" for row in counts))
    for row in rows:
        blocking = parse_json(row["blocking_dimensions"]) or []
        print(
            f"{row['priority_rank'] or '-':>3}. {row['code']} {row['name']} "
            f"bucket={row['action_bucket'] or '-'} gate={row['gate_status']} "
            f"eligible={bool(row['eligible_for_buy'])} "
            f"supported={row['supported_count']}/{row['required_count']} "
            f"blocking={','.join(blocking[:4]) or '-'}"
        )
    if tasks:
        print("Open research tasks:")
        for task in tasks:
            print(
                f"- P{task['priority']} {task['code']} {task['name']} "
                f"{task['dimension_id']} {task['task_type']}: {task['title']}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh and inspect the Xinwei v0.5 evidence gate")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--date", default="latest", help="Snapshot date or 'latest'")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="Refresh evidence links, gate snapshots, and research tasks")
    refresh.add_argument("--code", action="append", help="Optional six-digit code; repeat for multiple codes")

    show = sub.add_parser("show", help="Show one stock's gate snapshot")
    show.add_argument("--code", required=True, help="Six-digit A-share code")

    report = sub.add_parser("report", help="Print a compact evidence-gate report")
    report.add_argument("--top", type=int, default=30, help="Rows/tasks to show")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "refresh":
            command_refresh(args)
        elif args.command == "show":
            command_show(args)
        elif args.command == "report":
            command_report(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
