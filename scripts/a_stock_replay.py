#!/usr/bin/env python
"""Build paper replay metrics for daily A-share recommendation candidates.

The replay is a research audit layer, not a trading executor. It assumes a
candidate generated after a daily snapshot can first be observed at the next
available trading day's open, then measures subsequent close/high/low outcomes
from local K-line rows.
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

from a_stock_daily import DEFAULT_DB, as_float, connect, init_db, now_cn


REPLAY_VERSION = "paper-replay-v0.1-next-open"
HORIZONS = (1, 3, 5, 10, 20)
MAX_ENTRY_GAP_DAYS = 7


@dataclass
class ReplayResult:
    run_id: int
    rank: int
    code: str
    name: str
    run_date: str
    strategy_version: str
    candidate_score: float
    candidate_price: float | None
    entry_date: str | None
    entry_price: float | None
    entry_source: str
    latest_date: str | None
    latest_close: float | None
    trading_days_observed: int
    returns: dict[int, float | None]
    latest_return_pct: float | None
    max_close_return_pct: float | None
    max_intraday_return_pct: float | None
    worst_intraday_return_pct: float | None
    max_drawdown_pct: float | None
    best_date: str | None
    worst_date: str | None
    take_profit_5_hit: int
    stop_loss_5_hit: int
    stop_loss_8_hit: int
    status: str
    note: str | None
    raw: dict[str, Any]


def parse_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def pct(current: float | None, base: float | None) -> float | None:
    if current is None or base in (None, 0):
        return None
    return round((current / base - 1.0) * 100.0, 2)


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def calendar_gap_days(start: str, end: str) -> int | None:
    start_date = parse_date(start)
    end_date = parse_date(end)
    if not start_date or not end_date:
        return None
    return (end_date - start_date).days


def load_candidate_rows(
    conn: sqlite3.Connection,
    run_id: int | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if run_id is not None:
        clauses.append("rr.id = ?")
        params.append(run_id)
    if since:
        clauses.append("rr.run_date >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    limit_sql = "LIMIT ?" if limit else ""
    if limit:
        params.append(limit)
    return conn.execute(
        f"""
        SELECT
            rr.id AS run_id,
            rr.run_date,
            rr.strategy_version,
            rc.rank,
            rc.code,
            rc.name,
            rc.score,
            rc.metrics_json
        FROM recommendation_candidates rc
        JOIN recommendation_runs rr ON rr.id = rc.run_id
        {where}
        ORDER BY rr.run_date ASC, rr.id ASC, rc.rank ASC
        {limit_sql}
        """,
        params,
    ).fetchall()


def load_kline_after(conn: sqlite3.Connection, code: str, run_date: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT trade_date, open, close, high, low, source
        FROM stock_kline_daily
        WHERE code = ? AND trade_date > ?
        ORDER BY trade_date ASC
        """,
        (code, run_date),
    ).fetchall()


def max_drawdown_from_lows(rows: list[sqlite3.Row]) -> float | None:
    peak: float | None = None
    worst: float | None = None
    for row in rows:
        high = as_float(row["high"])
        low = as_float(row["low"])
        close = as_float(row["close"])
        reference = high if high is not None else close
        if reference is not None:
            peak = reference if peak is None else max(peak, reference)
        if peak in (None, 0):
            continue
        trough = low if low is not None else close
        if trough is None:
            continue
        drawdown = (trough / peak - 1.0) * 100.0
        worst = drawdown if worst is None else min(worst, drawdown)
    return round(worst, 2) if worst is not None else None


