#!/usr/bin/env python
"""Cross-check local A-share data providers for the watchlist."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from a_stock_daily import DEFAULT_DB, as_float, connect, init_db, now_cn


@dataclass
class ProviderHealthRow:
    check_date: str
    code: str
    name: str
    primary_source: str
    comparison_source: str
    primary_price: float | None
    comparison_price: float | None
    price_diff_pct: float | None
    primary_amount: float | None
    comparison_amount: float | None
    status: str
    note: str
    raw: dict[str, Any]


def latest_market_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM market_snapshot").fetchone()
    return row["trade_date"] if row and row["trade_date"] else None


def normalize_limit(limit: int | None) -> int | None:
    if limit is None or limit == 0:
        return None
    return max(1, limit)


def model_scope_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    date_row = conn.execute("SELECT MAX(trade_date) AS trade_date FROM stock_model_scores").fetchone()
    model_date = date_row["trade_date"] if date_row and date_row["trade_date"] else None
    if not model_date:
        return active_watchlist_codes(conn, limit)
    params: list[Any] = [model_date]
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT sm.code, sm.name
        FROM stock_model_scores sm
        JOIN stock_watchlist w ON w.code = sm.code
        WHERE sm.trade_date = ?
          AND w.status = 'active'
        ORDER BY sm.priority_rank ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def active_watchlist_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    params: list[Any] = []
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT code, name
        FROM stock_watchlist
        WHERE status = 'active'
        ORDER BY first_recommended_date DESC, first_rank ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def all_watchlist_codes(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    params: list[Any] = []
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ?"
        params.append(limit)
    rows = conn.execute(
        f"""
        SELECT code, name
        FROM stock_watchlist
        ORDER BY status ASC, first_recommended_date DESC, first_rank ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    return [(row["code"], row["name"]) for row in rows]


def select_codes(conn: sqlite3.Connection, scope: str, limit: int | None) -> list[tuple[str, str]]:
    if scope == "model":
        return model_scope_codes(conn, limit)
    if scope == "all":
        return all_watchlist_codes(conn, limit)
    return active_watchlist_codes(conn, limit)


def is_intraday_snapshot(created_at: str | None, check_date: str) -> bool:
    if not created_at:
        return False
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError:
        return False
    return parsed.date().isoformat() == check_date and parsed.time() < time(15, 5)


def status_for_diff(
    primary_price: float | None,
    comparison_price: float | None,
    diff_pct: float | None,
    comparison_low: float | None = None,
    comparison_high: float | None = None,
    created_at: str | None = None,
    check_date: str | None = None,
) -> tuple[str, str]:
    if primary_price is None:
        return "primary_missing", "market_snapshot price is missing"
    if comparison_price is None:
        return "comparison_missing", "same-date K-line close is missing"
    if check_date and is_intraday_snapshot(created_at, check_date):
        if comparison_low is not None and comparison_high is not None:
            if comparison_low <= primary_price <= comparison_high:
                return "range_pass", "intraday snapshot price is inside same-day K-line range"
            return "fail", "intraday snapshot price is outside same-day K-line range"
    if diff_pct is None:
        return "unknown", "price difference cannot be calculated"
    abs_diff = abs(diff_pct)
    if abs_diff <= 0.30:
        return "pass", "same-date close is aligned"
    if abs_diff <= 1.00:
        return "warn", "same-date close has mild drift"
    return "fail", "same-date close drift exceeds 1%; check adjustment, suspension or source fields"