def build_replay_result(conn: sqlite3.Connection, row: sqlite3.Row) -> ReplayResult:
    metrics = parse_json(row["metrics_json"])
    candidate_price = as_float(metrics.get("price"))
    run_date = row["run_date"]
    code = row["code"]
    klines = load_kline_after(conn, code, run_date)
    base_raw = {
        "replay_version": REPLAY_VERSION,
        "horizons": HORIZONS,
        "entry_rule": "next available trading day open after recommendation run date",
        "candidate_metrics": metrics,
    }
    if not klines:
        return ReplayResult(
            run_id=row["run_id"],
            rank=row["rank"],
            code=code,
            name=row["name"],
            run_date=run_date,
            strategy_version=row["strategy_version"],
            candidate_score=row["score"],
            candidate_price=candidate_price,
            entry_date=None,
            entry_price=None,
            entry_source="next_open_unavailable",
            latest_date=None,
            latest_close=None,
            trading_days_observed=0,
            returns={h: None for h in HORIZONS},
            latest_return_pct=None,
            max_close_return_pct=None,
            max_intraday_return_pct=None,
            worst_intraday_return_pct=None,
            max_drawdown_pct=None,
            best_date=None,
            worst_date=None,
            take_profit_5_hit=0,
            stop_loss_5_hit=0,
            stop_loss_8_hit=0,
            status="no_entry_kline",
            note="No local K-line row after the recommendation date.",
            raw=base_raw | {"kline_rows": 0},
        )

    entry = klines[0]
    gap = calendar_gap_days(run_date, entry["trade_date"])
    if gap is not None and gap > MAX_ENTRY_GAP_DAYS:
        return ReplayResult(
            run_id=row["run_id"],
            rank=row["rank"],
            code=code,
            name=row["name"],
            run_date=run_date,
            strategy_version=row["strategy_version"],
            candidate_score=row["score"],
            candidate_price=candidate_price,
            entry_date=entry["trade_date"],
            entry_price=as_float(entry["open"]),
            entry_source="stale_next_open",
            latest_date=None,
            latest_close=None,
            trading_days_observed=0,
            returns={h: None for h in HORIZONS},
            latest_return_pct=None,
            max_close_return_pct=None,
            max_intraday_return_pct=None,
            worst_intraday_return_pct=None,
            max_drawdown_pct=None,
            best_date=None,
            worst_date=None,
            take_profit_5_hit=0,
            stop_loss_5_hit=0,
            stop_loss_8_hit=0,
            status="stale_entry_gap",
            note=f"First K-line after run date is {gap} calendar days later; replay withheld.",
            raw=base_raw | {"kline_rows": len(klines), "entry_gap_days": gap},
        )

    entry_price = as_float(entry["open"])
    if entry_price in (None, 0):
        return ReplayResult(
            run_id=row["run_id"],
            rank=row["rank"],
            code=code,
            name=row["name"],
            run_date=run_date,
            strategy_version=row["strategy_version"],
            candidate_score=row["score"],
            candidate_price=candidate_price,
            entry_date=entry["trade_date"],
            entry_price=entry_price,
            entry_source="next_open_missing",
            latest_date=None,
            latest_close=None,
            trading_days_observed=0,
            returns={h: None for h in HORIZONS},
            latest_return_pct=None,
            max_close_return_pct=None,
            max_intraday_return_pct=None,
            worst_intraday_return_pct=None,
            max_drawdown_pct=None,
            best_date=None,
            worst_date=None,
            take_profit_5_hit=0,
            stop_loss_5_hit=0,
            stop_loss_8_hit=0,
            status="missing_entry_price",
            note="Entry K-line exists but open price is missing.",
            raw=base_raw | {"kline_rows": len(klines), "entry_gap_days": gap},
        )

    returns: dict[int, float | None] = {}
    for horizon in HORIZONS:
        index = horizon - 1
        close = as_float(klines[index]["close"]) if len(klines) > index else None
        returns[horizon] = pct(close, entry_price)

    latest = klines[-1]
    latest_close = as_float(latest["close"])
    close_returns = [
        (pct(as_float(item["close"]), entry_price), item["trade_date"])
        for item in klines
        if as_float(item["close"]) is not None
    ]
    high_returns = [
        (pct(as_float(item["high"]), entry_price), item["trade_date"])
        for item in klines
        if as_float(item["high"]) is not None
    ]
    low_returns = [
        (pct(as_float(item["low"]), entry_price), item["trade_date"])
        for item in klines
        if as_float(item["low"]) is not None
    ]
    valid_close = [(value, day) for value, day in close_returns if value is not None]
    valid_high = [(value, day) for value, day in high_returns if value is not None]
    valid_low = [(value, day) for value, day in low_returns if value is not None]
    best_high = max(valid_high, key=lambda item: item[0]) if valid_high else (None, None)
    worst_low = min(valid_low, key=lambda item: item[0]) if valid_low else (None, None)
    max_close = max((value for value, _ in valid_close), default=None)
    trading_days_observed = len(klines)
    status = "complete_20d" if trading_days_observed >= 20 else "open"

    return ReplayResult(
        run_id=row["run_id"],
        rank=row["rank"],
        code=code,
        name=row["name"],
        run_date=run_date,
        strategy_version=row["strategy_version"],
        candidate_score=row["score"],
        candidate_price=candidate_price,
        entry_date=entry["trade_date"],
        entry_price=round(entry_price, 4),
        entry_source="next_open",
        latest_date=latest["trade_date"],
        latest_close=latest_close,
        trading_days_observed=trading_days_observed,
        returns=returns,
        latest_return_pct=pct(latest_close, entry_price),
        max_close_return_pct=round(max_close, 2) if max_close is not None else None,
        max_intraday_return_pct=best_high[0],
        worst_intraday_return_pct=worst_low[0],
        max_drawdown_pct=max_drawdown_from_lows(klines),
        best_date=best_high[1],
        worst_date=worst_low[1],
        take_profit_5_hit=1 if best_high[0] is not None and best_high[0] >= 5 else 0,
        stop_loss_5_hit=1 if worst_low[0] is not None and worst_low[0] <= -5 else 0,
        stop_loss_8_hit=1 if worst_low[0] is not None and worst_low[0] <= -8 else 0,
        status=status,
        note=None,
        raw=base_raw
        | {
            "kline_rows": len(klines),
            "entry_gap_days": gap,
            "entry_kline_source": entry["source"],
            "latest_kline_source": latest["source"],
        },
    )


def save_replay_results(conn: sqlite3.Connection, results: list[ReplayResult]) -> None:
    now = now_cn().isoformat(timespec="seconds")
    rows = []
    for result in results:
        rows.append(
            (
                result.run_id,
                result.rank,
                result.code,
                result.name,
                result.run_date,
                result.strategy_version,
                result.candidate_score,
                result.candidate_price,
                result.entry_date,
                result.entry_price,
                result.entry_source,
                result.latest_date,
                result.latest_close,
                result.trading_days_observed,
                result.returns.get(1),
                result.returns.get(3),
                result.returns.get(5),
                result.returns.get(10),
                result.returns.get(20),
                result.latest_return_pct,
                result.max_close_return_pct,
                result.max_intraday_return_pct,
                result.worst_intraday_return_pct,
                result.max_drawdown_pct,
                result.best_date,
                result.worst_date,
                result.take_profit_5_hit,
                result.stop_loss_5_hit,
                result.stop_loss_8_hit,
                result.status,
                result.note,
                json.dumps(result.raw, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            )
        )
    conn.executemany(
        """
        INSERT INTO paper_replay_results(
            run_id, rank, code, name, run_date, strategy_version, candidate_score,
            candidate_price, entry_date, entry_price, entry_source, latest_date,
            latest_close, trading_days_observed, return_1d_pct, return_3d_pct,
            return_5d_pct, return_10d_pct, return_20d_pct, latest_return_pct,
            max_close_return_pct, max_intraday_return_pct, worst_intraday_return_pct,
            max_drawdown_pct, best_date, worst_date, take_profit_5_hit,
            stop_loss_5_hit, stop_loss_8_hit, status, note, raw_json, created_at,
            updated_at
        ) VALUES(
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(run_id, rank) DO UPDATE SET
            code=excluded.code,
            name=excluded.name,
            run_date=excluded.run_date,
            strategy_version=excluded.strategy_version,
            candidate_score=excluded.candidate_score,
            candidate_price=excluded.candidate_price,
            entry_date=excluded.entry_date,
            entry_price=excluded.entry_price,
            entry_source=excluded.entry_source,
            latest_date=excluded.latest_date,
            latest_close=excluded.latest_close,
            trading_days_observed=excluded.trading_days_observed,
            return_1d_pct=excluded.return_1d_pct,
            return_3d_pct=excluded.return_3d_pct,
            return_5d_pct=excluded.return_5d_pct,
            return_10d_pct=excluded.return_10d_pct,
            return_20d_pct=excluded.return_20d_pct,
            latest_return_pct=excluded.latest_return_pct,
            max_close_return_pct=excluded.max_close_return_pct,
            max_intraday_return_pct=excluded.max_intraday_return_pct,
            worst_intraday_return_pct=excluded.worst_intraday_return_pct,
            max_drawdown_pct=excluded.max_drawdown_pct,
            best_date=excluded.best_date,
            worst_date=excluded.worst_date,
            take_profit_5_hit=excluded.take_profit_5_hit,
            stop_loss_5_hit=excluded.stop_loss_5_hit,
            stop_loss_8_hit=excluded.stop_loss_8_hit,
            status=excluded.status,
            note=excluded.note,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        rows,
    )


def refresh_replay_results(
    conn: sqlite3.Connection,
    run_id: int | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[ReplayResult]:
    candidates = load_candidate_rows(conn, run_id=run_id, since=since, limit=limit)
    results = [build_replay_result(conn, row) for row in candidates]
    if results:
        save_replay_results(conn, results)
    return results


def replay_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status IN ('open', 'complete_20d') THEN 1 ELSE 0 END) AS active_rows,
            AVG(latest_return_pct) AS avg_latest_return,
            AVG(return_1d_pct) AS avg_1d_return,
            AVG(return_3d_pct) AS avg_3d_return,
            SUM(take_profit_5_hit) AS take_profit_5_hits,
            SUM(stop_loss_5_hit) AS stop_loss_5_hits,
            SUM(CASE WHEN latest_return_pct > 0 THEN 1 ELSE 0 END) AS positive_latest,
            MAX(latest_date) AS latest_replay_date
        FROM paper_replay_results
        """
    ).fetchone()
    return dict(row) if row else {}


def print_summary(conn: sqlite3.Connection, results: list[ReplayResult]) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    summary = replay_summary(conn)
    parts = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"Refreshed {len(results)} paper replay rows. {parts}")
    if summary.get("total"):
        total = summary["total"] or 0
        active = summary["active_rows"] or 0
        positive = summary["positive_latest"] or 0
        hit_rate = positive / active * 100 if active else 0
        print(
            "Replay summary: "
            f"rows={total}, active={active}, latest={summary.get('latest_replay_date')}, "
            f"avg_latest={summary.get('avg_latest_return'):.2f}% "
            f"avg_T1={summary.get('avg_1d_return'):.2f}% "
            f"positive_rate={hit_rate:.1f}%"
        )