def build_health_rows(
    conn: sqlite3.Connection,
    check_date: str,
    codes: list[tuple[str, str]],
) -> list[ProviderHealthRow]:
    if not codes:
        return []
    code_names = {code: name for code, name in codes}
    placeholders = ",".join("?" for _ in codes)
    params: list[Any] = [check_date, check_date, *code_names.keys()]
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(ms.code, k.code) AS code,
            COALESCE(ms.name, k.name) AS name,
            ms.source AS primary_source,
            k.source AS comparison_source,
            ms.price AS primary_price,
            k.close AS comparison_price,
            ms.amount AS primary_amount,
            k.amount AS comparison_amount,
            k.high AS comparison_high,
            k.low AS comparison_low,
            ms.created_at AS primary_created_at,
            ms.trade_date AS primary_date,
            k.trade_date AS comparison_date
        FROM (
            SELECT *
            FROM market_snapshot
            WHERE trade_date = ?
        ) ms
        LEFT JOIN (
            SELECT *
            FROM stock_kline_daily
            WHERE trade_date = ?
        ) k
          ON k.code = ms.code
        WHERE ms.code IN ({placeholders})
        ORDER BY ms.code
        """,
        params,
    ).fetchall()

    by_code = {row["code"]: row for row in rows}
    health_rows: list[ProviderHealthRow] = []
    for code, fallback_name in codes:
        row = by_code.get(code)
        if row is None:
            raw = {"check_date": check_date, "reason": "no market snapshot row"}
            health_rows.append(
                ProviderHealthRow(
                    check_date=check_date,
                    code=code,
                    name=fallback_name,
                    primary_source="market_snapshot",
                    comparison_source="stock_kline_daily",
                    primary_price=None,
                    comparison_price=None,
                    price_diff_pct=None,
                    primary_amount=None,
                    comparison_amount=None,
                    status="primary_missing",
                    note="no market_snapshot row for this date",
                    raw=raw,
                )
            )
            continue

        primary_price = as_float(row["primary_price"])
        comparison_price = as_float(row["comparison_price"])
        comparison_high = as_float(row["comparison_high"])
        comparison_low = as_float(row["comparison_low"])
        diff_pct = (
            round((comparison_price / primary_price - 1.0) * 100.0, 4)
            if primary_price not in (None, 0) and comparison_price is not None
            else None
        )
        status, note = status_for_diff(
            primary_price,
            comparison_price,
            diff_pct,
            comparison_low,
            comparison_high,
            row["primary_created_at"],
            check_date,
        )
        primary_source = row["primary_source"] or "market_snapshot"
        comparison_source = row["comparison_source"] or "stock_kline_daily"
        raw = {
            "primary_date": row["primary_date"],
            "comparison_date": row["comparison_date"],
            "primary_created_at": row["primary_created_at"],
            "primary_source": primary_source,
            "comparison_source": comparison_source,
            "comparison_high": comparison_high,
            "comparison_low": comparison_low,
            "check": "market_snapshot.price vs stock_kline_daily.close",
            "mode": "intraday_range" if status == "range_pass" else "close_diff",
        }
        health_rows.append(
            ProviderHealthRow(
                check_date=check_date,
                code=code,
                name=row["name"] or fallback_name,
                primary_source=primary_source,
                comparison_source=comparison_source,
                primary_price=primary_price,
                comparison_price=comparison_price,
                price_diff_pct=diff_pct,
                primary_amount=as_float(row["primary_amount"]),
                comparison_amount=as_float(row["comparison_amount"]),
                status=status,
                note=note,
                raw=raw,
            )
        )
    return health_rows


def save_health_rows(conn: sqlite3.Connection, rows: list[ProviderHealthRow]) -> None:
    for check_date in sorted({row.check_date for row in rows}):
        conn.execute("DELETE FROM provider_health_checks WHERE check_date = ?", (check_date,))
    created_at = now_cn().isoformat(timespec="seconds")
    payload = [
        (
            row.check_date,
            row.code,
            row.name,
            row.primary_source,
            row.comparison_source,
            row.primary_price,
            row.comparison_price,
            row.price_diff_pct,
            row.primary_amount,
            row.comparison_amount,
            row.status,
            row.note,
            json.dumps(row.raw, ensure_ascii=False, separators=(",", ":")),
            created_at,
        )
        for row in rows
    ]
    conn.executemany(
        """
        INSERT INTO provider_health_checks(
            check_date, code, name, primary_source, comparison_source,
            primary_price, comparison_price, price_diff_pct,
            primary_amount, comparison_amount, status, note, raw_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(check_date, code, primary_source, comparison_source) DO UPDATE SET
            name=excluded.name,
            primary_price=excluded.primary_price,
            comparison_price=excluded.comparison_price,
            price_diff_pct=excluded.price_diff_pct,
            primary_amount=excluded.primary_amount,
            comparison_amount=excluded.comparison_amount,
            status=excluded.status,
            note=excluded.note,
            raw_json=excluded.raw_json,
            created_at=excluded.created_at
        """,
        payload,
    )


def refresh_provider_health(
    conn: sqlite3.Connection,
    check_date: str | None = None,
    scope: str = "model",
    limit: int | None = 50,
) -> list[ProviderHealthRow]:
    init_db(conn)
    check_date = check_date or latest_market_date(conn)
    if not check_date:
        raise RuntimeError("No market snapshot date available.")
    codes = select_codes(conn, scope, normalize_limit(limit))
    rows = build_health_rows(conn, check_date, codes)
    if rows:
        save_health_rows(conn, rows)
    return rows


def summarize(rows: list[ProviderHealthRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return counts


def command_refresh(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        rows = refresh_provider_health(conn, args.date, args.scope, args.limit)
        conn.commit()
    counts = summarize(rows)
    parts = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    date_text = rows[0].check_date if rows else args.date or "-"
    print(f"Refreshed {len(rows)} provider health rows for {date_text}. {parts}")


def command_show(args: argparse.Namespace) -> None:
    with connect(args.db) as conn:
        init_db(conn)
        date = args.date
        if not date:
            row = conn.execute("SELECT MAX(check_date) AS check_date FROM provider_health_checks").fetchone()
            date = row["check_date"] if row and row["check_date"] else None
        if not date:
            print("No provider health rows found.")
            return
        rows = conn.execute(
            """
            SELECT *
            FROM provider_health_checks
            WHERE check_date = ?
            ORDER BY
                CASE status
                    WHEN 'fail' THEN 0
                    WHEN 'warn' THEN 1
                    WHEN 'comparison_missing' THEN 2
                    WHEN 'primary_missing' THEN 3
                    WHEN 'unknown' THEN 4
                    WHEN 'range_pass' THEN 5
                    ELSE 5
                END,
                ABS(COALESCE(price_diff_pct, 0)) DESC,
                code ASC
            LIMIT ?
            """,
            (date, args.top),
        ).fetchall()
        summary = conn.execute(
            """
            SELECT status, COUNT(*) AS n
            FROM provider_health_checks
            WHERE check_date = ?
            GROUP BY status
            ORDER BY status
            """,
            (date,),
        ).fetchall()
    print(f"Provider health date={date}")
    print(", ".join(f"{row['status']}={row['n']}" for row in summary))
    for row in rows:
        print(
            f"{row['code']} {row['name']} {row['status']} "
            f"snapshot={row['primary_price']} kline={row['comparison_price']} "
            f"diff={row['price_diff_pct']}% {row['note']}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-check local A-share data providers")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument("--date", help="Check date, defaults to latest market snapshot date")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="Refresh provider health rows")
    refresh.add_argument("--scope", choices=["model", "active", "all"], default="model")
    refresh.add_argument("--limit", type=int, default=50, help="0 means no limit")

    show = sub.add_parser("show", help="Show provider health rows")
    show.add_argument("--top", type=int, default=30)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "refresh":
            command_refresh(args)
        elif args.command == "show":
            command_show(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