def show_rows(conn: sqlite3.Connection, top: int = 20) -> None:
    rows = conn.execute(
        """
        SELECT
            run_id, rank, code, name, run_date, entry_date, latest_date,
            trading_days_observed, latest_return_pct, return_1d_pct,
            max_intraday_return_pct, worst_intraday_return_pct, status, note
        FROM paper_replay_results
        ORDER BY run_date DESC, run_id DESC, rank ASC
        LIMIT ?
        """,
        (top,),
    ).fetchall()
    if not rows:
        print("No paper replay rows found.")
        return
    print("run rank code   name       entry      latest     days latest%  T+1%  max%   min%   status")
    for row in rows:
        print(
            f"{row['run_id']:>3} {row['rank']:>4} {row['code']:<6} {row['name'][:8]:<8} "
            f"{str(row['entry_date'] or '-'):<10} {str(row['latest_date'] or '-'):<10} "
            f"{row['trading_days_observed']:>4} "
            f"{row['latest_return_pct'] if row['latest_return_pct'] is not None else '-':>7} "
            f"{row['return_1d_pct'] if row['return_1d_pct'] is not None else '-':>6} "
            f"{row['max_intraday_return_pct'] if row['max_intraday_return_pct'] is not None else '-':>6} "
            f"{row['worst_intraday_return_pct'] if row['worst_intraday_return_pct'] is not None else '-':>6} "
            f"{row['status']}"
        )
        if row["note"]:
            print(f"     note: {row['note']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh paper replay metrics for A-share recommendations")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="command", required=True)
    refresh = sub.add_parser("refresh", help="Refresh replay rows")
    refresh.add_argument("--run-id", type=int)
    refresh.add_argument("--since")
    refresh.add_argument("--limit", type=int)
    show = sub.add_parser("show", help="Show replay rows")
    show.add_argument("--top", type=int, default=20)
    sub.add_parser("summary", help="Show aggregate replay summary")
    args = parser.parse_args()

    with connect(args.db) as conn:
        init_db(conn)
        if args.command == "refresh":
            results = refresh_replay_results(conn, run_id=args.run_id, since=args.since, limit=args.limit)
            conn.commit()
            print_summary(conn, results)
        elif args.command == "show":
            show_rows(conn, args.top)
        elif args.command == "summary":
            print(json.dumps(replay_summary(conn), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
